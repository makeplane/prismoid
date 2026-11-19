# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The door of `execute_step`: what a step's row says it needs before any rung is bought.

**Idempotent under replay, and this is what the kill test is for.** `execute_step` is a durable
step, so on crash-and-restart the workflow replays and any step that did NOT complete runs again. A
step found in `running` is therefore not an illegal transition - it is a step **whose worker died
inside it**. The state machine was right to refuse `running → running`; the recovery path simply
had to know what it was looking at.

`done` is different: that one already finished, and re-running it would mean a second sandbox and
a second bill. It is handled FIRST, as the twin guard.

**Resuming a step parked on `ask_user`** is the third case, and it is neither a recovery nor a
fresh start: the step is `running`, the task is `waiting`, and `resume_state` holds an answer. It
is the *same* attempt continuing - a mid-run question is not a retry.

The triage is TOTAL: every state a crash can persist has a defined re-entry.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import InvalidTransition, ModelIncapable, NotFound, PolicyDenied
from prismoid.operations import execution as engine
from prismoid.operations import longform as longform_ops
from prismoid.operations import machines
from prismoid.operations.execution import creator, giveback, parks, wake
from prismoid.operations.execution.device import on_device
from prismoid.operations.execution.errors import runnable_contract
from prismoid.operations.execution.handlers import recover
from prismoid.operations.execution.handlers.state import Injected, Ladder
from prismoid.operations.execution.mounts import upstream_artifacts
from prismoid.operations.execution.record import (
    BACKOFF,
    HANDBACKS,
    WOKE,
    handbacks,
    owed_gotchas,
    reissue,
    with_record,
)
from prismoid.operations.execution.taps import step_event
from prismoid.schemas.contract import Contract
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.guard import BUDGET_DECLINED, outcome_narration
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.park import PARK, ParkKind, parked_kind
from prismoid.schemas.plan import StepSpec, StepState, TaskStatus

# The resume keys a gate's wake writes: the same run continues with what the person said.
WAKES = ("answer", "approval_action", "connect_result", "pause_resumed", "budget_answer")


@dataclass
class Reentry:
    """How the step re-enters the ladder, read from its row."""

    recovered: bool = False  # the previous worker died inside it
    reenters: bool = False  # died mid-run: the attempt goes on, its call log replays what it bought
    resume: dict[str, object] | None = None  # a park's wake: the same run continues
    at_review: bool = False  # a VERIFYING re-entry resumes at the review, never re-produces
    carried: list[str] = field(default_factory=list)  # why the attempt before the park failed
    failures: list[FailureKind] = field(default_factory=list)  # and the kinds of those deaths
    budget_consented: bool = False  # the owner bought the next attempt at the budget gate
    budget_stop: Outcome | None = None  # a mid-run budget park whose cap was kept


async def triage(
    session: AsyncSession, task_id: uuid.UUID, step_id: uuid.UUID, *, seat: bool, handed_back: int | None
) -> str | Ladder:
    """The step's final word if the door settles it, or the ladder it walks. Commits what each
    re-entry writes; leaves no row lock held."""
    task = await session.get(Task, task_id)
    step = await session.get(PlanStep, step_id)
    if task is None or step is None:
        raise NotFound("no such task or step")
    if task.is_terminal():
        return await _ended(session, task, step)

    spec = StepSpec.model_validate(step.spec)  # JSONB → a typed value. The boundary.
    contract = runnable_contract(spec, step.id)
    # The steers this step takes reach its contract per attempt, not here (`steers.steered`).
    if step.step_state in (StepState.DONE, StepState.SKIPPED):
        return await _settled_twin(session, task, step)
    record = handbacks(step)
    if handed_back is not None and handed_back < len(record):
        return _replayed(step, record, handed_back)
    # Only once a replay has caught up: the cancel sends wakes, and a send at a replayed position
    # takes the function id the journal holds for that recv or sleep (ADR-087 §2).
    if await creator.refuse_if_gone(session, task):
        return await _ended(session, task, step)
    if (reparked := await recover.recover_parked_gate(session, task, step, record)) is not None:
        return reparked

    entered = step.step_state  # the state a recovery found: only a step that was running parks
    found = await _reentry(session, task, step, spec)
    if isinstance(found, str):
        return found
    seated = seat or engine.is_seat(spec)
    if found.recovered and entered in (StepState.RUNNING, StepState.VERIFYING) and on_device(task):
        # **No blind re-run on the device.** The step was running commands in the
        # user's own folder when the worker or the helper went away: a fresh attempt could redo a
        # half-finished edit or a push. It parks, waiting for a person - before any rung is bought.
        logfire.warn("step {step_id} recovered mid-flight on the device - parking", step_id=str(step.id))
        return await parks.park_device_away(session, task, step, why="interrupted", seat=seated)
    if found.recovered:
        await recover.recovered_mid_flight(
            session, task, step, same_attempt=found.at_review or found.reenters
        )
    return await _ladder(session, task, step, spec, found, seat=seat, seated=seated, contract=contract)


