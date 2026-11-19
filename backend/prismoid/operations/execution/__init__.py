# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Execution - the engine room that runs one step, as a package.

`__init__` keeps the whole public surface - `from prismoid.operations import execution`
and every `execution.name` reference works exactly as before. The seams:
  mounts.py   - what a step's frame is loaded with (upstream artifacts, inputs, skills)
  taps.py     - frames emitted during a step (journal, step events, preview, narration)
  settle.py   - a guarded run's typed ending, read
  caps.py     - a step's gate budgets and the words a spent one answers with
An attempt (`_attempt`) is a pipeline of stages, one module each:
  acquire.py  - the step's frame
  equip.py    - its mounts and the run's hands (`TaskDeps`; the browser's are in ports.py)
  run.py      - the agent, built and run, and its typed ending read
  classify.py - a run that stopped without ending, as the one `Deferral` its park saves
  publish.py  - its output through the one door
  harvest.py  - what it delivered, checked against its contract
`execute_step` is a short driver: the door triages entry, then each rung dispatches to one handler
per outcome (`handlers/`: entry, rung, deferral, failure, fence, expand, published, produce). The
helpers those handlers share, one module each:
  parks.py    - opening a park: the gate a stopped run waits on
  wake.py     - a park's wake: a gate's resolution mapped into the run's resume
  giveback.py - a parked frame given back, and the ends a park can come to
  steers.py   - the steers a step takes, and fence 1
  finish.py   - done, unverified, with QA notes or escalated; dependents promoted
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.deps import TaskDeps
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanEdit as PlanEditRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.errors import ModelIncapable, NotFound, PolicyDenied
from prismoid.operations import call_log, live, machines, steering, stopped
from prismoid.operations import egress_allowances as allow_ops
from prismoid.operations import gates as gate_ops
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import routines as routine_ops
from prismoid.operations.execution import (
    acquire,
    classify,
    ending,
    equip,
    express_rung,
    giveback,
    harvest,
    publish,
    run,
    steers,
)
from prismoid.operations.execution import wake as wake  # tests patch the wake's races on it
from prismoid.operations.execution.caps import (
    APPROVAL_UNMATCHED as APPROVAL_UNMATCHED,
)
from prismoid.operations.execution.caps import (
    DENIED_BY_USER as DENIED_BY_USER,
)
from prismoid.operations.execution.caps import (
    MAX_STEP_HANDOFF_GATES as MAX_STEP_HANDOFF_GATES,
)
from prismoid.operations.execution.caps import (
    MAX_STEP_QUESTION_GATES as MAX_STEP_QUESTION_GATES,
)
from prismoid.operations.execution.caps import (
    NOT_RUN_BESIDE_HANDOFF as NOT_RUN_BESIDE_HANDOFF,
)
from prismoid.operations.execution.caps import (
    OUT_OF_APPROVALS as OUT_OF_APPROVALS,
)
from prismoid.operations.execution.caps import (
    OUT_OF_HANDOFFS as OUT_OF_HANDOFFS,
)
from prismoid.operations.execution.caps import (
    OUT_OF_QUESTIONS as OUT_OF_QUESTIONS,
)
from prismoid.operations.execution.deferrals import (
    NOT_RUN_FOR_CONTROL as NOT_RUN_FOR_CONTROL,
)
from prismoid.operations.execution.deferrals import (
    ApprovalDeferral as ApprovalDeferral,
)
from prismoid.operations.execution.deferrals import AttemptResult, Produced
from prismoid.operations.execution.deferrals import (
    ConnectDeferral as ConnectDeferral,
)
from prismoid.operations.execution.deferrals import (
    ControlDeferral as ControlDeferral,
)
from prismoid.operations.execution.deferrals import (
    Deferral as Deferral,
)
from prismoid.operations.execution.deferrals import (
    PauseDeferral as PauseDeferral,
)
from prismoid.operations.execution.deferrals import browser_park as _browser_park
from prismoid.operations.execution.device import Cut as _Cut
from prismoid.operations.execution.device import DeviceAway, NoChrome
from prismoid.operations.execution.device import check_chrome as _check_chrome
from prismoid.operations.execution.device import cut_short as _cut_short
from prismoid.operations.execution.device import owed_cut as _owed_cut
from prismoid.operations.execution.ending import (
    fail as fail,  # the turn's failed ending lives beside its delivered one
)
from prismoid.operations.execution.errors import IncompleteStep
from prismoid.operations.execution.finish import MAX_VERIFY_ATTEMPTS as MAX_VERIFY_ATTEMPTS
from prismoid.operations.execution.giveback import close_cancelled_park as close_cancelled_park
from prismoid.operations.execution.handlers import (
    deferral,
    entry,
    expand,
    failure,
    fence,
    produce,
    published,
    rung,
)
from prismoid.operations.execution.handlers.state import AGAIN, Ladder, Ran, Round
from prismoid.operations.execution.mounts import (
    materialise_skills as materialise_skills,
)
from prismoid.operations.execution.mounts import (
    seat_filename as seat_filename,
)
from prismoid.operations.execution.mounts import (
    superseded_paths as _superseded_paths,
)
from prismoid.operations.execution.record import ASKING as ASKING
from prismoid.operations.execution.record import HANDBACKS as HANDBACKS
from prismoid.operations.execution.record import OWED_CUT, owed_run
from prismoid.operations.execution.record import PACING as PACING  # the workflow reads `execution.PACING`
from prismoid.operations.execution.taps import (
    step_event as _step_event,
)
from prismoid.operations.execution.wake import control_answer as control_answer
from prismoid.operations.execution.wake import store_answer as store_answer
from prismoid.schemas.computer_use import DEFAULT_ALLOWLIST

