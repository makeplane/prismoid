# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routines end to end against the real database (ADR-048): the standing object's verbs,
and the fire gate's idempotent layers - the failure matrix's first rows as tests.

Needs the stack: `just up && just migrate`. The dispatcher stays the NullDispatcher - a fire
MINTS (row + event + ledger receipt); running the minted task is the worker's job and not
under test here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.routine import Routine, RoutineRun
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, NotFound
from prismoid.operations import gates
from prismoid.operations import routines as ops
from prismoid.schemas.events import GateResolved, TaskStatusChanged
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateAction, TaskStatus
from prismoid.schemas.routines import MatchedFire, RoutineStatus, RunDecision, RunSource, occurrence_for_clock
from tests.support.asks import question_set
from tests.support.cast import a_workspace, bound_scope, member

pytestmark = pytest.mark.integration


async def _human(session: AsyncSession, name: str = "Rou Tine") -> Actor:
    return (await member(session, name))[0]


async def _routine(session: AsyncSession, actor: Actor, **kw: object) -> Routine:
    defaults: dict[str, object] = {
        "name": f"probe-{uuid.uuid4().hex[:8]}",
        "instruction": "post the standup summary to the thread",
        "schedule": {"cron": "0 9 * * 1-5", "every_s": None, "at": None, "timezone": "UTC"},
    }
    defaults.update(kw)
    return await ops.create_routine(session, actor, **defaults)  # pyright: ignore[reportArgumentType]


def _past(minutes: int) -> dt.datetime:
    return utcnow() - dt.timedelta(minutes=minutes)


async def _tasks_of(session: AsyncSession, routine_id: uuid.UUID) -> int:
    q = select(func.count()).select_from(Task).where(Task.origin_routine_id == routine_id)
    return int(await session.scalar(q) or 0)


async def test_create_computes_next_fire_and_the_defaults_hold() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        assert routine.status == RoutineStatus.ACTIVE
        assert routine.next_fire_at is not None and routine.next_fire_at > utcnow()
        assert float(routine.budget_usd_per_run) == settings.routines.default_budget_usd
        assert routine.overlap == "skip" and routine.catch_up == "skip" and routine.notify == "findings"

        # The scope law: a stranger sees nothing - NotFound, never Forbidden.
        stranger = await _human(session, "Stranger")
        with pytest.raises(NotFound):
            await Routine.get_for(session, routine.id, stranger)

        # A personal name is unique per owner (the bots precedent).
        with pytest.raises(Conflict):
            await _routine(session, me, name=routine.name)


async def test_fire_mints_an_ordinary_task_with_provenance_and_the_receipt() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        fire_at = _past(1)
        routine.next_fire_at = fire_at  # the sweep's world, arranged by hand
        await session.commit()
        occ = occurrence_for_clock(fire_at)

        result = await ops.execute_fire(session, routine.id, occurrence=occ, source=RunSource.CLOCK)
        assert result.decision == RunDecision.MINTED and result.task_id is not None

        task = await session.get(Task, result.task_id)
        assert task is not None
        assert task.origin_routine_id == routine.id and task.origin_depth == 0
        assert task.clarify == "never", "nobody is there to answer an intake round"
        assert task.budget == {"usd": float(routine.budget_usd_per_run)}, "unattended work is capped"
        assert task.goal == routine.instruction
        assert (task.title or "").startswith(routine.name)

        run = await RoutineRun.by_occurrence(session, routine.id, occ)
        assert run is not None and run.decision == "minted" and run.task_id == task.id
        assert run.scheduled_for == fire_at, "the intended instant is the honest lateness stamp"

        await session.refresh(routine)
        assert routine.runs_minted == 1 and routine.last_fired_at is not None
        assert routine.next_fire_at is not None and routine.next_fire_at > utcnow(), "advanced"


async def test_the_same_occurrence_never_mints_twice() -> None:
    """Layer 2 + 3: a replayed fire (crash resume, racing sweeps) answers with the original
    receipt - one task, one ledger row."""
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        fire_at = _past(1)
        routine.next_fire_at = fire_at
        await session.commit()
        occ = occurrence_for_clock(fire_at)

        first = await ops.execute_fire(session, routine.id, occurrence=occ, source=RunSource.CLOCK)
        second = await ops.execute_fire(session, routine.id, occurrence=occ, source=RunSource.CLOCK)
        assert first.decision == RunDecision.MINTED
        assert second.run_id == first.run_id and second.task_id == first.task_id
        assert await _tasks_of(session, routine.id) == 1


async def test_the_edit_race_journals_superseded_and_mints_nothing() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        stale = occurrence_for_clock(_past(10))  # an occurrence the row no longer names

        result = await ops.execute_fire(session, routine.id, occurrence=stale, source=RunSource.CLOCK)
        assert result.decision == RunDecision.SUPERSEDED and result.task_id is None
        assert await _tasks_of(session, routine.id) == 0
        run = await RoutineRun.by_occurrence(session, routine.id, stale)
        assert run is not None, "every non-fire is a journaled row"


