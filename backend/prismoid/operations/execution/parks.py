# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Opening a park: the gate a stopped run waits on, one function per park kind.

Each `park_on_*` opens its gate and saves the park on the step row (its `ParkRecord`; the run and
the calls its resume answers are on the step's call log) in one transaction; `open_gate` hands the
gate to the workflow, and gives the frame back if the gate cannot open.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from collections.abc import Awaitable
from dataclasses import replace

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.connector import Connector
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import device_gates, gates, machines, questions
from prismoid.operations import execution as engine
from prismoid.operations.connector_attach import service_words
from prismoid.operations.execution import classify, giveback
from prismoid.operations.execution.deferrals import (
    ApprovalDeferral,
    ConnectDeferral,
    ControlDeferral,
    Deferral,
    PauseDeferral,
)
from prismoid.operations.execution.deferrals import park_record as _park_record
from prismoid.operations.execution.device import Cut as _Cut
from prismoid.operations.execution.device import left_running as _left_running
from prismoid.operations.execution.device import on_device as _on_device
from prismoid.operations.execution.device import park_device_away as _park_away
from prismoid.operations.execution.record import ASKING, BACKOFF
from prismoid.operations.execution.record import hand_back as _hand_back
from prismoid.operations.execution.record import with_record as _with_record
from prismoid.schemas.connector_catalog import CATALOG, entry_for, suggest
from prismoid.schemas.device_away import DeviceAwayWhy
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.ids import StepId
from prismoid.schemas.park import ParkKind
from prismoid.schemas.plan import (
    StepSpec,
    TaskStatus,
)


async def open_gate(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    opening: Awaitable[uuid.UUID],
    *,
    hand_back: bool = True,
) -> str:
    """Open the park's gate and commit it: the ladder's answer to a step that stopped to ask, to
    connect or for consent. When the gate does not open, none of the park lands (the rollback, as
    before 1b) and the park is given back - `_attempt` already released the
    Machine as parked, and nothing will ever resume it - then the failure goes on up.

    `hand_back=False` commits the gate without recording the hand-back (`HANDBACKS`): a takeover
    park whose race may still settle in place (`wake.settle_lease_race`) records it only when it really
    hands the park to its workflow, so a replay never re-issues a park the workflow never recv'd."""
    task_id, step_id, attempt = task.id, step.id, step.attempt  # a rollback expires the rows
    profile = machines.seat_profile(task, spec)
    try:
        gate_id = await opening
        outcome = _hand_back(step, f"park:{gate_id}") if hand_back else f"{ASKING}{gate_id}"
        await session.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        await giveback.give_park_back(task_id, step_id, attempt, profile=profile)
        raise
    return outcome


async def park_on_question(
    session: AsyncSession, task: Task, step: PlanStep, deferral: Deferral
) -> uuid.UUID:
    """A step asked. Open a gate, save the park, move the task to `waiting`.

    The park goes into `step.resume_state` and the run is on the step's call log, so the resume
    finishes the same run; the question goes into a gate through the *same* `gates` machinery the intake round uses, so a
    mid-run question renders, resolves, and times out exactly like an intake one - one kind of gate,
    one CLI, one sweep. The task moves to `waiting`, which is the honest status: it is not running,
    and it is not failed; it is waiting on a human - unless it is paused: then it stays
    `paused`. An answer still resumes the run: it spends tokens until its next tool dispatch, where
    it parks on the pause, and the task's resume lets it go on from there.
    """

    asked = deferral.asked or questions.mid_run(deferral.questions, now=utcnow())
    gate = await gates.open_question(session, task, StepId(step.id), asked)
    step.resume_state = _with_record(step, _park_record(deferral, ParkKind.QUESTION, gate.id))
    await engine.advance(session, task_id=task.id, to=TaskStatus.WAITING)
    logfire.info(
        "step {step} parked on {n} question(s) - gate {gate}",
        step=str(step.id),
        n=len(asked.questions),
        gate=str(gate.id),
    )
    return gate.id


async def park_on_connect(
    session: AsyncSession, task: Task, step: PlanStep, deferral: ConnectDeferral
) -> uuid.UUID:
    """The step wants a service. Resolve WHAT to offer - a catalog entry by slug, or
    the creator's own not-yet-attached row - open the CONNECT gate, save the park, park.
    An unmatched service still opens the gate with an empty slug: the block renders honestly
    and points at the connectors page rather than the request vanishing into prose."""

    want = deferral.service.strip().lower()
    slug, display = "", deferral.service.strip() or "a service"
    entry = entry_for(want)
    row = None
    if entry is None:
        rows = await Connector.resolve_refs(session, task.creator_actor_id, [want])
        row = rows.get(want)
    if entry is None and row is None and want:
        # The spoken-name fallback: the agent says "google calendar", the catalog
        # slug is `google_calendar` - resolve through the SAME word-boundary grammar the
        # routing door speaks, so a service the directory demonstrably offers never opens a
        # slug-less block. Owner rows outrank the catalog (attachable on the spot).
        owned = await Connector.for_owner(session, task.creator_actor_id, limit=200)
        row_hits = suggest(want, [(str(r.id), tuple(sorted(service_words(r)))) for r in owned])
        if row_hits:
            row = next(r for r in owned if str(r.id) == row_hits[0][0])
        else:
            cat_hits = suggest(
                want,
                [(e.slug, (*e.keywords, e.slug.replace("_", " "), e.name.lower())) for e in CATALOG],
            )
            if cat_hits:
                entry = entry_for(cat_hits[0][0])
    if entry is not None:
        slug, display = entry.slug, entry.name
    elif row is not None:
        slug, display = row.slug, row.name

    gate = await gates.open_connect(
        session, task, StepId(step.id), slug=slug, display=display, why=deferral.why
    )
    step.resume_state = _with_record(
        step,
        {
            **_park_record(deferral, ParkKind.CONNECT, gate.id),
            "connect": {"slug": slug, "display": display, "service": deferral.service},
        },
    )
    await engine.advance(session, task_id=task.id, to=TaskStatus.WAITING)  # a paused task stays paused
    logfire.info(
        "step {step} parked on a connect request ({service}) - gate {gate}",
        step=str(step.id),
        service=deferral.service,
        gate=str(gate.id),
    )
    return gate.id


async def park_on_approval(
    session: AsyncSession, task: Task, step: PlanStep, deferral: ApprovalDeferral
) -> uuid.UUID:
    """Side-effectful connector (or browser) calls want consent. Open ONE
    approve gate for the whole batch, save the park, park the task at `waiting` (a paused
    task stays `paused`) - the ask park's spine: the resume reads every pending call (and its inspect
    `fingerprint`, when the browser toolset attached one: it rebuilds
    `ToolApproved(override_args={..., "expect": fingerprint})` from it rather than re-inspecting)
    off the replayed run, and answers all of them from the gate's one resolution."""

    gate = await gates.open_approval(
        session,
        task,
        StepId(step.id),
        deferral.items,
        source=deferral.source,
        host=deferral.host,
        scopes=deferral.scopes,
        asks_because=deferral.whys,
    )
    step.resume_state = _with_record(step, _park_record(deferral, ParkKind.APPROVAL, gate.id))
    await engine.advance(session, task_id=task.id, to=TaskStatus.WAITING)  # a paused task stays paused
    logfire.info(
        "step {step} parked on {n} approval(s) - gate {gate}",
        step=str(step.id),
        n=len(deferral.items),
        gate=str(gate.id),
    )
    return gate.id


async def park_on_pause(
    session: AsyncSession, task: Task, step: PlanStep, deferral: PauseDeferral
) -> uuid.UUID:
    """The task is paused: open its PAUSE gate and save the park - no questions,
    no clock. The task stays `paused` (it is not waiting on an answer) and the Machine stays
    `parked`; the task's resume lifts the gate and the same run goes on, every deferred call told
    it did not run. The caller read the task's status under its row lock, still held here."""

    gate = await gates.open_pause(session, task, StepId(step.id))
    step.resume_state = _with_record(step, _park_record(deferral, ParkKind.PAUSE, gate.id))
    logfire.info(
        "step {step} parked on a pause ({n} call(s) deferred) - gate {gate}",
        step=str(step.id),
        n=len(deferral.calls),
        gate=str(gate.id),
    )
    return gate.id


async def park_on_control(
    session: AsyncSession, task: Task, step: PlanStep, deferral: ControlDeferral
) -> uuid.UUID:
    """A person is at the browser. Open the CONTROL gate, save the park, park the
    task at `waiting` (a paused task stays `paused`) - the ask park's spine with the
    control channel in `resume_state`: `control` names the gate's source and holder, and
    `hydrate_resume` turns the gate's resolution into the parked call's text. Like every park it
    keeps the workflow's hand-back record (`_with_record`) and its `ParkRecord`. Nothing here resolves the gate: the
    lease's ending answers it (`gates.resolve_control`), an editor's Skip or the TTL declines it."""

    cloud_wait = settings.computer_use.control_wait_s
    gate = await gates.open_control(
        session,
        task,
        StepId(step.id),
        source=deferral.source,
        reason=deferral.reason,
        holder=deferral.holder,
        host=deferral.host,
        lease_since=deferral.since,
    )
    step.resume_state = _with_record(
        step,
        {
            **_park_record(deferral, ParkKind.CONTROL, gate.id),
            "control": {
                "source": deferral.source,
                "holder": deferral.holder,
                # a device's gate waits the away limit (`device_gates.control_wait_s`): its timeout says so
                **({"wait_s": wait} if (wait := device_gates.control_wait_s(task)) != cloud_wait else {}),
            },
        },
    )
    await engine.advance(session, task_id=task.id, to=TaskStatus.WAITING)  # a paused task stays paused
    logfire.info(
        "step {step} parked on a person at the browser ({source}) - gate {gate}",
        step=str(step.id),
        source=deferral.source,
        gate=str(gate.id),
    )
    return gate.id


async def pause_lifted(session: AsyncSession, task: Task, deferral: PauseDeferral) -> dict[str, object]:
    """A pause park whose pause already lifted continues in place: the resume it would have woken
    with, naming who paused and who resumed off the record."""
    resumed = await classify.status_moved(session, task, TaskStatus.RUNNING)
    paused = (
        await classify.status_moved(session, task, TaskStatus.PAUSED, before=resumed.seq) if resumed else None
    )
    who = classify.mover(paused) or deferral.paused_by
    return {
        **_park_record(replace(deferral, paused_by=who), ParkKind.PAUSE, None),
        "pause_resumed": True,
        "resumed_by": classify.mover(resumed) or "",
        "cancelled": False,
    }


async def park_device_away(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    why: DeviceAwayWhy,
    seat: bool = False,
    cut: _Cut | None = None,
    resume: dict[str, object] | None = None,
    gotchas: list[str] | None = None,
) -> str:
    """The step waits for its device (`device.park_device_away`); a task that ended
    meanwhile gives the step's park back instead, like every door that finds it ended."""
    title = step.title  # read first: the give-back's commit may expire the rows
    parked = await _park_away(
        session, task, step, why=why, seat=seat, cut=cut, resume=resume, gotchas=gotchas
    )
    if parked is None:
        await giveback.give_back_a_dead_park(session, task, step)
        return f"(the task ended while step {title!r} was in flight)"
    return parked


async def park_cut_short(
    session: AsyncSession, task: Task, step: PlanStep, *, seat: bool, gotchas: list[str]
) -> str:
    """Park an attempt cut short on the device (`_cut_short`) as `interrupted`, the journal left to
    the attempt a person resumes (`look`): the command may still be finishing there, or the helper
    is not there to ask. What the run spent is committed first, as the failed-attempt arm does."""
    with contextlib.suppress(Exception):
        await session.commit()
    return await park_device_away(
        session, task, step, why="interrupted", seat=seat, cut=_Cut(look=True), gotchas=gotchas
    )


async def park_beside_a_command(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    *,
    seat: bool,
    gotchas: list[str],
) -> str | None:
    """**No fresh attempt beside a command still running**: the attempt that ran it may
    have ended while the helper runs on (`_left_running`), and a second one would act in the person's
    folder, unaware. Nothing is bought; a person resumes. Only a Machine's frame keeps a journal: a
    seat's throwaway one holds none of the person's, and a cloud task never asks. (Out of
    `execute_step` only to keep that function analysable.)"""
    if seat or engine.is_seat(spec) or not _on_device(task):
        return None
    running = await _left_running(session, task, step)
    if running is None:
        return None
    return await park_device_away(session, task, step, why="interrupted", cut=running, gotchas=gotchas)


def back_off(step: PlanStep, *, seconds: float, gotchas: list[str], failures: list[FailureKind]) -> str:
    """The step waits `seconds` on its workflow's durable clock before its next attempt: its
    provider just failed, and asking it again at once is how a blip becomes an outage. A pace is a
    hand-back like any other (recorded, so a replay re-issues it). The wait ends the ladder's walk,
    so what the next attempt is owed rides the row, as a budget park keeps it: the gotchas and their
    kinds (a guarded run's pickup rides the record, `record.OWED_RUN`). The caller commits; the step
    is left `ready`."""
    step.resume_state = _with_record(step, {BACKOFF: {"gotchas": list(gotchas), "failures": list(failures)}})
    deadline = engine.utcnow() + dt.timedelta(seconds=seconds)
    return _hand_back(step, f"pace:{deadline.isoformat()}")