# A delta is a *preview*, not the document: 3500 bytes is what fits inside NOTIFY's ceiling
# alongside the envelope, and what a reader can take in.
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import (
    PlanEdited,
    StepDequeued,
    TaskDelivered,
    TaskDequeued,
    TaskStatusChanged,
)
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.park import park_of
from prismoid.schemas.plan import PlanEdit as PlanEditView
from prismoid.schemas.plan import PlanEditKind, StepSpec, StepState, TaskStatus
from prismoid.schemas.plan import is_seat as is_seat  # re-exported: the ladder and the seat tests ask here
from prismoid.schemas.steering import (
    REJECT_CANCELLED,
    REJECT_TURN_ENDED,
    SafePoint,
    SteerNote,
    replan_reason,
    rerun_text,
)


def _check_computer_use(profile: str) -> None:
    """The kill switch: off, a browser-profiled step is not dispatched, resumed, or
    reattached - the reason names the exact env var an operator flips. A plain step never gates
    on this at all; `settings.computer_use.enabled` has nothing to do with `base`/`code` frames."""
    if profile == "browser" and not settings.computer_use.enabled:
        raise PolicyDenied(
            "computer use is disabled (PRISMOID_COMPUTER_USE_ENABLED=false); the step was not run"
        )


def _with_parked_allowlist(allowed: set[str], resume: dict[str, object]) -> set[str]:
    """The homes recomputed now plus the park's `once` widens: `frame.allow` replaces the proxy list, and the DB
    never holds a `once`. Only `once` widens ride, so an always-grant revoked while parked stays gone."""
    return set(allowed) | set(park_of(resume).browser_once)


async def _browser_allowlist(session: AsyncSession, task: Task, contract: Contract) -> set[str]:
    """The frame's egress allowlist, the union of four equal homes: the contract's domains, the task's (and
    ancestors') accepted grants, the creator's always-grants, and the shipped default."""

    allowed = set(contract.allow_domains)
    allowed.update(await allow_ops.grants_for_task(session, task))
    allowed.update(await allow_ops.hosts_for_owner(session, task.creator_actor_id))
    # This union must never end up empty for a browser step: on sandbar a sandbox born with an
    # empty allowlist has no egress principal at all (docker.go's instance.Egress refuses it,
    # ErrBadSpec) and can never be widened later, so DEFAULT_ALLOWLIST always contributes something.
    allowed.update(DEFAULT_ALLOWLIST)
    return allowed


async def frame_allowlist(session: AsyncSession, task: Task, spec: StepSpec) -> set[str]:
    """Every host the step's frame may reach: a browser frame's four homes, any other
    frame's contract domains alone. The one list `acquire` pushes, and what R53 reads to ask
    whether a shell could have acted outside (`steering.reruns`). Only a browser frame is ever
    widened live, and its list is never empty, so a widen never gives a frame its first route out."""
    if spec.contract is None:
        raise IncompleteStep(spec.title, "contract")
    if machines.seat_profile(task, spec) == "browser":
        return await _browser_allowlist(session, task, spec.contract)
    return set(spec.contract.allow_domains)


