# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Machine time is metered per segment.

Every segment edge is here: a release (a Machine's and a throwaway frame's), a cancelled park's
hand-back, sleep's finisher, a reset of a warm Machine, a reconciler fix that takes the frame
away, a frame found gone at a bind, and the qa, figures and verify frames. And the money rule:
the watermark moves with the hand-back, so a segment is under-billed when it is lost on the way,
never billed twice."""

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from prismoid import storage
from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import session_scope
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import giveback as ex_giveback
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.machines import meter, store
from prismoid.operations.machines import reconcile as reconcile_ops
from prismoid.schemas.machines import MachineState, RowFix, Segment, SleepReason, machine_prefix
from prismoid.schemas.plan import StepSpec
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _usage(workspace_id: uuid.UUID, task_id: uuid.UUID) -> list[UsageEvent]:
    async with session_scope(workspace_id=workspace_id) as s:
        rows = list(
            (
                await s.scalars(
                    select(UsageEvent)
                    .where(UsageEvent.task_id == task_id, UsageEvent.family == "machine")
                    .order_by(UsageEvent.at)
                )
            ).all()
        )
        s.expunge_all()  # the scope's closing rollback would expire them
        return rows


async def _seconds(workspace_id: uuid.UUID, task_id: uuid.UUID) -> list[int]:
    return [int(u.units["second"]) for u in await _usage(workspace_id, task_id)]


async def _task_and_step(name: str = "Ada") -> tuple[Any, Any, uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        actor, workspace = await member(s, name)
        task = await task_ops.create_task(s, actor, goal="metered work")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="w", spec={}
        )
        s.add(step)
        await s.commit()
        return actor, workspace, task.id, step.id


async def _acquire(workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID, **kw: Any) -> Any:
    async with session_scope(workspace_id=workspace_id) as s:
        task, step = await s.get(Task, task_id), await s.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = kw.pop("attempt", 1)
        kw.setdefault("profile", "base")
        kw.setdefault("allow_domains", [])
        return await machines.acquire(s, task, step, **kw)


async def _bill(workspace_id: uuid.UUID, segment: Segment | None) -> None:
    """On a caller's session, as `_attempt`'s finally does."""
    assert segment is not None
    async with session_scope(workspace_id=workspace_id) as s:
        await machines.bill(s, segment)
        await s.commit()


async def _rewind(workspace_id: uuid.UUID, task_id: uuid.UUID, seconds: int) -> dt.datetime:
    """Move the Machine's watermark `seconds` into the past: that much warm time goes unbilled."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.locked_for_task(s, task_id)
        assert row is not None and row.metered_until is not None
        rewound = row.metered_until - dt.timedelta(seconds=seconds)
        row.metered_until = rewound
        await s.commit()
        return rewound


async def _machine(workspace_id: uuid.UUID, task_id: uuid.UUID) -> Machine:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        s.expunge(row)
        return row


# ── the brief's four ──────────────────────────────────────────────────────────────────────


async def test_a_step_on_the_machine_bills_its_segment_to_the_turn_author(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="metered")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="w", spec={}
        )
        s.add(step)
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        t, st = await s.get(Task, task.id), await s.get(PlanStep, step.id)
        assert t is not None and st is not None
        got = await machines.acquire(s, t, st, profile="browser", allow_domains=[])
        row = await Machine.for_task(s, task.id)
        assert row is not None and row.metered_until is not None
        row.metered_until = row.metered_until - dt.timedelta(seconds=120)  # two minutes of work
        await s.commit()
        segment = await machines.release(got, parked=False)
        assert segment is not None and segment.seconds >= 120
        await machines.bill(s, segment)  # on the caller's session, as `_attempt` does
        await s.commit()
    rows = await _usage(workspace.id, task.id)
    assert len(rows) == 1
    usage = rows[0]
    assert usage.units["second"] >= 120 and usage.model == "machine.browser" and usage.price_version == 0
    assert usage.cost_usd >= Decimal("0.0015")  # 2 min at 0.00075/min
    assert usage.actor_id == ada.id  # the turn's author (1a)