async def _ended(session: AsyncSession, task: Task, step: PlanStep) -> str:
    """A cancelled/failed/delivered task's queued or parked steps eject at the door instead of
    advancing a terminal task (a parked seat once woke after its task was cancelled and retried
    `cancelled → waiting` forever). Nothing to run, nothing to raise - and a park this step still
    holds is given back (a restart while parked, then a cancel before recovery, lands here and
    never reaches the workflow's cancel exit)."""
    title = step.title
    await giveback.give_back_a_dead_park(session, task, step)
    return f"(the task ended while step {title!r} was in flight)"


async def _settled_twin(session: AsyncSession, task: Task, step: PlanStep) -> str:
    """**A settled step reconciles BEFORE any gate or resume ceremony.** Post-recovery, the engine
    can resurrect the SAME inline child twice - the executor-recovery copy and the replaying
    parent's re-invocation - and the loser of that race arrives here after the winner already
    finished. It can even arrive carrying `resume_state.answer`: the loser's own parked question,
    TTL-resolved, writes onto the SHARED row (once the twin resumed "with the user's answer"
    onto a DONE outline, and its `done → retrying` InvalidTransition failed a task whose outline
    had SUCCEEDED). So this guard outranks the resume branch: the row's state machine is the
    arbiter, the loser wipes its stale crumbs, and the parent gets a normal return instead of a
    dead task."""
    logfire.warn(
        "step {step_id} re-entered while {state} - twin execution reconciled as a no-op",
        step_id=str(step.id),
        state=step.state,
    )
    if step.resume_state is not None:
        step.resume_state = None
        await session.commit()
    await giveback.give_back_a_dead_park(session, task, step)  # the loser's park, if it still holds one
    return "(already settled - a recovered twin execution reconciled itself)"


def _replayed(step: PlanStep, record: list[str], handed_back: int) -> str:
    """**A replay re-issues the workflow's hand-backs in the order it took them.** The workflow
    replays `run_step` by its journal: a park's recv and a pace's sleep each hand back their own
    recorded result, but only if the workflow calls them again in the same order. Read from the
    row's record, not the step's state: a run that parked and was then paced is `ready` with its
    park behind it, and re-pacing first breaks the journal."""
    logfire.warn(
        "step {s}: the workflow replays hand-back {n} of {m}",
        s=str(step.id),
        n=handed_back + 1,
        m=len(record),
    )
    return reissue(record[handed_back])


async def _reentry(session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec) -> Reentry | str:
    """How the step re-enters, by what its row holds: a budget gate's answer, a device resumed, a
    park's wake, or else the state a crash left it in."""
    state = step.resume_state
    if state and "budget_action" in state:
        return await _budget_answered(session, task, step)
    if state and "device_resumed" in state:
        carried, resume, at_review = await recover.resumed_on_device(session, task, step)
        return Reentry(resume=resume, at_review=at_review, carried=carried)
    if state and any(key in state for key in WAKES):
        return await _woken(session, task, step, spec)
    return _by_state(step)


