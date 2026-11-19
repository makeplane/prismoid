# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routines - the run ledger and the routine's ears on the record.

The receipt every fire and non-fire writes (`journal`, one append-only `routine_runs` row per
occurrence), the clock helpers the fire gate and the verbs share (`advance_past` moves
`next_fire_at` only by compare-and-advance, and only for clock fires), the failure ladder and its
auto-pause, the settle hook deliver/fail call inside their own transaction, and the event matcher
the sweep runs. `routines.py` is the public face; `routine_fire.py` is the gate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import orjson
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.routine import Routine, RoutineRun
from prismoid.db.models.task import Task
from prismoid.operations import inbox as inbox_ops
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.routines import (
    EVENT_MENU,
    FireResult,
    MatchedFire,
    Notify,
    PausedReason,
    RoutineStatus,
    RunDecision,
    RunSource,
    ScheduleSpec,
    jitter_offset_s,
    next_fire_at,
    occurrence_for_event,
)


def jitter(routine_id: uuid.UUID) -> int:
    return jitter_offset_s(routine_id, settings.routines.jitter_max_s)


def spec_of(routine: Routine) -> ScheduleSpec:
    return ScheduleSpec.model_validate(routine.trigger)


def advance_past(
    routine: Routine, spec: ScheduleSpec | None, fire_instant: dt.datetime | None, now: dt.datetime
) -> None:
    """CAS the clock past a decided occurrence - CLOCK fires only. `fire_instant is None` means
    a token/API/event fire: it never moves the schedule (the compare-and-advance law; `advance`
    refuses None too, so the guard holds at both altitudes)."""
    if spec is not None and fire_instant is not None:
        routine.advance(
            occurrence_fire_at=fire_instant,
            to=next_fire_at(spec, after=now, jitter_s=jitter(routine.id)),
        )


async def auto_pause(
    session: AsyncSession,
    routine: Routine,
    *,
    reason: PausedReason,
    note: str,
    task: Task | None = None,
    title: str | None = None,
) -> None:
    """The ONE auto-pause transition (budget breach, the failure ladder): status + reason +
    note + the inbox knock, in lockstep - previously spelled out longhand at each site, and the
    copies had already drifted. An auto-pause always notifies, whatever the
    routine's notify policy: this is the routine's own life, not a run's."""
    routine.status = RoutineStatus.PAUSED.value
    routine.paused_reason = reason.value
    routine.pause_note = note
    await inbox_ops.routine_paused(
        session,
        actor_id=routine.owner_actor_id,
        routine_id=routine.id,
        routine_name=routine.name,
        why=note,
        task=task,
        title=title,
    )


async def count_failure(
    session: AsyncSession, routine: Routine, *, task: Task | None = None, last_error: str = ""
) -> None:
    """One failure ladder for mint refusals AND settled failures (they share the threshold by
    design): bump, and past the threshold, pause with the knock."""
    routine.consecutive_failures += 1
    if (
        routine.consecutive_failures >= routine.pause_after_failures
        and routine.status == RoutineStatus.ACTIVE
    ):
        note = f"paused after {routine.consecutive_failures} consecutive failed runs"
        if last_error:
            note += f" - last: {last_error[:200]}"
        await auto_pause(session, routine, reason=PausedReason.FAILURES, note=note, task=task)


async def fire_failed(session: AsyncSession, routine: Routine, *, why: str) -> None:
    """A fire that could not become a task (a refused mint, a crash): it walks the failure ladder
    like a failed run, and the FIRST failure of a streak knocks the owner - unless the ladder just
    paused the routine (that knock already says it) or the owner chose `never`. A streak ends
    where a run's does: a settled success resets the count and says "recovered"."""
    was_active = routine.status == RoutineStatus.ACTIVE
    await count_failure(session, routine, last_error=why)
    paused_now = was_active and routine.status != RoutineStatus.ACTIVE
    if routine.consecutive_failures != 1 or paused_now or routine.notify == Notify.NEVER:
        return
    await inbox_ops.routine_fire_failed(
        session,
        actor_id=routine.owner_actor_id,
        routine_id=routine.id,
        routine_name=routine.name,
        why=why[:400],
    )