async def record_dequeued(session: AsyncSession, task_id: uuid.UUID, turn: int = 1) -> None:
    """One durable `task.dequeued`, the record's queue hop (created or continued → dequeued); measurement takes
    the first mark per turn, since a crash-resume may append a second."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskDequeued(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            turn=turn,
        ),
    )


async def record_step_dequeued(session: AsyncSession, task_id: uuid.UUID, step_id: uuid.UUID) -> None:
    """One durable `step.dequeued`: the step's child workflow started on the step
    queue. `task.status(running) → step.dequeued` is queue hop 2, per step, on the record."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        StepDequeued(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step_id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
        ),
    )


async def advance(
    session: AsyncSession, task_id: uuid.UUID, to: TaskStatus, *, turn: int | None = None, reason: str = ""
) -> str:
    """Move a task's status and put it on the stream, in one transaction.

    `transition()` raises `InvalidTransition` on an illegal move - including on a *replay*
    that tries to advance a task that already advanced. That is not a nuisance, it is the machine
    refusing to be double-applied, and the caller decides whether that means "already done" or
    "genuinely wrong". A stop carries its account (`stopped.account`) and `reason` (the budget's).

    `turn` is the stale-writer guard: a crash-recovered turn-N workflow re-running an un-journalled status
    write while turn N+1 is live would otherwise flip the NEW turn's status (a recovered `mark_running` on a
    planning continuation makes `draft_plan` think it already planned - a silent wrong delivery). A workflow
    that names a turn the row has moved past writes nothing.

    The status is read under the task row lock (`Task.held`), so a pause that committed after
    the caller loaded its copy is seen: a park or a gate's answer leaves a paused task paused
    (`Task.stays_paused`), whichever park it is.
    """
    task = await Task.held(session, task_id)

    if turn is not None and task.turn != turn:
        logfire.warn(
            "stale turn-{turn} workflow tried {to} on task {t} (now turn {now}); refused",
            turn=turn,
            to=to.value,
            t=str(task_id),
            now=task.turn,
        )
        return task.status

    if task.state is to or task.stays_paused(to):
        return task.status  # already there (a replayed effect), or paused until a person resumes

    task.transition(to)
    if task.is_terminal():
        await call_log.forget(session, task.id)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskStatusChanged(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            status=to,
            reason=reason,
            summary=await stopped.account(session, task) if to is TaskStatus.CANCELLED else "",
        ),
    )
    if to is TaskStatus.CANCELLED:
        # A gate's cancel (budget, clarification) stops the task as surely as the header's: what
        # never applied says so here too, or its bubble reads "accepted" forever.
        await steering.reject_unapplied(session, task, REJECT_CANCELLED)
    return task.status


async def execute_step(
    session: AsyncSession,
    task_id: uuid.UUID,
    step_id: uuid.UUID,
    *,
    seat: bool = False,
    handed_back: int | None = None,
) -> str:
    """Run one step until it has produced, or give up with a diagnosis; verification signs after delivery
    (ADR-019), except the fleet's in-line review. `handed_back` counts the run_step workflow's hand-backs."""
    ladder = await entry.triage(session, task_id, step_id, seat=seat, handed_back=handed_back)
    if isinstance(ladder, str):
        return ladder
    while True:
        said = await _rung(ladder)
        if said is not None:
            return said


async def _rung(ladder: Ladder) -> str | None:
    """One rung of the ladder: the step's final word, or None to go round again."""
    ladder.rung_at, ladder.no_machine = time.monotonic(), False
    if (
        ladder.resume is None
        and ladder.injected is None
        and (stopped := await rung.open_fresh(ladder)) is not None
    ):
        return stopped
    # Each attempt's contract carries the steers the step has taken - a resume's included (its
    # saved epoch), and the injected review's.
    attempt_spec = await steers.steered(ladder.session, ladder.task, ladder.step, ladder.spec)
    ran = await _run(ladder, attempt_spec)
    if not isinstance(ran, Ran):
        return None if ran is AGAIN else ran
    if await fence.fence_one(ladder, ran) is AGAIN:
        return None
    ended = fence.typed_end(ran)
    await fence.blocked(ladder, ran)
    if await expand.expand(ladder, ran) is AGAIN:
        return None
    if ran.undelivered is None:
        said = await published.deliver(ladder, ran, ended)
    else:
        said = await produce.nothing_published(ladder, ran)
    return None if said is AGAIN else said