async def _budget_answered(session: AsyncSession, task: Task, step: PlanStep) -> Reentry | str:
    """Parked on the budget gate before its next attempt (the rung top): the owner's answer decides
    whether that attempt is bought. Not a resume - the attempt that spent the cap is over, so the
    next one starts fresh, told what went wrong (`carried`)."""
    state = step.resume_state or {}
    consented = state["budget_action"] == "accept"
    carried, failures = owed_gotchas(state)
    step.resume_state = with_record(step, None)
    if not consented:
        await giveback.end_at_the_budget_gate(session, task)
        return f"(the task stopped at its budget before step {step.title!r} tried again)"
    if task.state is TaskStatus.WAITING:
        await engine.advance(session, task_id=task.id, to=TaskStatus.RUNNING)  # back off the gate
    await session.commit()
    return Reentry(carried=carried, failures=failures, budget_consented=True)


async def _woken(session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec) -> Reentry | str:
    """A park's wake: the resume is the run's from here on; the row keeps the workflow's record and
    the park's, so a worker that dies before the run logs the answer hydrates it again (`recover`)."""
    state = step.resume_state or {}
    resume = {k: v for k, v in state.items() if k not in (HANDBACKS, WOKE)}
    step.resume_state = with_record(step, {PARK: state[PARK]} if PARK in state else None)
    if resume.get("cancelled"):
        return await _cancelled(session, task, step, spec, resume)
    budget_stop = None
    if resume.get("budget_answer") == "decline":
        budget_stop = await _cap_kept(session, task, step, spec, resume)
    if task.state is TaskStatus.WAITING:
        await engine.advance(session, task_id=task.id, to=TaskStatus.RUNNING)  # back off the gate
    # A fresh RUNNING anchor on the record (the progress tree's timing law): without it the step's
    # running→done delta silently includes the whole parked interval. No transition - step_event
    # only appends the event.
    await step_event(session, task, step, StepState.RUNNING, detail=wake.resumed_detail(resume))
    # **Commit the un-park before the attempt runs, or it deadlocks.** `advance` took the task's row
    # lock via `next_seq`; the resume skips the rung's usual pre-attempt commit (this is not a fresh
    # attempt), so without this the lock is held across the whole resumed agent run and the first
    # usage emit - its own transaction - blocks on it forever.
    await session.commit()
    if budget_stop is not None:
        # nothing to continue: the injected ending settles the step
        return Reentry(budget_stop=budget_stop)
    return Reentry(resume=resume)


async def _cancelled(
    session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec, resume: dict[str, object]
) -> str:
    """The human said stop. The park is still up - bind it only to give it back - and fail. A
    browser step publishes its recording first (the spec: "... cancel or steer")."""
    profile = machines.seat_profile(task, spec)
    if profile == "browser":
        await giveback.cancelled_browser_evidence(session, task, step)
        # Committed here: the PolicyDenied below unwinds this session, and the gate row (resolved
        # `cancel`) re-hydrates a crash-recovered replay regardless.
        with contextlib.suppress(Exception):
            await session.commit()
    title = step.title  # read first: the discard's commit may roll back and expire the rows
    await giveback.discard_parked_frame(session, task, step, profile=profile)
    if parked_kind(resume) is ParkKind.BUDGET:
        # A mid-run budget park cancelled, its wake read from the gate row:
        # it ends the task, exactly as the between-attempts budget gate's cancel does.
        await giveback.read_again(session, task)
        await giveback.end_at_the_budget_gate(session, task)
        return f"(the task stopped at its budget while step {title!r} was parked)"
    raise PolicyDenied("the user cancelled the question this step was waiting on")


async def _cap_kept(
    session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec, resume: dict[str, object]
) -> Outcome:
    """The cap was kept: nothing more is bought. The recording is published,
    the park given back, and the step settles `partial` - no model call."""
    profile = machines.seat_profile(task, spec)
    if profile == "browser":
        await giveback.cancelled_browser_evidence(session, task, step)
    await giveback.discard_parked_frame(session, task, step, profile=profile)
    await giveback.read_again(session, task, step)  # read below: the state, the event, the settle
    ended = resume.get("budget_ended")
    if isinstance(ended, dict):
        return Outcome.model_validate(ended)
    return Outcome(status="partial", summary=BUDGET_DECLINED, blocked_by="budget")