async def test_catch_up_skip_journals_the_miss_and_advances() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(
            session, me, schedule={"cron": "0 * * * *", "every_s": None, "at": None, "timezone": "UTC"}
        )
        fire_at = (utcnow() - dt.timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
        routine.next_fire_at = fire_at
        await session.commit()

        result = await ops.execute_fire(
            session, routine.id, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
        )
        assert result.decision == RunDecision.SKIPPED_MISSED
        assert "missed" in result.detail and await _tasks_of(session, routine.id) == 0
        await session.refresh(routine)
        advanced = routine.next_fire_at
        assert advanced is not None and advanced > utcnow()


async def test_catch_up_once_mints_one_coalesced_make_up() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(
            session, me, schedule={"cron": "0 * * * *", "every_s": None, "at": None, "timezone": "UTC"}
        )
        routine.catch_up = "once"
        fire_at = (utcnow() - dt.timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
        routine.next_fire_at = fire_at
        await session.commit()

        result = await ops.execute_fire(
            session, routine.id, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
        )
        assert result.decision == RunDecision.MINTED
        assert "coalesced" in result.detail, "one make-up for the latest miss - never replay-all"
        assert await _tasks_of(session, routine.id) == 1
        run = await RoutineRun.by_occurrence(session, routine.id, occurrence_for_clock(fire_at))
        assert run is not None and run.scheduled_for is not None
        assert run.scheduled_for > fire_at, "the make-up represents the MOST RECENT miss"


async def test_pause_resume_and_the_paused_race_mints_nothing() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        rid = routine.id  # held as a value: the raced fire below ROLLS BACK, expiring the object
        fire_at = _past(1)
        routine.next_fire_at = fire_at
        await session.commit()

        await ops.pause_routine(session, me, rid, note="vacation")
        assert routine.paused_reason == "user" and routine.pause_note == "vacation"
        assert all(row[0] != rid for row in await Routine.due(session)), "paused rows never reach the sweep"

        # A fire enqueued BEFORE the pause lands on the current row and stands down.
        raced = await ops.execute_fire(
            session, rid, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
        )
        assert raced.decision == RunDecision.SUPERSEDED and await _tasks_of(session, rid) == 0

        resumed = await ops.resume_routine(session, me, rid)
        assert resumed.status == RoutineStatus.ACTIVE
        assert resumed.next_fire_at is not None and resumed.next_fire_at > utcnow(), (
            "resume recomputes from NOW - never replays the stale past"
        )


async def test_manual_fire_works_even_paused_and_respects_the_ledger() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        await ops.pause_routine(session, me, routine.id)

        result = await ops.fire(session, me, routine.id)
        assert result.decision == RunDecision.MINTED, "the play button is user-caused and always present"
        assert await _tasks_of(session, routine.id) == 1
        pairs = await ops.runs_for(session, me, routine.id, limit=10, cursor=None)
        assert pairs and pairs[0][0].source == "manual"


async def test_a_one_shot_fires_once_and_retires() -> None:
    async with session_scope() as session:
        me = await _human(session)
        at = utcnow() + dt.timedelta(hours=1)
        routine = await _routine(
            session, me, schedule={"cron": None, "every_s": None, "at": at.isoformat(), "timezone": "UTC"}
        )
        # Time passes: the instant arrives (arranged by hand; within the grace window).
        fire_at = _past(1)
        routine.next_fire_at = fire_at
        routine.trigger = {**routine.trigger, "at": fire_at.isoformat()}
        await session.commit()

        result = await ops.execute_fire(
            session, routine.id, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
        )
        assert result.decision == RunDecision.MINTED
        await session.refresh(routine)
        assert routine.status == RoutineStatus.RETIRED, 'fired once - the UI says "Ran"'
        assert routine.next_fire_at is None


async def test_seasons_retire_honestly_at_the_gate() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.max_runs = 0  # the season is already complete
        fire_at = _past(1)
        routine.next_fire_at = fire_at
        await session.commit()

        result = await ops.execute_fire(
            session, routine.id, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
        )
        assert result.decision == RunDecision.SKIPPED_SEASON
        await session.refresh(routine)
        assert routine.status == RoutineStatus.RETIRED
        run = await RoutineRun.by_occurrence(session, routine.id, occurrence_for_clock(fire_at))
        assert run is not None and "retired" in run.detail, "the retirement is a journaled row"


async def test_the_active_cap_is_a_409_at_create(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as session:
        me = await _human(session)
        monkeypatch.setattr(settings.routines, "max_active_per_user", 1)
        await _routine(session, me)
        with pytest.raises(Conflict, match="retire one"):
            await _routine(session, me)


async def test_due_reads_postgres_time_and_only_active_schedule_rows() -> None:
    async with session_scope() as session:
        me = await _human(session)
        due_routine = await _routine(session, me)
        due_routine.next_fire_at = _past(2)
        future_routine = await _routine(session, me, name=f"future-{uuid.uuid4().hex[:6]}")
        await session.commit()

        due = await Routine.due(session)
        ids = {row[0] for row in due}
        assert due_routine.id in ids and future_routine.id not in ids


# ── W2: the policy gate - the failure matrix as tests ─────────────────────────────────────────


_FIRE_SEQ = iter(range(1, 10_000))


async def _clock_fire(session: AsyncSession, routine: Routine) -> object:
    """Arrange the sweep's world by hand and fire: set next_fire_at, fire that occurrence.

    Each call takes a DISTINCT past instant, and every instant stays well inside the
    miss-grace window: occurrences truncate to whole seconds (same-second fires would collide
    into one occurrence and the replay layer would - correctly - answer with the first
    receipt), and an instant older than the grace window would - correctly - journal a miss.
    The gate is right on both; the test must hand it neither."""
    fire_at = utcnow() - dt.timedelta(seconds=30 + 2 * next(_FIRE_SEQ))
    routine.next_fire_at = fire_at
    await session.commit()
    return await ops.execute_fire(
        session, routine.id, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
    )


async def test_throttle_floor_journals_and_advances() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        first = await _clock_fire(session, routine)
        assert first.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]

        await session.refresh(routine)
        second = await _clock_fire(session, routine)  # within min_interval_s of the mint
        assert second.decision == RunDecision.SKIPPED_THROTTLE  # pyright: ignore[reportAttributeAccessIssue]
        await session.refresh(routine)
        assert routine.next_fire_at is not None and routine.next_fire_at > utcnow(), "advanced"
        assert await _tasks_of(session, routine.id) == 1


async def test_daily_cap_moves_the_clock_to_tomorrow() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.daily_fire_cap = 1
        routine.min_interval_s = 0  # isolate the cap from the floor
        first = await _clock_fire(session, routine)
        assert first.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]

        await session.refresh(routine)
        second = await _clock_fire(session, routine)
        assert second.decision == RunDecision.SKIPPED_CAP  # pyright: ignore[reportAttributeAccessIssue]
        assert "midnight" in second.detail  # pyright: ignore[reportAttributeAccessIssue]
        await session.refresh(routine)
        from prismoid.schemas.routines import next_day_start

        assert routine.next_fire_at is not None
        assert routine.next_fire_at >= next_day_start("UTC", utcnow()), "silent until the day turns"


async def test_monthly_cap_pauses_and_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    from prismoid.db.models.inbox import InboxItem, InboxKind
    from prismoid.operations import usage as usage_ops

    async def five_dollars(session: object, routine_id: object, *, since: object) -> Decimal:
        return Decimal("5")

    monkeypatch.setattr(usage_ops, "routine_month_to_date", five_dollars)
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.monthly_cap_usd = Decimal("1")
        routine.min_interval_s = 0
        result = await _clock_fire(session, routine)
        assert result.decision == RunDecision.SKIPPED_BUDGET  # pyright: ignore[reportAttributeAccessIssue]
        await session.refresh(routine)
        assert routine.status == RoutineStatus.PAUSED and routine.paused_reason == "budget"
        items = await InboxItem.list_for(session, me)
        assert any(i.kind == InboxKind.ROUTINE_PAUSED.value for i in items), "the pause knocks"


async def test_overlap_skip_stands_down_while_a_run_lives() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        first = await _clock_fire(session, routine)
        assert first.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]
        # the minted task is `created` - live; nothing runs it (NullDispatcher)

        second = await _clock_fire(session, routine)
        assert second.decision == RunDecision.SKIPPED_OVERLAP  # pyright: ignore[reportAttributeAccessIssue]
        assert "still going" in second.detail  # pyright: ignore[reportAttributeAccessIssue]
        assert await _tasks_of(session, routine.id) == 1