async def test_sleep_bills_the_idle_tail_and_stops_the_meter(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="idle tail")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="w", spec={}
        )
        s.add(step)
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        t, st = await s.get(Task, task.id), await s.get(PlanStep, step.id)
        assert t is not None and st is not None
        got = await machines.acquire(s, t, st, profile="base", allow_domains=[])
        segment = await machines.release(got, parked=False)
        if segment is not None:
            await machines.bill(s, segment)
        await s.commit()
        row = await Machine.locked_for_task(s, task.id)  # re-read: release moved it on its own session
        assert row is not None and row.metered_until is not None
        row.metered_until = row.metered_until - dt.timedelta(seconds=600)  # ten idle minutes
        machine_id, epoch = row.id, row.epoch
        await s.commit()
    try:
        await machines.sleep(machine_id, epoch, SleepReason.IDLE)
        async with session_scope(workspace_id=workspace.id) as s:
            after = await s.get(Machine, machine_id)
            assert after is not None and after.metered_until is None
        assert sum(u.units["second"] for u in await _usage(workspace.id, task.id)) >= 600
    finally:
        await store.delete_prefix(machine_prefix(machine_id))


async def test_a_throwaway_frame_bills_once(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="seat")
        await s.commit()
    await meter.meter_throwaway(task.id, "browser", 90)
    rows = await _usage(workspace.id, task.id)
    assert len(rows) == 1 and rows[0].cost_usd == Decimal("0.001125")


async def test_the_summary_sums_seconds_and_cost(fake_frames: Any, silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="summary")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="w", spec={}
        )
        s.add(step)
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        t, st = await s.get(Task, task.id), await s.get(PlanStep, step.id)
        assert t is not None and st is not None
        await machines.acquire(s, t, st, profile="base", allow_domains=[])
    await meter.meter_throwaway(task.id, "base", 60)
    async with session_scope(workspace_id=workspace.id) as s:
        summary = await machines.summary(s, task.id)
        assert await machines.summary(s, uuid.uuid4()) is None  # no Machine, no summary
    assert summary is not None and summary.warm_seconds == 60 and summary.cost_usd == Decimal("0.0005")
    assert summary.state == "running" and summary.browser_saved is False


# ── release: never records, returns what the caller bills ─────────────────────────────────