async def _run(ladder: Ladder, attempt_spec: StepSpec) -> Ran | str | Round:
    """The rung's run, or the review it owes: what it produced, the step's final word (a park), or
    `AGAIN` (a park answered in place). The attempt's exceptions are read here, inside the same
    `try` as the park handlers, so a park that raises meets the same arms an attempt does."""
    if ladder.injected is not None:
        # The VERIFYING re-entry: the produce is durable, only the review is owed. Feed the settled artifacts
        # straight to the outcome handlers - no sandbox, no attempt bump, no model call re-bought.
        narration, produced, outline = ladder.injected
        ladder.injected = None
        return Ran(narration, produced, outline, attempted=False)
    # **A resume is consumed by its attempt, success or failure.** If the resumed attempt raises
    # and `resume` survived, the next "retry" would re-enter the resume path - same dead frame,
    # same error, attempt never incremented, the ladder's brakes never reached. That is not a
    # hypothesis: the first fleet run spun verifying↔retrying to seq 1000+ on exactly this.
    # Consume it BEFORE the attempt; a failed resume's next rung is a fresh attempt (its own step
    # boundary), and the question is re-asked if the model still needs it.
    attempt_resume, ladder.resume = ladder.resume, None
    step = ladder.step
    fresh, ladder.reentered = not ladder.reentered and owed_run(step) is None, False  # else re-entered
    # A cut owed to a fresh attempt stays on the row until that attempt's prompt naming it commits.
    owed = _Cut.restored((step.resume_state or {}).get(OWED_CUT)) if attempt_resume is None else None
    try:
        outcome = await _attempt(
            ladder.session,
            ladder.task,
            step,
            await equip.settle_skill_pins(ladder.session, ladder.task, step, attempt_spec),
            ladder.gotchas,
            ladder.upstream,
            attempt_resume,
            seat=ladder.seated,
            cut=owed,
            paper=await rung.on_paper(ladder, attempt_spec),
            fresh=fresh,
        )
        parked = await deferral.handle(ladder, outcome)
        return Ran.of(cast(Produced, outcome)) if parked is None else parked
    except DeviceAway:
        return await failure.device_away(ladder, attempt_resume)
    except (PolicyDenied, ModelIncapable) as denied:
        if _cut_short(ladder.task, denied):
            return await failure.cut_short(ladder)
        if isinstance(denied, NoChrome):
            await failure.no_chrome(ladder, denied)
            raise
        return await failure.produced_nothing(ladder, denied)
    except Exception as failed:
        if not _cut_short(ladder.task, failed):
            raise
        return await failure.cut_short(ladder)


async def _cleared_allowlist(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    *,
    profile: str,
    resume: dict[str, object] | None,
) -> tuple[set[str], set[str]]:
    """The two walls before a frame, then the frame's list: `vouched`, what the frame's allowlist
    says now (a browser park carries only what lies beyond it), and `allowed`, what this attempt
    pushes."""
    try:
        _check_computer_use(profile)
        _check_chrome(task, profile)
        vouched = await frame_allowlist(session, task, spec)
        allowed = (
            _with_parked_allowlist(vouched, resume)
            if profile == "browser" and resume is not None
            else vouched
        )
    except Exception:
        # A resume that dies before `acquire` must give its parked frame back, or it leaks until
        # an operator sweep (a fresh attempt has no park to leak).
        if resume is not None:
            await giveback.discard_parked_frame(session, task, step, profile=profile)
        raise
    return vouched, allowed