async def test_overlap_queue_buffers_exactly_one_then_mints() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.overlap = "queue"
        routine.min_interval_s = 0
        first = await _clock_fire(session, routine)
        assert first.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]
        live_task = await Task.live_for_routine(session, routine.id)
        assert live_task is not None

        await session.refresh(routine)
        fire2_at = utcnow() - dt.timedelta(seconds=95)  # distinct instants: occurrences are whole seconds
        routine.next_fire_at = fire2_at
        await session.commit()
        occ2 = occurrence_for_clock(fire2_at)
        buffered = await ops.execute_fire(session, routine.id, occurrence=occ2, source=RunSource.CLOCK)
        assert buffered.decision == RunDecision.BUFFERED
        await session.refresh(routine)
        assert routine.buffered_occurrence == occ2
        advanced_to = routine.next_fire_at
        assert advanced_to is None or advanced_to > utcnow(), (
            "the park advanced the clock - the sweep stops re-offering"
        )

        # A second due fire finds the one buffer slot taken.
        fire3_at = utcnow() - dt.timedelta(seconds=105)
        routine.next_fire_at = fire3_at
        await session.commit()
        third = await ops.execute_fire(
            session, routine.id, occurrence=occurrence_for_clock(fire3_at), source=RunSource.CLOCK
        )
        assert third.decision == RunDecision.SKIPPED_OVERLAP
        assert "buffer full" in third.detail

        # The previous run settles; the parked fire re-enters and has the floor.
        live_task.status = "delivered"  # direct write: the machine's path is not under test
        await session.commit()
        minted = await ops.execute_fire(session, routine.id, occurrence=occ2, source=RunSource.CLOCK)
        assert minted.decision == RunDecision.MINTED
        assert "buffering" in minted.detail
        await session.refresh(routine)
        assert routine.buffered_occurrence is None, "the receipt clears the slot"
        assert await _tasks_of(session, routine.id) == 2


async def test_overlap_queue_expires_its_park(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.overlap = "queue"
        routine.min_interval_s = 0
        first = await _clock_fire(session, routine)
        assert first.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]

        old = _past(10)
        occ = occurrence_for_clock(old)
        routine.buffered_occurrence = occ  # a park taken 10 minutes ago, live run still going
        await session.commit()
        monkeypatch.setattr(settings.routines, "queue_buffer_max_s", 60)
        expired = await ops.execute_fire(session, routine.id, occurrence=occ, source=RunSource.CLOCK)
        assert expired.decision == RunDecision.SKIPPED_OVERLAP
        assert "expired" in expired.detail
        await session.refresh(routine)
        assert routine.buffered_occurrence is None


async def test_overlap_replace_cancels_then_mints() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.overlap = "replace"
        routine.min_interval_s = 0
        first = await _clock_fire(session, routine)
        first_task = await session.get(Task, first.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert first_task is not None

        await session.refresh(routine)
        second = await _clock_fire(session, routine)
        assert second.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]
        assert "replaced" in second.detail  # pyright: ignore[reportAttributeAccessIssue]
        await session.refresh(first_task)
        assert first_task.status == "cancelled", "the one cancel machinery, not a fork of it"
        assert await _tasks_of(session, routine.id) == 2


async def test_overlap_replace_cancel_writes_no_human_actor() -> None:
    """The REPLACE cancel is the routine's policy, not the owner's hand - the events
    it writes (the status flip AND the gate it closes) carry no actor."""
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.overlap = "replace"
        routine.min_interval_s = 0
        first = await _clock_fire(session, routine)
        live = await session.get(Task, first.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert live is not None
        live_id = live.id
        step = PlanStep(task_id=live_id, ordinal=0, title="post the summary")
        session.add(step)
        await session.flush()
        await gates.open_question(session, live, StepId(step.id), question_set(("Which channel?", [])))
        await session.commit()

        await session.refresh(routine)
        second = await _clock_fire(session, routine)
        assert second.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]

        events = await TaskEvent.replay(session, live_id, since=0)
        cancelled = [
            e for e in events if isinstance(e, TaskStatusChanged) and e.status is TaskStatus.CANCELLED
        ]
        closed = [e for e in events if isinstance(e, GateResolved) and e.action is GateAction.CANCEL]
        assert len(cancelled) == 1 and len(closed) == 1
        assert [e.actor for e in (*cancelled, *closed)] == [None, None]