def _by_state(step: PlanStep) -> Reentry:
    """No resume on the row: the state a crash left the step in says how it goes on."""
    if step.step_state is StepState.RUNNING:
        return Reentry(recovered=True, reenters=True)
    if step.step_state is StepState.READY:
        # A paced step whose wait is over: the next rung is a fresh run (the column's law). A
        # replay before the wait ran out re-paced at the top, from the record. A provider's
        # backoff kept what that run is owed: the gotchas (a guarded run's pickup rides the record).
        carried, failures = owed_gotchas((step.resume_state or {}).get(BACKOFF) or {})
        step.resume_state = with_record(step, None)
        step.transition(StepState.RUNNING)
        return Reentry(carried=carried, failures=failures)
    if step.step_state is StepState.RETRYING:
        # Killed between the RETRYING commit and its fresh attempt (the ladder walks retrying →
        # ready → running in one breath, but the record commits between rungs under load). Finish
        # the walk it began - an infra rung, not a model rung.
        step.transition(StepState.READY)
        step.transition(StepState.RUNNING)
        return Reentry(recovered=True)
    if step.step_state is StepState.VERIFYING:
        # Killed mid-review (fleet verify or the QA gate). The PRODUCE is already durable -
        # artifacts harvested and committed before VERIFYING was entered - so re-running the
        # attempt would re-buy work the user already paid for. Resume AT the review instead; the
        # injected outcome feeds the settled artifacts straight to the outcome handlers.
        return Reentry(recovered=True, at_review=True)
    if step.step_state is StepState.ESCALATED:
        # The failure already happened and is on the record; a recovered workflow replays it
        # faithfully instead of dying on an undefined edge (the twin guard's sibling law).
        raise ModelIncapable(f"step {step.id} already escalated - its diagnosis is on the stream")
    # PENDING is the one truly illegal entry: dispatch only enqueues READY ids, so being here
    # means the graph promoted wrongly - loud is correct.
    raise InvalidTransition(f"step {step.id} is {step.state}; it cannot be executed")


async def _ladder(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    found: Reentry,
    *,
    seat: bool,
    seated: bool,
    contract: Contract,
) -> Ladder:
    """The ladder the step walks, with what it needs before any attempt spends a cent."""
    # What the steps this one depends on published. Fetched once, outside the ladder: a retry is a
    # fresh attempt at the same step, not a fresh plan - its sources do not change between rungs.
    upstream = await upstream_artifacts(session, task, step)

    # THE ASSEMBLY FLOOR, before any attempt spends a cent: below 80% surviving sections the
    # document is not the deliverable that was asked for - fail honestly now, with the roll call,
    # instead of assembling a hollow book with a confident cover.
    if spec.lane_role == "assembly":
        await longform_ops.assembly_ready_guard(session, task, step)

    # **The injected outcome:** a VERIFYING re-entry owes only the review - its artifacts are
    # already durable rows. If the harvest somehow left nothing (it cannot: VERIFYING is only
    # entered with `produced`), degrade honestly to a fresh attempt. A kept cap injects its
    # `partial` ending.
    stop = found.budget_stop
    injected: Injected | None = (outcome_narration(stop), [], stop) if stop is not None else None
    if found.at_review:
        injected = await recover.review_owed(session, step) or injected
    return Ladder(
        session=session,
        task=task,
        step=step,
        spec=spec,
        contract=contract,
        seat=seat,
        seated=seated,
        upstream=upstream,
        # **The ladder's gotchas.** Append-only: every rung is kept, because *why it took three
        # attempts* is the most interesting thing about a step that took three attempts.
        gotchas=found.carried,
        failures=found.failures,
        resume=found.resume,
        injected=injected,
        budget_consented=found.budget_consented,
        reentered=found.reenters,
    )