async def _attempt(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    gotchas: list[str],
    upstream: Sequence[Artifact] = (),
    resume: dict[str, object] | None = None,
    *,
    seat: bool = False,
    cut: _Cut | None = None,
    paper: bool = False,
    fresh: bool,
) -> AttemptResult:
    """One rung: acquire -> equip -> run -> classify -> publish -> harvest, holding the frame between them
    (the task's Machine for a serial step, retries too). `resume` continues a park; `cut`: a cut command."""
    if spec.agent is None or spec.contract is None:
        raise IncompleteStep(step.id, "agent" if spec.agent is None else "contract")

    if spec.lane_role == "express":
        # The frameless rung - same ladder around it, none of the frame choreography inside it. Express
        # never parks to ask (`can_ask_user=False`): no Deferral arm, and `resume` is structurally None.
        return await express_rung.attempt_express(
            session, task, step, spec, gotchas, fresh=fresh, resumed=resume is not None
        )

    # The wire opens BEFORE the frame so even frame prep has a face (the engine facts
    # below) - a frame boot is seconds of the old dead air.
    wire = live.Wire(
        TaskId(task.id), StepId(step.id), ProjectId(task.project_id) if task.project_id else None
    )
    wire.anchor(task.event_seq)
    # Engine facts for the progress tree: the step machinery around the model - frame
    # prep, mounting, harvest, the system-side publishes - narrated on the step's channel.
    engine_fact = live.activity_emitter(wire)

    # The frame profile follows the AGENT, not the lane - `seat_profile` answers
    # "base" for anything but a browsing step and, on the device, the maker.
    # Checked BEFORE `acquire` (a fresh attempt and a resume alike): the kill switch flipped under a
    # parked gate must end the resume with the reason, not hand the model a browser it no longer has.
    profile = machines.seat_profile(task, spec)
    vouched, allowed = await _cleared_allowlist(session, task, step, spec, profile=profile, resume=resume)
    phases = acquire.phases_for(wire, engine_fact, seat=seat, paper=paper)
    acquired = await acquire.acquire(
        session, task, step, profile=profile, allowed=allowed, resume=resume, seat=seat, phases=phases
    )
    frame = run.frame_for(acquired)  # on paper: the paper until a tool needs the Machine
    parked = False  # a step that parked to ask keeps its frame alive for the resume
    deps: TaskDeps | None = None  # set once mounted below; a failure before then has none to harvest
    pulse: asyncio.Task[None] | None = None

    # From here on the step holds its frame: the `finally` below gives it back, so nothing - not
    # even a cancellation landing on the progress row's close - may run outside this `try`.
    try:
        await phases.close(acquired.label)
        owed_cut = await _owed_cut(frame, cut)
        # **No dead air**. It runs beside the step and is cancelled when the step ends -
        # `finally`, not on the happy path, because the case that most needs a sign of life is the
        # one where the step is about to blow up.
        pulse = asyncio.create_task(live.heartbeat(wire))
        bench = equip.Bench(frame=frame, acquired=acquired, allowed=allowed, profile=profile, seat=seat)
        equipped = await equip.equip(
            session,
            task,
            step,
            spec,
            bench=bench,
            upstream=upstream,
            resume=resume,
            wire=wire,
            fact=engine_fact,
        )
        deps = equipped.deps
        prepared = await run.prepare(session, task, step, spec, equipped=equipped, bench=bench, wire=wire)

        # **Release the connection before the run.** The step's session must sit *committed and idle*
        # across the agent run - the invariant the usage emitter and the journal/preview taps are
        # built on (each records on its own short session; see `usage.emitter` and taps.py). The
        # RUNNING transition committed already, but the reads that built this frame (inputs, skills,
        # catalog, routing) re-opened a transaction. Commit it, or a multi-minute model run holds a
        # pooled connection *idle in transaction* - a snapshot open the whole time (VACUUM can't move
        # past it) and a connection off the pool at frontier width, which is exactly the `idle in
        # transaction` that once blocked a live migration. Harvest and the transitions after
        # the run open a fresh one; nothing here needs to outlive the read.
        await session.commit()

        brief = run.Brief(spec=spec, gotchas=gotchas, resume=resume, owed_cut=owed_cut, fresh=fresh)
        result, settled = await run.run_agent(
            session, task, step, brief=brief, bench=bench, equipped=equipped, prepared=prepared
        )
        park = await classify.classify(
            session, task, result, settled=settled, inbox=equipped.inbox, spend=deps.spend
        )
        if park is not None:
            parked = await prepared.parked()  # a draft on paper moves to the Machine first
            return await _browser_park(frame, deps.browser, park, vouched=vouched)

        at = publish.Output(
            frame=acquired.frame, deps=deps, spec=spec, step=step, equipped=equipped, fact=engine_fact
        )
        narration, outline = await publish.publish_output(at, result, override=settled.override)
        definition = prepared.definition
        ending = harvest.Yield(
            narration=narration,
            outline=outline,
            publishes=definition.publishes,
            delivers_outcome=definition.delivers_outcome,
            profile=profile,
        )
        produced = await harvest.collect(
            session, task, step, spec, frame=acquired.frame, deps=deps, run=ending, fact=engine_fact
        )
        return Produced(narration, produced, outline)
    except (PolicyDenied, ModelIncapable):
        # Best-effort, and only for a browser step whose frame got far enough to mount a `deps`
        # (a failure before then has no browser to finalize). The recording is evidence of what
        # happened before the failure, not a deliverable - `working`, unconditionally, the same
        # role an ordinary failed attempt's artifacts would get if it had any to harvest. The
        # harvest rides its own SAVEPOINT: a flush error here must unwind before it is
        # suppressed, or the aborted transaction outlives this handler and poisons whatever
        # commits next - never the bare exception this handler is about to re-raise regardless.
        if profile == "browser" and not parked and deps is not None:
            await harvest.record_evidence(
                session, task, step, frame, deps, success=False, own_output=False, fact=engine_fact
            )
        raise
    except asyncio.CancelledError:
        # A cancel mid-run (a sibling's failure cancelling the wave, a worker shutdown): the
        # spec's recording covers "success, failure, cancel or steer". Best-effort and BOUNDED -
        # a cancelled attempt must not linger on evidence - and committed here, because nothing
        # downstream of a cancellation will commit it. Never swallows the cancellation.
        if profile == "browser" and not parked and deps is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(30):
                    await harvest.record_evidence(
                        session, task, step, frame, deps, success=False, own_output=False, fact=engine_fact
                    )
                    await session.commit()
        raise
    finally:
        if pulse is not None:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse
        # A park keeps its frame for the resume; anything else gives it back: a Machine goes idle,
        # a throwaway frame is destroyed. On its own short session, so a poisoned step session
        # cannot strand the hold. This session may still hold the task's row (the harvest's
        # events) - safe here, unlike `release_parked`: the Machine is running and held by this
        # attempt, so no sleep can be holding its row while it waits on ours.
        segment = None
        try:
            segment = await machines.release(acquired, parked=parked)
        except Exception:
            # Never mask the step's own outcome. A release that failed leaves the Machine held by this
            # attempt; the next attempt of this step takes it over, and the reconciler handles anything else.
            logfire.exception("machine release failed for step {step}", step=str(step.id))
        if segment is not None:
            # The warm time the release closed, billed HERE on the step's session - the
            # usage record takes the task's row, which this session may already hold - and
            # committed with the step's outcome. Best-effort in a SAVEPOINT: a poisoned session
            # loses this segment, never bills it twice (the watermark moved).
            await machines.bill(session, segment)