async def test_mint_errors_walk_the_ladder_to_auto_pause() -> None:
    from prismoid.db.models.inbox import InboxItem, InboxKind

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.template = {**routine.template, "bot": "ghost-bot-that-never-was"}
        routine.min_interval_s = 0
        routine.pause_after_failures = 3
        await session.commit()

        decisions = []
        for _ in range(3):
            result = await _clock_fire(session, routine)
            decisions.append(result.decision)  # pyright: ignore[reportAttributeAccessIssue]
            await session.refresh(routine)
        assert decisions == [RunDecision.ERROR] * 3
        assert routine.status == RoutineStatus.PAUSED and routine.paused_reason == "failures"
        assert routine.consecutive_failures == 3
        await session.refresh(me)  # the mint-refusal ROLLBACK expired every object we hold
        items = await InboxItem.list_for(session, me)
        assert any(i.kind == InboxKind.ROUTINE_PAUSED.value for i in items)


async def test_settle_hook_counts_resets_and_speaks_recovery() -> None:
    from prismoid.db.models.inbox import InboxItem, InboxKind

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        minted = await _clock_fire(session, routine)
        task = await session.get(Task, minted.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert task is not None

        await ops.on_settled(session, task, failed=True, summary="it broke")
        await ops.on_settled(session, task, failed=True, summary="it broke again")
        await session.commit()
        await session.refresh(routine)
        assert routine.consecutive_failures == 2

        await ops.on_settled(session, task, failed=False, summary="all well now")
        await session.commit()
        await session.refresh(routine)
        assert routine.consecutive_failures == 0, "success resets the ladder"
        items = await InboxItem.list_for(session, me)
        recoveries = [i for i in items if i.kind == InboxKind.ROUTINE_RUN.value and "recovered" in i.title]
        assert recoveries, "the recovery speaks once (Datadog's transition taxonomy)"


async def test_notify_policy_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from prismoid.db.models.artifact import Artifact
    from prismoid.db.models.inbox import InboxItem, InboxKind

    async def no_artifacts(session: object, task_id: object) -> list[object]:
        return []

    async def one_deliverable(session: object, task_id: object) -> list[object]:
        return [SimpleNamespace(role="deliverable")]

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        minted = await _clock_fire(session, routine)
        task = await session.get(Task, minted.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert task is not None

        async def count_runs() -> int:
            items = await InboxItem.list_for(session, me)
            return sum(1 for i in items if i.kind == InboxKind.ROUTINE_RUN.value)

        # findings (default) + nothing published → silence
        monkeypatch.setattr(Artifact, "latest_for_task", staticmethod(no_artifacts))
        await ops.on_settled(session, task, failed=False, summary="quiet run")
        await session.commit()
        assert await count_runs() == 0, "the silence rule holds"

        # findings + a deliverable → speaks
        monkeypatch.setattr(Artifact, "latest_for_task", staticmethod(one_deliverable))
        await ops.on_settled(session, task, failed=False, summary="made a thing")
        await session.commit()
        assert await count_runs() == 1

        # never → silent even on failure; the ledger still counts it
        routine.notify = "never"
        await session.commit()
        await ops.on_settled(session, task, failed=True, summary="broke silently")
        await session.commit()
        assert await count_runs() == 1
        await session.refresh(routine)
        assert routine.consecutive_failures == 1

        # failure → failures speak (no escalation item exists for this task)
        routine.notify = "failure"
        await session.commit()
        await ops.on_settled(session, task, failed=True, summary="broke loudly")
        await session.commit()
        items = await InboxItem.list_for(session, me)
        assert any(i.kind == InboxKind.ROUTINE_RUN.value and "failed" in i.title for i in items), (
            "failure notify speaks"
        )


async def test_hold_posture_mints_held_and_knocks() -> None:
    from prismoid.db.models.inbox import InboxItem, InboxKind

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.posture = "hold"
        routine.min_interval_s = 0
        result = await _clock_fire(session, routine)
        assert result.decision == RunDecision.HELD  # pyright: ignore[reportAttributeAccessIssue]
        task = await session.get(Task, result.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert task is not None and task.held_at is not None, "born held - the fork lane"
        items = await InboxItem.list_for(session, me)
        assert any(i.kind == InboxKind.ROUTINE_HELD.value for i in items), "the knock"


async def test_create_refuses_intervals_under_the_floor() -> None:
    from prismoid.errors import ValidationFailed

    async with session_scope() as session:
        me = await _human(session)
        with pytest.raises(ValidationFailed, match="floor"):
            await _routine(
                session,
                me,
                schedule={"cron": None, "every_s": 60, "at": None, "timezone": "UTC"},
            )


# ── W3: the inbound fire door ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_burst_window() -> None:
    """The door's burst gate is an in-process window; a suite's own earlier fires must not 429
    a later test (the auth limiter's fixture, one lane over)."""
    ops._fire_limiter._hits.clear()  # pyright: ignore[reportPrivateUsage]


async def test_fire_token_lifecycle_opens_and_closes_the_door() -> None:
    from prismoid.errors import Unauthorized

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        await session.commit()

        raw = await ops.mint_fire_token(session, me, routine.id)
        assert raw.startswith("rt_") and len(raw) > 30
        assert routine.fire_token_hash is not None and raw not in routine.fire_token_hash

        result = await ops.fire_via_token(session, routine.id, raw)
        assert result.decision == RunDecision.MINTED
        pairs = await ops.runs_for(session, me, routine.id, limit=5, cursor=None)
        assert pairs[0][0].source == "api", "the token door is the api source"

        with pytest.raises(Unauthorized):
            await ops.fire_via_token(session, routine.id, "rt_not-the-token")

        rotated = await ops.mint_fire_token(session, me, routine.id)
        with pytest.raises(Unauthorized):
            await ops.fire_via_token(session, routine.id, raw)  # rotation supersedes
        await ops.revoke_fire_token(session, me, routine.id)
        with pytest.raises(Unauthorized):
            await ops.fire_via_token(session, routine.id, rotated)  # revocation closes


async def test_fire_payload_rides_the_untrusted_lane() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        await session.commit()
        raw = await ops.mint_fire_token(session, me, routine.id)

        result = await ops.fire_via_token(session, routine.id, raw, payload=b'{"alert": "disk full on db-3"}')
        assert result.decision == RunDecision.MINTED
        task = await session.get(Task, result.task_id)
        assert task is not None
        assert "UNTRUSTED" in task.goal and "/task/inputs/trigger.json" in task.goal, (
            "the payload law fences the data (the Claude fire-payload rule)"
        )
        assert task.goal.startswith(routine.instruction), "the instruction stays first, verbatim"
        assert [i["filename"] for i in task.inputs] == ["trigger.json"]


async def test_idempotency_key_replays_the_original_run() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        await session.commit()
        raw = await ops.mint_fire_token(session, me, routine.id)

        first = await ops.fire_via_token(session, routine.id, raw, idempotency_key="deploy-77")
        replay = await ops.fire_via_token(session, routine.id, raw, idempotency_key="deploy-77")
        assert first.decision == RunDecision.MINTED
        assert replay.run_id == first.run_id and replay.task_id == first.task_id
        assert await _tasks_of(session, routine.id) == 1, "a replayed webhook is ONE run"


async def test_token_fire_respects_pause_and_the_burst_gate() -> None:
    from prismoid.errors import Conflict as ConflictErr
    from prismoid.errors import RateLimited

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        raw = await ops.mint_fire_token(session, me, routine.id)
        await ops.pause_routine(session, me, routine.id)
        with pytest.raises(ConflictErr, match="paused"):
            await ops.fire_via_token(session, routine.id, raw)

        # The burst gate sits BEFORE any row read - a looping caller buys 429s, not journal rows.
        import contextlib

        with pytest.raises(RateLimited):
            for _ in range(20):
                with contextlib.suppress(ConflictErr):  # paused - the window still counts knocks
                    await ops.fire_via_token(session, routine.id, raw)


async def test_authed_fire_with_payload_pays_the_api_lane() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        await session.commit()

        result = await ops.fire(session, me, routine.id, payload=b'{"n": 1}')
        assert result.decision == RunDecision.MINTED
        pairs = await ops.runs_for(session, me, routine.id, limit=5, cursor=None)
        assert pairs[0][0].source == "api", (
            "credentials + payload = a machine calling: the api source, the full policy stack"
        )


# ── W4: the event lane ────────────────────────────────────────────────────────────────────────


async def _event_routine(session: AsyncSession, actor: Actor, **kw: object) -> Routine:
    defaults: dict[str, object] = {
        "name": f"listener-{uuid.uuid4().hex[:8]}",
        "instruction": "note the settled task in one sentence",
        "on_events": ["task.delivered"],
        "min_interval_s": 0,
    }
    defaults.pop("min_interval_s")  # not a create param - set after
    defaults.update(kw)
    routine = await ops.create_routine(session, actor, **defaults)  # pyright: ignore[reportArgumentType]
    routine.min_interval_s = 0
    await session.commit()
    return routine


async def _deliver_event(session: AsyncSession, actor: Actor, task: Task, summary: str = "done") -> int:
    """Append a real TaskDelivered frame to the record - the matcher reads the record, so the
    test writes the record."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.operations.tasks import actor_ref
    from prismoid.schemas.events import TaskDelivered
    from prismoid.schemas.ids import ProjectId, TaskId

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskDelivered(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            summary=summary,
        ),
    )
    await session.commit()
    return seq


async def _window(session: AsyncSession) -> list[MatchedFire]:
    return await ops.match_window(
        session, since=utcnow() - dt.timedelta(minutes=10), until=utcnow() + dt.timedelta(seconds=1)
    )


async def test_event_routine_creates_with_hold_default_and_menu_validation() -> None:
    from prismoid.errors import ValidationFailed

    async with session_scope() as session:
        me = await _human(session)
        routine = await _event_routine(session, me)
        assert routine.trigger_kind == "event" and routine.next_fire_at is None
        assert routine.posture == "hold", "an ambient trigger confirms before acting (Apple's rule)"
        assert routine.trigger["kinds"] == ["task.delivered"]

        with pytest.raises(ValidationFailed, match="menu"):
            await _event_routine(session, me, on_events=["task.renamed"])
        with pytest.raises(ValidationFailed, match="exactly one When"):
            await ops.create_routine(session, me, name="x", instruction="y")


async def test_matcher_fires_a_project_listener_and_the_fenced_snapshot_reaches_the_task() -> None:
    from prismoid.operations import tasks as tasks_ops

    async with session_scope() as session:
        me = await _human(session)
        from prismoid.operations import projects as project_ops

        project = (await project_ops.create(session, me, name=f"ops-{uuid.uuid4().hex[:6]}")).project
        routine = await _event_routine(session, me, project_id=project.id)

        watched = await tasks_ops.create_task(session, me, goal="the watched work", project_id=project.id)
        seq = await _deliver_event(session, me, watched, summary="shipped the memo")

        fires = await _window(session)
        mine = [f for f in fires if f.routine_id == routine.id]
        assert len(mine) == 1
        fire = mine[0]
        assert fire.occurrence == f"ev:{watched.id}:{seq}"
        assert fire.chain_depth == 1

        result = await ops.execute_fire(
            session,
            routine.id,
            occurrence=fire.occurrence,
            source=RunSource.EVENT,
            payload=fire.payload,
            chain_depth=fire.chain_depth,
        )
        assert result.decision == RunDecision.HELD, "the hold default holds"
        minted = await session.get(Task, result.task_id)
        assert minted is not None and minted.origin_depth == 1
        assert [i["filename"] for i in minted.inputs] == ["trigger.json"]
        assert "UNTRUSTED" in minted.goal and "(event fire)" in minted.goal


async def test_matcher_scopes_personal_and_project_apart() -> None:
    from prismoid.operations import tasks as tasks_ops

    async with session_scope() as session:
        me = await _human(session)
        other = await _human(session, "Other")
        personal_listener = await _event_routine(session, me)

        mine_personal = await tasks_ops.create_task(session, me, goal="my personal work")
        await _deliver_event(session, me, mine_personal)
        theirs = await tasks_ops.create_task(session, other, goal="their work")
        await _deliver_event(session, other, theirs)

        fires = await _window(session)
        mine = [f for f in fires if f.routine_id == personal_listener.id]
        assert len(mine) == 1, "a personal listener hears its owner's personal tasks, only"


async def test_matcher_never_crosses_workspaces_for_a_person_in_two() -> None:
    """The matcher scans every workspace at once (platform mode). One person in two workspaces, a personal
    listener in each: a personal task event in workspace A fires A's listener and NEVER B's - B's
    minted task would otherwise carry A's summary into B."""
    from prismoid.db.models import tenant
    from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
    from prismoid.operations import tasks as tasks_ops

    async with session_scope() as session:
        person, workspace_a = await member(session, "Two Workspaces")
        _founder, workspace_b = await a_workspace(session, "B")
        await WorkspaceMember.add(session, workspace_b.id, person.id, WorkspaceRole.MEMBER)
        tenant.bind(session, workspace_b.id, WorkspaceRole.MEMBER.value)
        in_b = await _event_routine(session, person)
        tenant.bind(session, workspace_a.id, WorkspaceRole.OWNER.value)
        in_a = await _event_routine(session, person)

        work_in_a = await tasks_ops.create_task(session, person, goal="workspace A private work")
        await _deliver_event(session, person, work_in_a, summary="workspace A's secret summary")

        fires = await _window(session)
        assert [f for f in fires if f.routine_id == in_a.id], "the same-workspace listener still fires"
        assert not [f for f in fires if f.routine_id == in_b.id], (
            "a workspace-A event never fires the same person's workspace-B routine"
        )
        assert in_b.workspace_id == workspace_b.id and in_a.workspace_id == workspace_a.id


async def test_a_routines_own_receipt_never_retriggers_it() -> None:
    async with session_scope() as session:
        me = await _human(session)
        routine = await _event_routine(session, me)
        # its own minted run settles...
        minted = await ops.fire(session, me, routine.id)
        own_task = await session.get(Task, minted.task_id)
        assert own_task is not None
        await _deliver_event(session, me, own_task, summary="the run's own delivery")

        fires = await _window(session)
        assert not [f for f in fires if f.routine_id == routine.id], (
            "the GITHUB_TOKEN rule: a routine's own run cannot restart it"
        )


async def test_chain_depth_caps_loudly_at_the_gate() -> None:
    from prismoid.operations import tasks as tasks_ops

    async with session_scope() as session:
        me = await _human(session)
        routine = await _event_routine(session, me)
        deep = await tasks_ops.create_task(session, me, goal="deep chain work")
        deep.origin_depth = 3  # arranged: three routine hops already behind it
        await session.commit()
        seq = await _deliver_event(session, me, deep)

        fires = await _window(session)
        mine = [f for f in fires if f.routine_id == routine.id]
        assert mine and mine[0].chain_depth == 4
        result = await ops.execute_fire(
            session,
            routine.id,
            occurrence=f"ev:{deep.id}:{seq}",
            source=RunSource.EVENT,
            chain_depth=4,
        )
        assert result.decision == RunDecision.SUPPRESSED_LOOP
        assert "cap" in result.detail and await _tasks_of(session, routine.id) == 0
        run = await RoutineRun.by_occurrence(session, routine.id, f"ev:{deep.id}:{seq}")
        assert run is not None, "unlike Exchange, the drop is a visible ledger row"


async def test_failed_menu_word_maps_to_the_wire_and_filters() -> None:
    from prismoid.db.models.event import TaskEvent
    from prismoid.operations import tasks as tasks_ops
    from prismoid.operations.tasks import actor_ref
    from prismoid.schemas.events import TaskStatusChanged
    from prismoid.schemas.ids import TaskId
    from prismoid.schemas.plan import TaskStatus

    async with session_scope() as session:
        me = await _human(session)
        routine = await _event_routine(session, me, on_events=["task.failed"])
        broke = await tasks_ops.create_task(session, me, goal="doomed work")
        for status in (TaskStatus.PAUSED, TaskStatus.FAILED):
            seq = await broke.next_seq(session)
            await TaskEvent.append(
                session,
                TaskStatusChanged(
                    seq=seq,
                    task_id=TaskId(broke.id),
                    project_id=None,
                    at=utcnow(),
                    actor=actor_ref(me),
                    status=status,
                    reason="the drill",
                ),
            )
        await session.commit()

        fires = [f for f in await _window(session) if f.routine_id == routine.id]
        assert len(fires) == 1, "paused is not failed - the menu word is the OUTCOME"


async def test_rescanned_windows_never_double_fire() -> None:
    from prismoid.operations import tasks as tasks_ops

    async with session_scope() as session:
        me = await _human(session)
        routine = await _event_routine(session, me, posture="auto")
        watched = await tasks_ops.create_task(session, me, goal="watched once")
        await _deliver_event(session, me, watched)

        first = [f for f in await _window(session) if f.routine_id == routine.id]
        second = [f for f in await _window(session) if f.routine_id == routine.id]
        assert len(first) == 1 and len(second) == 1, "overlapping windows re-offer freely"
        for fire in (first[0], second[0]):
            await ops.execute_fire(
                session,
                routine.id,
                occurrence=fire.occurrence,
                source=RunSource.EVENT,
                payload=fire.payload,
                chain_depth=fire.chain_depth,
            )
        assert await _tasks_of(session, routine.id) == 1, "the ledger's unique occurrence absorbs"


async def test_cancelling_a_held_run_is_clean_not_a_500() -> None:
    """A HELD task's workflow is never enqueued, and the cancel's
    post-commit wake hit DBOS's non-existent-destination error AFTER the cancel had landed -
    a 500 for a success. True for held forks long before routines. The wake is a poke; a poke
    with no addressee is a no-op (the dispatcher's tolerance, pinned here)."""
    from prismoid.operations import steering

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.posture = "hold"
        routine.min_interval_s = 0
        held = await _clock_fire(session, routine)
        assert held.decision == RunDecision.HELD  # pyright: ignore[reportAttributeAccessIssue]

        status = await steering.cancel(session, me, held.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert status == "cancelled"
        task = await session.get(Task, held.task_id)  # pyright: ignore[reportAttributeAccessIssue]
        assert task is not None and task.status == "cancelled" and task.held_at is None


async def test_agent_proposal_lands_paused_and_the_owner_arms_it() -> None:
    """W6's one non-negotiable: `routine.create` can propose, never arm. The proposer closure
    (the tool's hands) creates the routine BORN PAUSED under the task's owner, notes WHICH task
    proposed it, knocks the inbox - and the owner's resume is the arming ceremony."""
    from prismoid.db.models.inbox import InboxItem, InboxKind

    async with session_scope() as session:
        me = await _human(session)
        task = Task(id=uuid.uuid4(), goal="morning digest", creator_actor_id=me.id, criteria={}, inputs=[])
        session.add(task)  # the proposing task: the proposer acts in ITS workspace
        await session.commit()
        me_id, task_id, workspace_id = me.id, task.id, task.workspace_id

    propose = ops.proposer(task_id, me_id, None)
    message = await propose(
        {"name": f"digest-{uuid.uuid4().hex[:8]}", "instruction": "morning digest", "cron": "0 9 * * 1"}
    )
    assert "PAUSED" in message

    async with bound_scope(workspace_id) as session:  # the owner's door binds the task's workspace
        me = await session.get(Actor, me_id)
        assert me is not None
        rows = await Routine.list_for(session, me, project_id=None, limit=50, cursor=None)
        routine = next(r for r in rows if str(task_id) in (r.pause_note or ""))
        assert routine.status == RoutineStatus.PAUSED
        assert routine.paused_reason == "user"
        assert routine.next_fire_at is None, "paused means the sweep can never see it"
        items = await InboxItem.list_for(session, me)
        assert any(i.kind == InboxKind.ROUTINE_PAUSED.value for i in items), "the proposal knocks"

        # The arming ceremony: resume computes the FIRST next_fire - from now, never the past.
        resumed = await ops.resume_routine(session, me, routine.id)
        assert resumed.status == RoutineStatus.ACTIVE
        assert resumed.next_fire_at is not None and resumed.next_fire_at > utcnow()


async def test_proposal_refusals_are_results_not_crashes() -> None:
    """The tool's contract (the web door's stance): a refusal comes back as an actionable STRING the
    agent can relay - journaled ok=false - never an exception up the agent loop."""
    from prismoid.ai.tools.routines import routines_toolset

    async with session_scope() as session:
        me = await _human(session)
        await session.commit()
        me_id = me.id

    journal_lines: list[tuple[str, bool]] = []

    async def journal(tool: str, ok: bool, *_: object, **__: object) -> None:
        journal_lines.append((tool, ok))

    toolset = routines_toolset(journal, ops.proposer(uuid.uuid4(), me_id, None))
    fn: Any = toolset.tools["routine_create"].function

    both = await fn(name="x", instruction="y", cron="0 9 * * *", every="4h")
    assert "not saved" in both and "exactly one" in both
    neither = await fn(name="x", instruction="y")
    assert "not saved" in neither
    assert journal_lines == [("routine.create", False), ("routine.create", False)]


# ── regressions ─────────────────────────────────────────────────────────────


async def test_an_event_routine_refuses_a_schedule_patch() -> None:
    """A ScheduleSpec written over an EventSpec (kind unflipped) was a permanently silent
    routine behind a 200."""
    from prismoid.errors import ValidationFailed

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me, schedule=None, on_events=["task.delivered"])
        with pytest.raises(ValidationFailed):
            await ops.update_routine(
                session,
                me,
                routine.id,
                schedule={"cron": "0 9 * * *", "every_s": None, "at": None, "timezone": None},
            )


async def test_seasons_govern_api_fires_too() -> None:
    """until_at/max_runs sat inside the clock-only branch - an event/token fire minted forever
    past the season."""
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me, max_runs=1)
        routine.runs_minted = 1  # the season is complete
        await session.flush()
        result = await ops.execute_fire(
            session, routine.id, occurrence=f"api:{uuid.uuid4().hex[:12]}", source=RunSource.API
        )
        assert result.decision == RunDecision.SKIPPED_SEASON
        await session.refresh(routine)
        assert routine.status == RoutineStatus.RETIRED


async def test_crash_resume_writes_the_receipt_it_was_owed() -> None:
    """create_task committed, then the receipt transaction was lost: the resumed gate must
    hand back THIS occurrence's task - it used to see its own mint as 'the previous run' and
    journal skipped_overlap while the task ran unrecorded."""
    from prismoid.operations import tasks as tasks_ops
    from prismoid.schemas.routines import mint_client_token

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        fire_at = _past(1)
        routine.next_fire_at = fire_at
        await session.flush()
        occ = occurrence_for_clock(fire_at)
        # simulate the crash window: the mint committed, the receipt never ran
        task = await tasks_ops.create_task(
            session,
            me,
            goal=routine.instruction,
            clarify="never",
            budget={"usd": 1.0},
            client_token=mint_client_token(routine.id, occ),
            origin_routine_id=routine.id,
        )
        result = await ops.execute_fire(session, routine.id, occurrence=occ, source=RunSource.CLOCK)
        assert result.decision == RunDecision.MINTED and result.task_id == task.id
        run = await RoutineRun.by_occurrence(session, routine.id, occ)
        assert run is not None and run.task_id == task.id and run.detail.startswith("recovered")
        await session.refresh(routine)
        assert routine.runs_minted == 1 and routine.next_fire_at is not None
        assert await _tasks_of(session, routine.id) == 1, "no second mint"


async def test_timezone_edits_stick_and_survive_lane_edits() -> None:
    """`set --timezone` alone was a silent no-op, and a cron edit silently reset an explicit
    timezone to the profile's."""
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(
            session,
            me,
            schedule={"cron": "0 8 * * *", "every_s": None, "at": None, "timezone": "Europe/Berlin"},
        )
        # timezone-only PATCH: the lane stays, the wall clock moves
        updated = await ops.update_routine(
            session,
            me,
            routine.id,
            schedule={"cron": None, "every_s": None, "at": None, "timezone": "Asia/Kolkata"},
        )
        assert updated.trigger["cron"] == "0 8 * * *"
        assert updated.trigger["timezone"] == "Asia/Kolkata"
        # lane PATCH without a timezone: the explicit one is KEPT, not re-resolved
        updated = await ops.update_routine(
            session,
            me,
            routine.id,
            schedule={"cron": "30 8 * * *", "every_s": None, "at": None, "timezone": None},
        )
        assert updated.trigger["cron"] == "30 8 * * *"
        assert updated.trigger["timezone"] == "Asia/Kolkata"


async def test_a_poison_fire_journals_error_and_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-PrismoidError used to escape the gate: workflow ERROR, no journal, no advance -
    the occurrence's id consumed and the routine wedged forever."""
    from prismoid.operations import tasks as tasks_ops

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database hiccuped")

    monkeypatch.setattr(tasks_ops, "create_task", _boom)
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        fire_at = _past(1)
        routine.next_fire_at = fire_at
        await session.flush()
        occ = occurrence_for_clock(fire_at)
        result = await ops.execute_fire(session, routine.id, occurrence=occ, source=RunSource.CLOCK)
        assert result.decision == RunDecision.ERROR and "RuntimeError" in result.detail
        run = await RoutineRun.by_occurrence(session, routine.id, occ)
        assert run is not None, "the poison fire wears a receipt"
        await session.refresh(routine)
        advanced_to = routine.next_fire_at
        assert advanced_to is not None and advanced_to > utcnow(), (
            "the schedule advanced past the poison occurrence - no wedge"
        )


async def test_junk_tokens_do_not_spend_the_burst_window() -> None:
    """The burst gate counted hits BEFORE the token check - junk-token spray could 429-lock
    the legitimate webhook out of its own door."""
    from prismoid.errors import Unauthorized

    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        token = await ops.mint_fire_token(session, me, routine.id)
        for _ in range(25):  # far past the 10/min cap
            with pytest.raises(Unauthorized):
                await ops.fire_via_token(session, routine.id, "rt_garbage")
        result = await ops.fire_via_token(session, routine.id, token)
        assert result.decision in (RunDecision.MINTED, RunDecision.HELD), (
            "the real caller's window was untouched by unauthenticated spray"
        )


async def test_the_sweep_drain_arm_sees_the_park_and_edits_free_the_slot() -> None:
    """The park's ONLY state is the row: `Routine.parked` is the sweep's second arm (no
    workflow sleeps on a queue slot, and an overlap edit off queue frees
    a slot that used to stay claimed forever."""
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.overlap = "queue"
        routine.min_interval_s = 0
        first = await _clock_fire(session, routine)
        assert first.decision == RunDecision.MINTED  # pyright: ignore[reportAttributeAccessIssue]

        await session.refresh(routine)
        fire2_at = utcnow() - dt.timedelta(seconds=95)
        routine.next_fire_at = fire2_at
        await session.commit()
        occ2 = occurrence_for_clock(fire2_at)
        buffered = await ops.execute_fire(session, routine.id, occurrence=occ2, source=RunSource.CLOCK)
        assert buffered.decision == RunDecision.BUFFERED

        parked = await Routine.parked(session)
        assert (routine.id, occ2) in [(row[0], row[1]) for row in parked], "the drain arm sees it"

        # The owner switches overlap off queue: the slot is freed at the edit…
        await ops.update_routine(session, me, routine.id, overlap="skip")
        await session.refresh(routine)
        assert routine.buffered_occurrence is None
        assert (routine.id, occ2) not in [(row[0], row[1]) for row in await Routine.parked(session)]
        # …and the drained re-entry (already enqueued by an earlier tick) journals honestly
        # instead of standing on a phantom buffer (superseded: the row changed under it).
        result = await ops.execute_fire(session, routine.id, occurrence=occ2, source=RunSource.CLOCK)
        assert result.decision == RunDecision.SUPERSEDED
        assert await RoutineRun.by_occurrence(session, routine.id, occ2) is not None


async def test_a_removed_owners_routine_never_fires_and_journals_why() -> None:
    """Unattended spend does not outlive membership. The owner is removed from the
    routine's workspace; the clock gate and the rt_ door both mint nothing and wear the reason."""
    from prismoid.db.models.workspace import WorkspaceMember

    async with session_scope() as session:
        me = await _human(session)
        clock = await _routine(session, me)
        via_token = await _routine(session, me)
        fire_at = _past(1)
        clock.next_fire_at = fire_at
        via_token.min_interval_s = 0
        await session.commit()
        raw = await ops.mint_fire_token(session, me, via_token.id)
        await WorkspaceMember.remove(session, clock.workspace_id, me.id)
        await session.commit()

        result = await ops.execute_fire(
            session, clock.id, occurrence=occurrence_for_clock(fire_at), source=RunSource.CLOCK
        )
        assert result.decision == RunDecision.SKIPPED_MEMBERSHIP and result.task_id is None
        tokened = await ops.fire_via_token(session, via_token.id, raw)
        assert tokened.decision == RunDecision.SKIPPED_MEMBERSHIP and tokened.task_id is None

        for routine in (clock, via_token):
            assert await _tasks_of(session, routine.id) == 0
            runs = list(await session.scalars(select(RoutineRun).where(RoutineRun.routine_id == routine.id)))
            assert [r.decision for r in runs] == ["skipped_membership"]
            await session.refresh(routine)
            assert routine.status == RoutineStatus.PAUSED.value