async def journal(
    session: AsyncSession,
    routine: Routine,
    *,
    occurrence: str,
    source: RunSource,
    decision: RunDecision,
    task_id: uuid.UUID | None = None,
    scheduled_for: dt.datetime | None = None,
    detail: str = "",
    chain_depth: int = 0,
) -> FireResult:
    """The receipt: one append-only row per occurrence, whatever was decided. The unique index
    absorbs every replay - a raced twin reads back the original and returns it."""
    routine_id = routine.id  # captured BEFORE any rollback: reading an expired ORM row after
    # one needs a sync refresh, which raises MissingGreenlet on the async session - the exact
    # replay path this fallback exists for 500'd on it
    run = RoutineRun(
        routine_id=routine_id,
        occurrence=occurrence,
        source=source.value,
        decision=decision.value,
        task_id=task_id,
        scheduled_for=scheduled_for,
        chain_depth=chain_depth,
        detail=detail[:400],
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await RoutineRun.by_occurrence(session, routine_id, occurrence)
        if existing is not None:
            return result_of(existing)
        raise
    return FireResult(run_id=run.id, decision=decision, task_id=task_id, detail=detail)


def result_of(run: RoutineRun) -> FireResult:
    return FireResult(
        run_id=run.id,
        decision=RunDecision(run.decision),
        task_id=run.task_id,
        detail=run.detail,
    )


# ── the settle hook - called by deliver/fail inside THEIR transaction ─────────────────────────


async def on_settled(session: AsyncSession, task: Task, *, failed: bool, summary: str) -> None:
    """A routine task settled: walk the failure ladder and apply the silence rule (`always`, `findings`,
    `failure`, `never`). Flush-only; a missing routine degrades to silence."""
    if task.origin_routine_id is None:
        return
    routine = await Routine.lock(session, task.origin_routine_id)
    if routine is None:
        return
    notify = routine.notify

    if failed:
        await count_failure(session, routine, task=task)
        # An escalation already carries this failure's diagnosis - same news twice is spam.
        if notify in (Notify.ALWAYS, Notify.FAILURE) and not await InboxItem.exists_for_task(
            session, task.id, InboxKind.ESCALATION.value
        ):
            await inbox_ops.routine_run(
                session,
                task,
                routine_id=routine.id,
                routine_name=routine.name,
                title=f"{routine.name}: run failed",
                body=summary or "The run failed. Open the task for the record.",
            )
        return

    recovered_after = routine.consecutive_failures
    routine.consecutive_failures = 0

    if recovered_after > 0 and notify != Notify.NEVER:
        await inbox_ops.routine_run(
            session,
            task,
            routine_id=routine.id,
            routine_name=routine.name,
            title=f"{routine.name}: recovered after {recovered_after} failed run(s)",
            body=summary,
        )
        return  # the recovery IS this settle's news - not a second "delivered" ping

    if notify == Notify.ALWAYS:
        await inbox_ops.routine_run(
            session,
            task,
            routine_id=routine.id,
            routine_name=routine.name,
            title=f"{routine.name}: ran",
            body=summary,
        )
    elif notify == Notify.FINDINGS and await _found(session, task):
        await inbox_ops.routine_run(
            session,
            task,
            routine_id=routine.id,
            routine_name=routine.name,
            title=f"{routine.name}: delivered",
            body=summary,
        )


async def _found(session: AsyncSession, task: Task) -> bool:
    """Did the run find something: its last step's frame says the Lead found news, or it published a
    deliverable. Writes nothing."""
    last = await TaskEvent.latest_of_type(session, task.id, "step.state")
    if isinstance(last, StepStateChanged) and last.findings:
        return True
    return any(a.role == "deliverable" for a in await Artifact.latest_for_task(session, task.id))


# ── the event matcher ────────────────────────────────────────────────────────────────────

# The menu-word → wire-type mapping lives ON the menu itself (schemas EVENT_MENU.wire) - one
# table for validation AND matching, so a kind added to the menu can never validate at create
# and then KeyError this sweep for everyone (the split-twin defect).
# "task.failed" is the user's word for an OUTCOME; the wire spells it task.status +
# payload.status=failed. The matcher speaks both.


def _event_matches(kind: str, event_type: str, payload: dict[str, Any], verdict: str | None) -> bool:
    if EVENT_MENU[kind].wire != event_type:
        return False
    if kind == "task.failed":
        return payload.get("status") == "failed"
    if kind == "verification.signed" and verdict is not None:
        return payload.get("verdict") == verdict
    if kind == "artifact.produced":
        return payload.get("role", "deliverable") == "deliverable"
    return True


def _event_snapshot(event_type: str, payload: dict[str, Any], task_id: uuid.UUID, seq: int) -> bytes:
    """The trigger.json the minted task reads: the event's FACTS, compact, and nothing that is
    not already the record. It rides the same untrusted lane as an API payload - instruction
    never comes in through this door."""
    keep = {
        k: (v[:2000] if isinstance(v, str) else v)
        for k, v in payload.items()
        if k in ("summary", "reason", "status", "verdict", "filename", "diagnosis", "at", "type")
    }
    return orjson.dumps({"event": event_type, "task_id": str(task_id), "seq": seq, **keep})


async def match_window(session: AsyncSession, *, since: dt.datetime, until: dt.datetime) -> list[MatchedFire]:
    """One scan of the record against every active event routine. Scope IS the
    routine's own scoping: a Project routine hears its Project's tasks, a personal routine its
    owner's - the Copilot write-access rule in Plane's role grammar, by construction.

    **Self-suppression is silent and structural** (the GITHUB_TOKEN rule): a routine's own
    minted task always emits the very receipts it listens for - journaling that non-fire per
    run would be noise wearing a doctrine's clothes. CROSS-routine chains are legitimate,
    carried (depth = trigger task's + 1), and refused LOUDLY at the gate past the cap."""
    listeners = await Routine.active_event_routines(session)
    if not listeners:
        return []
    wire_types = sorted({EVENT_MENU[k].wire for r in listeners for k in (r.trigger.get("kinds") or ())})
    rows = await TaskEvent.window_for_matcher(session, wire_types, since, until)
    fires: list[MatchedFire] = []
    for event, creator_id, project_id, origin_routine_id, origin_depth in rows:
        for routine in listeners:
            if routine.id == origin_routine_id:
                continue  # its own run's receipt - the silent structural rule above
            if not _in_scope(routine, event, creator_id, project_id):
                continue
            spec_verdict = routine.trigger.get("verdict")
            if not any(
                _event_matches(kind, event.type, event.payload or {}, spec_verdict)
                for kind in (routine.trigger.get("kinds") or ())
            ):
                continue
            fires.append(
                MatchedFire(
                    routine_id=routine.id,
                    occurrence=occurrence_for_event(event.task_id, event.seq),
                    payload=_event_snapshot(event.type, event.payload or {}, event.task_id, event.seq),
                    chain_depth=origin_depth + 1,
                )
            )
    return fires


def _in_scope(
    routine: Routine, event: TaskEvent, creator_id: uuid.UUID, project_id: uuid.UUID | None
) -> bool:
    """Does `routine` hear this event's task? Writes nothing."""
    if event.workspace_id != routine.workspace_id:
        return False  # the matcher scans every workspace (platform mode): a routine hears ITS workspace only
    if routine.project_id is not None:
        return routine.project_id == project_id  # a Project routine hears its Project
    # a personal routine hears its owner's PERSONAL tasks - project tasks are the project's
    return creator_id == routine.owner_actor_id and project_id is None