async def test_a_release_closes_its_segment_without_touching_the_tasks_row(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """T11-bill-on-caller: the step's session may hold the task's row (the harvest's events) while
    the release runs on its own - a release that recorded usage would wait on it, invisibly."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await _rewind(workspace.id, tid, 120)
    async with session_scope(workspace_id=workspace.id) as step_session:
        task = await step_session.get(Task, tid)
        assert task is not None
        await task.next_seq(step_session)  # the step holds the task's row, uncommitted
        segment = await asyncio.wait_for(machines.release(got, parked=False), 5)
        await step_session.rollback()
    assert segment is not None and segment.task_id == tid and segment.profile == "base"
    assert 120 <= segment.seconds < 180 and segment.at is not None
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.metered_until == segment.at  # the meter runs on, warm
    assert await _usage(workspace.id, tid) == [], "nothing is recorded until the caller bills it"


async def test_a_park_bills_up_to_the_park_and_keeps_the_meter_running(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await _rewind(workspace.id, tid, 60)
    segment = await machines.release(got, parked=True)
    assert segment is not None and segment.seconds >= 60
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.PARKED and row.metered_until == segment.at


async def test_a_release_that_lost_its_machine_bills_nothing(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    zombie = await _acquire(workspace.id, tid, sid, attempt=1)
    await _rewind(workspace.id, tid, 60)
    newer = await _acquire(workspace.id, tid, sid, attempt=2)  # takes the Machine over
    assert await machines.release(zombie, parked=False) is None, "a lost compare-and-set bills nothing"
    segment = await machines.release(newer, parked=False)
    assert segment is not None and segment.seconds >= 60, "the newer attempt's release bills it, once"


async def test_a_seat_frame_bills_once_when_it_is_destroyed(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid, seat=True, profile="browser")
    got.started -= 90  # a minute and a half of work
    segment = await machines.release(got, parked=False)
    assert segment is not None and segment.task_id == tid and segment.profile == "browser"
    assert 90 <= segment.seconds < 100 and segment.at is None  # the worker timed it; billed at db now
    assert fake_frames.world == {}
    await _bill(workspace.id, segment)
    rows = await _usage(workspace.id, tid)
    assert [(u.model, u.units) for u in rows] == [("machine.browser", {"second": segment.seconds})]
    assert rows[0].provider == "docker"


async def test_a_parked_throwaway_bills_its_run_at_the_park_and_its_resume_at_the_end(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A parked throwaway keeps its frame for the resume and bills the run
    so far at the park; the resume binds it by its old name with a fresh clock and bills only its
    own run when it ends - each portion once."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid, seat=True)
    got.started -= 40  # forty seconds of work, then the step asks
    parked = await machines.release(got, parked=True)
    assert parked is not None and parked.task_id == tid and 40 <= parked.seconds < 50 and parked.at is None
    assert list(fake_frames.world) == [got.frame.ref], "the park keeps its frame"
    resumed = await _acquire(workspace.id, tid, sid, resuming=True, legacy_ref=got.frame.ref)
    assert resumed.kind == "legacy"
    resumed.started -= 30
    segment = await machines.release(resumed, parked=False)
    assert segment is not None and segment.task_id == tid and 30 <= segment.seconds < 40, (
        "never the park's again"
    )
    assert fake_frames.world == {}


async def test_the_watermark_never_moves_backwards(fake_frames: Any, silent_dispatcher: None) -> None:
    """Fix round 1, M1: an edge's `now` is its transaction's start, and a lock wait can make it
    older than a watermark another transaction just wrote - the watermark keeps the later one."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    ahead = await _rewind(workspace.id, tid, -60)  # a watermark written after this edge's `now`
    assert await machines.release(got, parked=True) is None
    assert (await _machine(workspace.id, tid)).metered_until == ahead
    released = await machines.release_parked(tid, sid)
    assert released.held and released.segment is None
    assert (await _machine(workspace.id, tid)).metered_until == ahead
    async with session_scope(workspace_id=workspace.id) as s:
        row = await Machine.locked_for_task(s, tid)
        assert row is not None
        await meter.meter_segment(s, row, await Machine.db_now(s))
        assert row.metered_until == ahead
        await s.rollback()
    assert await _usage(workspace.id, tid) == []


# ── a cancelled park's hand-back ──────────────────────────────────────────────────────────


async def test_release_parked_returns_the_parks_segment(fake_frames: Any, silent_dispatcher: None) -> None:
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await machines.release(got, parked=True)
    await _rewind(workspace.id, tid, 300)  # five minutes parked
    released = await machines.release_parked(tid, sid)
    assert released.held and released.segment is not None and released.segment.seconds >= 300
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.metered_until == released.segment.at
    again = await machines.release_parked(tid, sid)
    assert not again.held and again.segment is None
    assert await _usage(workspace.id, tid) == [], "release_parked records nothing: its caller bills it"


async def test_a_park_that_slept_hands_back_no_segment(fake_frames: Any, silent_dispatcher: None) -> None:
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await machines.release(got, parked=True)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.PARK_HOLD) == "slept"
        released = await machines.release_parked(tid, sid)
        assert released.held and released.segment is None, "the sleep already billed it"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_the_park_hand_back_bills_in_its_own_transaction(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """`_give_park_back` runs on ids after its caller committed or rolled back: it bills the park's
    segment in a fresh transaction, never inside release_parked's locked one."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await machines.release(got, parked=True)
    await _rewind(workspace.id, tid, 300)
    await ex_giveback.give_park_back(tid, sid, 1, profile="base")
    assert [s >= 300 for s in await _seconds(workspace.id, tid)] == [True]
    assert list(fake_frames.world) == [got.frame.ref], "an idle Machine keeps its frame"


# ── sleep, reset, the reconciler, and a frame found gone ───────────────────────────────────


async def test_reset_of_a_warm_machine_bills_its_segment_and_stops_the_meter(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    ada, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await machines.release(got, parked=False)  # its sub-second segment is the caller's (unbilled here)
    await _rewind(workspace.id, tid, 200)
    async with session_scope(workspace_id=workspace.id) as s:
        await machines.reset(s, ada, tid)
    assert [s >= 200 for s in await _seconds(workspace.id, tid)] == [True]
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.GONE and row.metered_until is None


async def test_reset_of_a_sleeping_machine_bills_nothing_more(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    ada, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await machines.release(got, parked=False)
    await _rewind(workspace.id, tid, 100)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
        billed = await _seconds(workspace.id, tid)
        assert len(billed) == 1 and billed[0] >= 100
        async with session_scope(workspace_id=workspace.id) as s:
            await machines.reset(s, ada, tid)
        assert await _seconds(workspace.id, tid) == billed, "an asleep Machine has no warm time to bill"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_reconciler_fix_back_fills_from_the_watermark_after_its_commit(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A crash that skipped an emission is back-filled when the reconciler resolves the row,
    bounded by `metered_until` - and billed after the fix commits (`_apply` holds the Machine's row
    under `lock_briefly`, where the task's row must never be taken)."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await _rewind(workspace.id, tid, 120)
    await _bill(workspace.id, await machines.release(got, parked=False))  # the step's two minutes, billed
    await _rewind(workspace.id, tid, 300)  # then five idle minutes, and the frame died
    row = await _machine(workspace.id, tid)
    fake_frames.world.clear()
    async with session_scope(workspace_id=workspace.id) as holder:
        task = await holder.get(Task, tid)
        assert task is not None
        await task.next_seq(holder)  # a step holds the task's row
        fix = RowFix.of(reconcile_ops.view_of(row), MachineState.GONE)
        fixing = asyncio.create_task(reconcile_ops._apply(fix))  # pyright: ignore[reportPrivateUsage]
        for _ in range(100):  # the fix commits without the task's row
            if (await _machine(workspace.id, tid)).state == MachineState.GONE:
                break
            await asyncio.sleep(0.05)
        assert (await _machine(workspace.id, tid)).state == MachineState.GONE
        await holder.rollback()
    await asyncio.wait_for(fixing, 5)
    billed = await _seconds(workspace.id, tid)
    assert len(billed) == 2 and 120 <= billed[0] < 180 and 300 <= billed[1] < 360, "never the step twice"
    assert (await _machine(workspace.id, tid)).metered_until is None


async def test_a_dead_hold_released_by_the_reconciler_keeps_the_meter_running(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    await _acquire(workspace.id, tid, sid)
    watermark = await _rewind(workspace.id, tid, 300)
    row = await _machine(workspace.id, tid)
    fix = RowFix.of(reconcile_ops.view_of(row), MachineState.IDLE, keep_frame=True, release_hold=True)
    await reconcile_ops._apply(fix)  # pyright: ignore[reportPrivateUsage]
    after = await _machine(workspace.id, tid)
    assert after.state == MachineState.IDLE and after.metered_until == watermark, "the frame is still warm"
    assert await _usage(workspace.id, tid) == []


async def test_a_frame_found_gone_at_a_bind_bills_its_tail_and_the_wake_starts_anew(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """Metered: the bind finds the frame gone and wakes the Machine at a new epoch -
    the old frame's unbilled tail is billed then (as the reconciler would), and the new frame's
    meter starts at its wake."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await machines.release(first, parked=False)
    await _rewind(workspace.id, tid, 240)
    fake_frames.world.clear()  # an operator `docker rm`
    second = await _acquire(workspace.id, tid, sid, attempt=2)
    assert second.woke and second.epoch == first.epoch + 1
    billed = await _seconds(workspace.id, tid)
    assert len(billed) == 1 and 240 <= billed[0] < 300
    row = await _machine(workspace.id, tid)
    assert row.metered_until is not None and row.metered_until >= row.state_since


# ── never twice ───────────────────────────────────────────────────────────────────────────


async def test_a_bill_lost_on_a_poisoned_session_is_never_billed_again(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A step session poisoned by the failure that ended it loses its segment (logged); the sleep
    that follows bills only the idle tail, never the lost segment a second time."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await _rewind(workspace.id, tid, 120)
    segment = await machines.release(got, parked=False)
    assert segment is not None and segment.seconds >= 120
    async with session_scope(workspace_id=workspace.id) as poisoned:
        with pytest.raises(DBAPIError):
            await poisoned.execute(text("select 1/0"))
        await machines.bill(poisoned, segment)  # never raises
    assert await _usage(workspace.id, tid) == []
    await _rewind(workspace.id, tid, 600)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
        billed = await _seconds(workspace.id, tid)
        assert len(billed) == 1 and 600 <= billed[0] < 660, "the idle tail only"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


# ── the qa, figures and verify frames: billed from the moment the frame is up ────────────


class _Stop(Exception):
    """The fake frame's: whatever ran in it is over."""


class _WorkFrame:
    """A throwaway frame that comes up at once, works for a second, then stops what runs in it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def create(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def write_bytes(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(1.05)
        raise _Stop

    async def read_bytes(self, *args: Any, **kwargs: Any) -> bytes:
        raise FileNotFoundError

    async def destroy(self) -> None:
        return None


class _DeadFrame(_WorkFrame):
    """A frame that tries for a second to come up, and never does."""

    async def create(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(1.05)
        raise _Stop


def _frames(monkeypatch: pytest.MonkeyPatch, frame: type[_WorkFrame]) -> None:
    import prismoid.ai.frames as frames
    from prismoid.operations import verification

    for module in (frames, verification):
        monkeypatch.setattr(module, "new_frame", frame)
        monkeypatch.setattr(module, "ensure_ready", lambda profile="base": None)


def _deliverable(task_id: uuid.UUID) -> Artifact:
    body = b"# memo\n"
    digest = storage.put(body)
    return Artifact(
        id=uuid.uuid4(),
        lineage_id=uuid.uuid4(),
        task_id=task_id,
        type="document",
        role="deliverable",
        title="Memo",
        sha256=digest,
        bytes=len(body),
        storage_key=storage.key_for(digest),
        provenance={
            "path": "/artifacts/memo.md",
            "type": "document",
            "title": "Memo",
            "sha256": digest,
            "bytes": len(body),
            "sources": [],
            "agent": "writer",
            "model": "",
            "skill_pins": [],
            "at": "2026-10-01T00:00:00Z",
        },
    )


async def _qa(workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID) -> None:
    from prismoid.operations import qa

    async with session_scope(workspace_id=workspace_id) as s:
        task, step = await s.get(Task, task_id), await s.get(PlanStep, step_id)
        assert task is not None and step is not None
        await qa.review(s, task, step, StepSpec(), [_deliverable(task_id)])  # never raises


async def test_a_qa_frame_bills_its_time_even_when_the_pass_fails(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA commits its session before the frame work, so the frame bills on its own session - a
    pass that fails (PASS_UNREAD) still paid for its frame."""
    _frames(monkeypatch, _WorkFrame)
    _, workspace, tid, sid = await _task_and_step()
    await _qa(workspace.id, tid, sid)
    rows = await _usage(workspace.id, tid)
    assert [(u.model, u.units["second"] >= 1) for u in rows] == [("machine.base", True)]


async def test_a_qa_frame_that_never_came_up_bills_nothing(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, I1: the clock starts when `create` returns - a second spent failing to boot is
    not machine time."""
    _frames(monkeypatch, _DeadFrame)
    _, workspace, tid, sid = await _task_and_step()
    await _qa(workspace.id, tid, sid)
    assert await _usage(workspace.id, tid) == []


async def test_a_figures_frame_bills_its_time_after_the_harvest_commits(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.operations import figures

    harvested: list[str] = []

    async def harvest(session: Any, task: Task, step: PlanStep, frame: Any, **kw: Any) -> list[Artifact]:
        await asyncio.sleep(1.05)  # a second of work in the frame
        await task.next_seq(session)  # the harvest's events take the task's row
        harvested.append(frame.ref)
        return []

    monkeypatch.setattr(ex_harvest, "harvest", harvest)
    _, workspace, tid, sid = await _task_and_step()
    async with session_scope(workspace_id=workspace.id) as s:
        task, step = await s.get(Task, tid), await s.get(PlanStep, sid)
        assert task is not None and step is not None
        await asyncio.wait_for(figures._publish_batch(s, task, step, [("f1", b"png")], 1), 10)  # pyright: ignore[reportPrivateUsage]
    assert harvested and fake_frames.world == {}
    assert [(u.model, u.units["second"] >= 1) for u in await _usage(workspace.id, tid)] == [
        ("machine.base", True)
    ]


async def test_a_figures_frame_that_never_came_up_bills_nothing(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, I1: the bill rides the caller's session, and a caller that commits after the
    failure still finds nothing billed for a frame that never booted."""
    from prismoid.operations import figures

    _frames(monkeypatch, _DeadFrame)
    _, workspace, tid, sid = await _task_and_step()
    async with session_scope(workspace_id=workspace.id) as s:
        task, step = await s.get(Task, tid), await s.get(PlanStep, sid)
        assert task is not None and step is not None
        with pytest.raises(_Stop):
            await figures._publish_batch(s, task, step, [("f1", b"png")], 1)  # pyright: ignore[reportPrivateUsage]
        await s.commit()  # whatever rode this session lands now
    assert await _usage(workspace.id, tid) == []


async def test_a_figures_frame_that_fails_holding_the_tasks_row_never_hangs_its_bill(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harvest failed with the task's row locked on the caller's session: the bill rides that
    session (a bill on its own would wait on our own lock forever) and goes with its rollback."""
    from prismoid.operations import figures

    async def harvest(session: Any, task: Task, step: PlanStep, frame: Any, **kw: Any) -> list[Artifact]:
        await asyncio.sleep(1.05)  # a second of frame time: there is a bill to take the lock
        await task.next_seq(session)
        raise RuntimeError("the harvest failed")

    monkeypatch.setattr(ex_harvest, "harvest", harvest)
    _, workspace, tid, sid = await _task_and_step()
    async with session_scope(workspace_id=workspace.id) as s:
        task, step = await s.get(Task, tid), await s.get(PlanStep, sid)
        assert task is not None and step is not None
        with pytest.raises(RuntimeError, match="harvest failed"):
            await asyncio.wait_for(figures._publish_batch(s, task, step, [("f1", b"png")], 1), 10)  # pyright: ignore[reportPrivateUsage]
    assert fake_frames.world == {}


class _Verified(_WorkFrame):
    """A verify frame: up at once, the bytes written, no preview left behind."""

    async def write_bytes(self, *args: Any, **kwargs: Any) -> None:
        return None


class _SlowToVerify(_Verified):
    """A verify frame that takes a second to come up, then checks in no time at all."""

    async def create(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(1.05)


async def _verify(
    monkeypatch: pytest.MonkeyPatch, frame: type[_WorkFrame], *, suite_s: float, hold_task_row: bool
) -> tuple[Any, uuid.UUID]:
    """`verify_artifact` on a fresh deliverable, in a frame of `frame`'s kind whose suite runs
    `suite_s` seconds; the caller's session commits after it. The judge is out of the way."""
    from prismoid.ai.verification import pipeline
    from prismoid.operations import verification

    async def checks(*args: Any, **kwargs: Any) -> list[Any]:
        await asyncio.sleep(suite_s)
        return []

    async def no_judge(*args: Any, **kwargs: Any) -> None:
        return None

    _frames(monkeypatch, frame)
    monkeypatch.setattr(pipeline, "run_suite", checks)
    monkeypatch.setattr(verification, "judge", no_judge)
    _, workspace, tid, _ = await _task_and_step()
    async with session_scope(workspace_id=workspace.id) as s:
        artifact = _deliverable(tid)
        s.add(artifact)
        await s.commit()
        if hold_task_row:
            task = await s.get(Task, tid)
            assert task is not None
            await task.next_seq(s)  # the fleet's step session holds the task's row
        await asyncio.wait_for(verification.verify_artifact(s, artifact), 10)
        if hold_task_row:
            assert await _usage(workspace.id, tid) == [], "on the caller's session, until it commits"
        await s.commit()
        assert await verification.latest_for(s, artifact.id) is not None, "the receipt landed"
    return workspace, tid


async def test_a_verify_frame_bills_on_the_callers_session_beside_its_receipt(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet verifies in-line on a step session that may hold the task's row: the verify frame
    bills on the caller's session (never its own, which would wait on that lock), and only beside
    the receipt - so the task's row is never held across the judge's model calls."""
    workspace, tid = await _verify(monkeypatch, _Verified, suite_s=1.05, hold_task_row=True)
    assert [(u.model, u.units["second"] >= 1) for u in await _usage(workspace.id, tid)] == [
        ("machine.base", True)
    ]


async def test_a_verify_frames_start_up_is_not_billed(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, I1: one rule for every frame - the clock starts when `create` returns."""
    workspace, tid = await _verify(monkeypatch, _SlowToVerify, suite_s=0, hold_task_row=False)
    assert await _usage(workspace.id, tid) == []


async def test_a_throwaway_for_a_deleted_task_bills_nothing(silent_dispatcher: None) -> None:
    gone = uuid.uuid4()
    await meter.meter_throwaway(gone, "base", 60)  # no task: no row, and never an error
    async with session_scope(platform=True) as s:
        assert (await s.scalars(select(UsageEvent).where(UsageEvent.task_id == gone))).all() == []


async def test_a_machines_time_is_billed_to_its_rows_driver_not_the_workers_setting(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger's provider is the driver that ran the warm time - the
    Machine row's - so a worker switched to another driver never relabels a Machine it did not make."""
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")  # not the developer's .env
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    await _rewind(workspace.id, tid, 60)
    monkeypatch.setattr(settings.worker, "frame_driver", "sandbar")  # the operator switched since
    segment = await machines.release(got, parked=False)
    assert segment is not None and segment.driver == "docker"
    await _bill(workspace.id, segment)
    assert [u.provider for u in await _usage(workspace.id, tid)] == ["docker"]