@dataclass(frozen=True)
class Delivery:
    """What `deliver` did: `delivered`; `skipped` (a stale turn, a settled task); `held` (paused
    since the workflow's last boundary read - it holds for the resume and asks again); or
    `reopened` - fence 2 re-drafted the sinks in `replaced`, whose output and narration a steer
    superseded."""

    verdict: Literal["delivered", "reopened", "held", "skipped"]
    replaced: tuple[uuid.UUID, ...] = ()


async def deliver(
    session: AsyncSession,
    task_id: uuid.UUID,
    summary: str,
    *,
    turn: int | None = None,
    reopen: bool = True,
) -> Delivery:
    """The summary is written **for a human who wasn't watching** - and it is *sent*
    to them, because a person who wasn't watching does not see an event on a stream.

    `turn` is the stale-writer guard: a recovered turn-N workflow re-running its
    deliver while turn N+1 is planning would find the new turn's steps "unfinished" and fail the
    task the user just continued. A delivery addressed to a superseded turn is a no-op ("skipped",
    as is a replay onto a settled task). A turn paused since the workflow's last boundary read
    is "held": the workflow holds for the resume and asks again.

    Fence 2: a turn holding an accepted steer nothing took does not deliver what the steer overtook - its
    sinks are re-drafted and this returns "reopened" with the sinks it replaced, and the workflow runs the
    frontier again. Never on an action or navigate turn, nor when a sink that missed the steer acted outside
    the task, nor with `reopen=False` (the sweeper, which runs no steps): the steer is rejected instead and
    the turn delivers.
    """

    # The row lock first, then everything under it (fence 2 included): a pause or a cancel that
    # committed before it is seen here, and one after it waits for this delivery to commit.
    task = await Task.held(session, task_id)
    if turn is not None and task.turn != turn:
        logfire.warn(
            "stale turn-{turn} workflow tried to deliver task {t} (now turn {now}); refused",
            turn=turn,
            t=str(task_id),
            now=task.turn,
        )
        return Delivery("skipped")
    if task.is_terminal():
        return Delivery("skipped")  # a replayed effect, or a cancel that landed first
    if task.state is TaskStatus.PAUSED:
        return Delivery("held")  # delivery is a boundary too: the workflow holds for the resume

    # **A task with an unfinished step does not deliver. It fails, loudly, and says which step.**
    #
    # The frontier loop stops when nothing is `ready` - which is *usually* because everything is
    # done, and would also be true if a promotion had been missed under the fan-out (see
    # `promote_dependents`). The two are indistinguishable from inside the loop, and the wrong one
    # produces a **silent partial delivery**: a task that reports success, hands the user two of its
    # three documents, and gives nobody any reason to look.
    #
    # An invariant nobody checks is an invariant that stops holding. This checks it.
    unfinished = [
        s
        for s in await PlanStep.for_task(session, task_id)
        if s.step_state not in (StepState.DONE, StepState.SKIPPED)
    ]
    if unfinished:
        if (again := await _reopened_already(session, task, unfinished)) is not None:
            return again
        raise PolicyDenied(
            "refusing to deliver: "
            + ", ".join(f"step '{s.title}' is {s.state}" for s in unfinished[:4])
            + " - the frontier stopped with work outstanding, which is a scheduling bug, not a result"
        )

    # **FENCE 2**, under the task row lock so no steer lands between this read and
    # the delivery: an accepted steer nothing took re-drafts the turn's sinks. With nothing to
    # re-draft (a turn from before steering, no done sink), work that must not run twice (an action
    # or navigate turn, R45; a sink that called a connector or drove the browser, R53) or no runner
    # to run it, the steer says the turn ended before it applied, to everyone, and the turn
    # delivers.
    if await Steer.unapplied(session, task.id, turn=task.turn):
        if reopen and task.turn_epoch > 0 and (replaced := await _redraft_sinks(session, task)):
            return Delivery("reopened", tuple(replaced))
        await steering.reject_unapplied(session, task, REJECT_TURN_ENDED)

    # **Delivery closes the book**: a still-open gate is moot the moment the answer
    # ships - expired ON the record (each emits `gate.resolved`, seq'd before `task.delivered`),
    # so no surface can ever render a question over a delivered task. Once
    # a recovery race left four gates open across a delivery, and the page walled the board
    # off behind an unanswerable dock.

    await gate_ops.expire_open(session, task)

    # **Delivery tolerates a stranded WAITING.** With every step settled and every
    # gate now closed, `waiting` is a leftover of a park whose un-flip was lost (a worker died
    # on a parked question - the finished, paid-for document could not
    # deliver because waiting → delivered is not a legal edge). The legal path exists - walk it.
    if task.state is TaskStatus.WAITING:
        await advance(session, task_id=task.id, to=TaskStatus.RUNNING)

    ended = await ending.of_turn(session, task)
    task.transition(TaskStatus.DELIVERED)
    await call_log.forget(session, task.id)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskDelivered(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            summary=summary,
            ending=ended,
        ),
    )
    if task.origin_routine_id is not None and task.turn == 1:
        # A routine RUN's settle speaks through the SILENCE RULE, not the generic ping:
        # the routine decides whether this delivery is news, and the failure
        # ladder resets. Same transaction - the hook is flush-only. Turn 1 IS the run; later
        # turns are the owner's own follow-ups in the same thread - they ping like any task
        # and never touch the ladder (a follow-up walking it auto-paused routines whose
        # scheduled runs were all green).

        await routine_ops.on_settled(session, task, failed=False, summary=summary)
    else:
        await inbox_ops.task_completed(session, task, summary, ended)
    return Delivery("delivered")


async def _reopened_already(
    session: AsyncSession, task: Task, unfinished: Sequence[PlanStep]
) -> Delivery | None:
    """A delivery run again after its reopen committed (an effect is at-least-once): every unfinished
    step is a never-run re-draft of this turn that fence 2's record names. It answers as the first
    run did, instead of refusing to deliver an unfinished step."""
    if not all(
        s.step_state is StepState.READY and s.attempt == 0 and s.turn_epoch == task.turn_epoch
        for s in unfinished
    ):
        return None
    waiting = {str(s.id) for s in unfinished}
    for edit in reversed(await PlanEditRow.of_kind(session, task.id, PlanEditKind.REPLAN.value)):
        redrafted = edit.payload.get("redrafted")
        if isinstance(redrafted, dict) and waiting <= {str(v) for v in redrafted.values()}:
            return Delivery("reopened", tuple(uuid.UUID(str(k)) for k in redrafted))
    return None


async def _redraft_sinks(session: AsyncSession, task: Task) -> list[uuid.UUID]:
    """Fence 2's re-draft: each sink of the turn that missed a steer of it becomes a
    NEW step - its spec with the steers told in the goal, its incoming edges, READY under the
    current epoch, last in the plan - and its output is superseded. A new row because a done
    step's workflow id is spent: a reopened row would replay its finished run and never run. The
    old row stays done and feeds its re-draft, so it is never a sink again. The record says why the
    steps appeared (`plan_edits{replan}` naming each sink's re-draft, `plan.edited{replan}`).
    Nothing is re-drafted when any such sink must not run again: the caller
    rejects the turn's steers instead. Returns the sinks it replaced; the caller holds the task row
    and commits."""
    missed: list[tuple[PlanStep, Sequence[SteerNote]]] = []
    for sink in await steering.turn_sinks(session, task):
        taken = await steering.notes(session, await steering.unseen(session, task, sink))
        if taken:
            missed.append((sink, taken))
    for sink, _ in missed:
        if not await steering.reruns(session, task, sink):
            return []
    edges = await PlanEdgeRow.for_task(session, task.id)
    ordinal = max((s.ordinal for s in await PlanStep.for_task(session, task.id)), default=-1)
    redrafted: dict[uuid.UUID, uuid.UUID] = {}
    told: dict[uuid.UUID, SteerNote] = {}
    for sink, taken in missed:
        told.update((n.command_id, n) for n in taken)
        output = [a for a in await Artifact.for_step(session, sink.id) if a.role != "superseded"]
        spec = StepSpec.model_validate(sink.spec)
        if spec.contract is None:
            raise IncompleteStep(sink.id, "contract")
        brief = rerun_text(taken, _superseded_paths(output))
        contract = spec.contract.model_copy(update={"goal": f"{spec.contract.goal}\n\n{brief}"})
        ordinal += 1
        redraft = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=ordinal,
            kind=sink.kind,
            state=StepState.READY.value,
            title=sink.title,
            spec=spec.model_copy(update={"contract": contract, "redrafts": sink.id}).model_dump(mode="json"),
            epoch=task.plan_epoch,
            turn_epoch=task.turn_epoch,
        )
        session.add(redraft)
        await session.flush()  # the row before the edges that point at it (`write_steps`' law)
        for edge in (e for e in edges if e.to_step_id == sink.id):
            session.add(
                PlanEdgeRow(
                    task_id=task.id, from_step_id=edge.from_step_id, to_step_id=redraft.id, kind=edge.kind
                )
            )
        session.add(
            PlanEdgeRow(task_id=task.id, from_step_id=sink.id, to_step_id=redraft.id, kind="depends_on")
        )
        ids = [a.id for a in output]
        await Artifact.supersede(session, ids)
        await steering.apply(session, task, sink, taken, SafePoint.RERUN, superseded=ids)
        await _step_event(session, task, redraft, StepState.READY, detail="a steer arrived before delivery")
        redrafted[sink.id] = redraft.id
    if redrafted:
        reason = replan_reason(sorted(told.values(), key=lambda n: n.epoch))
        payload = {"reason": reason, "redrafted": {str(k): str(v) for k, v in redrafted.items()}}
        session.add(
            PlanEditRow(task_id=task.id, kind=PlanEditKind.REPLAN.value, reason=reason, payload=payload)
        )
        now = utcnow()
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            PlanEdited(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=now,
                edit=PlanEditView(
                    kind=PlanEditKind.REPLAN,
                    reason=reason,
                    at=now,
                    added=[StepId(new) for new in redrafted.values()],
                ),
            ),
        )
    return list(redrafted)
