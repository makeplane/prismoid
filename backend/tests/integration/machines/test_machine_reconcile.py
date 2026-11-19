# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The sweep and the reconciler: only what Postgres disowns is destroyed.

Every test is scoped to its own data (fix round 1, I1): the sweep and the reconciler read only
the rows the test made (`only_our_rows`), and the orphan arm lists only the test's own prefixes.
A pass over the whole instance would fix a real warm Machine in a developer's own database, or delete another
test's objects."""

import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, ClassVar

import pytest
from sqlalchemy import select

from prismoid import storage
from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import common, store
from prismoid.operations.machines import reconcile as reconcile_ops
from prismoid.schemas.machines import (
    LABEL_ATTEMPT,
    LABEL_EPOCH,
    LABEL_KIND,
    LABEL_MACHINE,
    LABEL_STEP,
    LABEL_TASK,
    LABEL_WORKSPACE,
    FrameInfo,
    MachineState,
    RowFix,
    frame_name,
    seed_holder,
    step_holder,
)
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.screen import Lease
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import FakeFrame, SandbarFakeFrame

pytestmark = pytest.mark.integration

_MINE: set[uuid.UUID] = set()  # the Machines this test made; the passes see nothing else
_REAL_FRAME_DRIVERS = Machine.frame_drivers
_REAL_INVENTORY = frames.inventory  # the factory's own listing, before a fixture installs the fake's


@pytest.fixture(autouse=True)
def only_our_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    sweepable = Machine.sweepable.__func__
    for_reconcile = Machine.for_reconcile.__func__

    async def our_sweepable(cls: type[Machine], session: Any) -> list[Machine]:
        return [r for r in await sweepable(cls, session) if r.id in _MINE]

    async def our_for_reconcile(cls: type[Machine], session: Any, ids: Any) -> list[Machine]:
        return [r for r in await for_reconcile(cls, session, ids) if r.id in _MINE]

    async def our_frame_drivers(cls: type[Machine], session: Any) -> set[str]:
        ours = await session.scalars(
            select(Machine).where(Machine.id.in_(list(_MINE)), Machine.frame_ref.is_not(None))
        )
        return {r.driver for r in ours.all()}

    _MINE.clear()
    monkeypatch.setattr(Machine, "frame_drivers", classmethod(our_frame_drivers))
    monkeypatch.setattr(Machine, "sweepable", classmethod(our_sweepable))
    monkeypatch.setattr(Machine, "for_reconcile", classmethod(our_for_reconcile))
    yield
    _MINE.clear()


async def _row(state: MachineState, **kw: Any) -> tuple[Any, Machine]:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="r")
        row = Machine(
            workspace_id=workspace.id,
            task_id=task.id,
            driver="docker",
            profile="base",
            state=state,
            state_since=kw.pop("since", utcnow()),
            last_active_at=kw.pop("active", utcnow()),
            epoch=kw.pop("epoch", 1),
            **kw,
        )
        s.add(row)
        await s.commit()
        _MINE.add(row.id)
        return workspace, row


async def _machine_frame(fake_frames: Any, row: Machine, epoch: int, **kw: Any) -> Any:
    frame = fake_frames(str(row.task_id), name=frame_name(row.id, epoch))
    await frame.create(
        labels={LABEL_KIND: "machine", LABEL_MACHINE: str(row.id), LABEL_EPOCH: str(epoch)}, **kw
    )
    return frame


async def _step(workspace: Any, task_id: uuid.UUID, *, state: str, task_status: str | None = None) -> str:
    """A plan step on the Machine's task, at attempt 1 - its holder string. `task_status` moves the
    task too (a delivered task's `running` step is a park nobody will resume)."""
    async with session_scope(workspace_id=workspace.id) as s:
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task_id,
            ordinal=0,
            kind="step",
            state=state,
            title="w",
            spec={},
            attempt=1,
        )
        s.add(step)
        if task_status is not None:
            task = await s.get(Task, task_id)
            assert task is not None
            task.status = task_status
        await s.commit()
        return step_holder(str(step.id), 1)


async def _hold(workspace: Any, row: Machine, holder: str, *, frame_ref: str | None) -> None:
    async with session_scope(workspace_id=workspace.id) as s:
        fresh = await s.get(Machine, row.id)
        assert fresh is not None
        fresh.held_by, fresh.held_since, fresh.frame_ref = holder, fresh.state_since, frame_ref
        await s.commit()


async def _set(workspace: Any, machine_id: uuid.UUID, **values: Any) -> None:
    async with session_scope(workspace_id=workspace.id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        for name, value in values.items():
            setattr(row, name, value)
        await s.commit()


async def _now(workspace: Any, machine_id: uuid.UUID) -> Machine:
    async with session_scope(workspace_id=workspace.id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def _rebuilt(task_id: uuid.UUID) -> list[str]:
    """The reasons of the task's `machine.rebuilt` events, in order."""
    async with row_scope(Task, task_id) as s:
        events = await s.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
        )
        return [e.payload["reason"] for e in events.all() if e.type == "machine.rebuilt"]


def _warned(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every warning the reconciler logs from here on: its message and its fields."""
    said: list[dict[str, Any]] = []
    monkeypatch.setattr(
        reconcile_ops.logfire, "warn", lambda message, **kw: said.append({"message": message, **kw})
    )
    return said


def _same(a: Machine, b: Machine) -> bool:
    cols = ("state", "state_since", "epoch", "held_by", "frame_ref", "last_active_at", "egress_version")
    return all(getattr(a, c) == getattr(b, c) for c in cols)


# ── the sweep ─────────────────────────────────────────────────────────────────────────────


async def test_the_sweep_dispatches_idle_sleeps_and_expiries_per_tick(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    long_ago = utcnow() - dt.timedelta(days=30)
    _, idle = await _row(
        MachineState.IDLE, active=utcnow() - dt.timedelta(seconds=settings.machines.idle_s + 60)
    )
    _, old = await _row(MachineState.ASLEEP, active=long_ago, checkpoint_object="machines/x/1.tar.gz")
    tick = utcnow()
    assert await machines.sweep_once(tick) == 2  # only this test's rows
    jobs = dict(recording_dispatcher.enqueues)
    keys = [key for _, key in recording_dispatcher.enqueues]
    assert f"machine-sleep-{idle.id}-1~{tick.isoformat()}" in keys
    assert f"machine-expire-{old.id}-1~{tick.isoformat()}" in keys
    assert "machine_sleep" in jobs and "machine_expire" in jobs


async def test_an_expiry_is_offered_every_tick_and_runs_once(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """An expire that errored is offered again next tick instead of being
    deduped forever; `expire`'s own compare-and-set keeps two ticks' runs to one expiry."""
    workspace, old = await _row(
        MachineState.ASLEEP,
        active=utcnow() - dt.timedelta(days=30),
        checkpoint_object=f"machines/{uuid.uuid4()}/1.tar.gz",
    )
    first, second = utcnow(), utcnow() + dt.timedelta(seconds=30)
    await machines.sweep_once(first)
    await machines.sweep_once(second)
    expiries = [key for job, key in recording_dispatcher.enqueues if job == "machine_expire"]
    assert expiries == [
        f"machine-expire-{old.id}-1~{first.isoformat()}",
        f"machine-expire-{old.id}-1~{second.isoformat()}",
    ]
    assert await machines.expire(old.id, 1) == "expired"
    assert await machines.expire(old.id, 1) == "skipped"
    row = await _now(workspace, old.id)
    assert row.state == MachineState.GONE and row.checkpoint_object is None and row.gone_reason == "expired"


# ── the reconciler ────────────────────────────────────────────────────────────────────────


async def test_a_second_worker_booting_keeps_a_live_machine_and_kills_the_stale(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    five_min_ago = utcnow() - dt.timedelta(minutes=5)
    workspace, live = await _row(MachineState.RUNNING, epoch=4, since=five_min_ago)
    live_frame = await _machine_frame(fake_frames, live, 4)
    holder = await _step(workspace, live.task_id, state="running")  # a live peer's running step
    await _hold(workspace, live, holder, frame_ref=live_frame.ref)
    before = await _now(workspace, live.id)
    stale_frame = await _machine_frame(fake_frames, live, 3)
    plan = await machines.reconcile_once()
    assert live_frame.ref in fake_frames.world and stale_frame.ref not in fake_frames.world
    assert plan.destroy == (stale_frame.ref,) and plan.fixes == ()
    assert _same(await _now(workspace, live.id), before)  # the live Machine's row is untouched


async def test_a_row_whose_frame_vanished_falls_back_to_its_checkpoint(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    workspace, row = await _row(
        MachineState.IDLE,
        since=utcnow() - dt.timedelta(minutes=5),
        checkpoint_object="machines/y/1.tar.gz",
        checkpoint_sha256="0" * 64,
        frame_ref="machine-gone-1",
    )
    await machines.reconcile_once()
    fixed = await _now(workspace, row.id)
    assert fixed.state == MachineState.ASLEEP and fixed.frame_ref is None


async def test_a_stopped_frame_is_kept_for_its_next_bind(fake_frames: Any, silent_dispatcher: None) -> None:
    """A restart left an idle Machine's container stopped, its files intact. The reconciler leaves
    the container and its row as they are: the next step or sleep binds it, which starts it."""
    workspace, row = await _row(
        MachineState.IDLE,
        epoch=2,
        since=utcnow() - dt.timedelta(minutes=5),
        checkpoint_object="machines/y/1.tar.gz",
        checkpoint_sha256="0" * 64,
    )
    frame = await _machine_frame(fake_frames, row, 2)
    await _set(workspace, row.id, frame_ref=frame.ref)
    frame.stop()
    frame.created_at = utcnow() - dt.timedelta(minutes=5)  # a restart stops old containers
    before = await _now(workspace, row.id)
    plan = await machines.reconcile_once()
    assert plan.destroy == () and plan.fixes == () and frame.ref in fake_frames.world
    assert _same(await _now(workspace, row.id), before)


async def test_a_stuck_row_that_never_had_a_frame_says_nothing_now(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A fork's child born `sleeping` with no frame, whose seed job is no longer
    active, lost no unsaved work, so the stuck rule's fix records no `machine.rebuilt` now; the row
    keeps `gone_reason` for its wake, and a seed that lands later clears it."""
    workspace, child = await _row(MachineState.SLEEPING, since=utcnow() - dt.timedelta(hours=1), epoch=0)
    await machines.reconcile_once(sleeping_jobs=frozenset(), seeding_jobs=frozenset())
    assert await _rebuilt(child.task_id) == []
    rested = await _now(workspace, child.id)
    assert rested.state == MachineState.GONE and rested.gone_reason == "lost"


async def test_a_fix_that_takes_a_frame_says_its_unsaved_work_was_lost(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A row whose frame died lost what `/work` held since its last
    checkpoint, and the timeline says so - once, after the row's commit (never under its lock),
    whether the row rests on its checkpoint or is gone. A stuck wake lost nothing unsaved."""
    five_min_ago = utcnow() - dt.timedelta(minutes=5)
    workspace, kept = await _row(
        MachineState.IDLE,
        since=five_min_ago,
        checkpoint_object="machines/y/1.tar.gz",
        checkpoint_sha256="0" * 64,
        frame_ref="machine-gone-1",
    )
    _, bare = await _row(MachineState.RUNNING, since=five_min_ago, frame_ref="machine-gone-2")
    _, waking = await _row(MachineState.WAKING, since=utcnow() - dt.timedelta(hours=1))
    await machines.reconcile_once()
    assert await _rebuilt(kept.task_id) == ["lost"]
    assert await _rebuilt(bare.task_id) == ["lost"]
    assert await _rebuilt(waking.task_id) == []
    assert (await _now(workspace, kept.id)).state == MachineState.ASLEEP
    gone = await _now(workspace, bare.id)
    assert gone.state == MachineState.GONE and gone.gone_reason is None  # said now, not at the wake


async def test_old_unlabelled_frames_go_after_their_grace(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.worker, "instance", "")  # only the default instance adopts pre-1b frames
    old = fake_frames("legacy", name="frame-old-step-1")
    await old.create()
    old.created_at = utcnow() - dt.timedelta(seconds=settings.machines.legacy_grace_s + 60)
    young = fake_frames("legacy2", name="frame-young-step-1")
    await young.create()
    stranger = fake_frames("x", name="qa-postgres")  # not a frame: some other container on the daemon
    await stranger.create()
    stranger.created_at = old.created_at
    await machines.reconcile_once()
    assert "frame-old-step-1" not in fake_frames.world and "frame-young-step-1" in fake_frames.world
    assert "qa-postgres" in fake_frames.world


async def test_a_named_instance_leaves_unlabelled_frames_alone(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.worker, "instance", "i9")  # a dev instance: another worktree's frames
    old = fake_frames("legacy", name="frame-other-step-1")
    await old.create()
    old.created_at = utcnow() - dt.timedelta(seconds=settings.machines.legacy_grace_s + 60)
    await machines.reconcile_once()
    assert "frame-other-step-1" in fake_frames.world


async def test_a_frame_with_a_malformed_label_is_skipped_and_the_pass_goes_on(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """Fix round 1, M5: one bad label must never stop every pass (nor be destroyed for it)."""
    bad = fake_frames("bad", name="machine-bad-1")
    await bad.create(labels={LABEL_KIND: "machine", LABEL_MACHINE: "not-a-uuid", LABEL_EPOCH: "1"})
    workspace, row = await _row(
        MachineState.IDLE, since=utcnow() - dt.timedelta(minutes=5), frame_ref="machine-x-1"
    )
    plan = await machines.reconcile_once()
    assert plan.skipped == ("machine-bad-1",) and "machine-bad-1" in fake_frames.world
    assert (await _now(workspace, row.id)).state == MachineState.GONE  # the rest of the pass ran


# ── dead holds ─────────────────────────────────────


async def test_a_dead_step_hold_is_released_and_the_frame_kept(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A hold whose attempt is not running blocks every later step for good.
    A finished attempt's (a release that failed), and a running step's on a finished task (a park
    nobody will resume) are both let go; a live park keeps its hold."""
    five_min_ago = utcnow() - dt.timedelta(minutes=5)
    workspace, done = await _row(MachineState.RUNNING, epoch=2, since=five_min_ago, active=five_min_ago)
    done_frame = await _machine_frame(fake_frames, done, 2)
    await _hold(workspace, done, await _step(workspace, done.task_id, state="done"), frame_ref=done_frame.ref)

    workspace2, orphaned_park = await _row(MachineState.PARKED, epoch=1, since=five_min_ago)
    park_frame = await _machine_frame(fake_frames, orphaned_park, 1)
    holder = await _step(workspace2, orphaned_park.task_id, state="running", task_status="delivered")
    await _hold(workspace2, orphaned_park, holder, frame_ref=park_frame.ref)

    workspace3, live_park = await _row(MachineState.PARKED, epoch=1, since=five_min_ago)
    live_frame = await _machine_frame(fake_frames, live_park, 1)
    live_holder = await _step(workspace3, live_park.task_id, state="running", task_status="waiting")
    await _hold(workspace3, live_park, live_holder, frame_ref=live_frame.ref)

    plan = await machines.reconcile_once()

    assert plan.destroy == ()
    for owner, row, ref in ((workspace, done, done_frame.ref), (workspace2, orphaned_park, park_frame.ref)):
        now = await _now(owner, row.id)
        assert now.state == MachineState.IDLE and now.held_by is None and now.held_since is None
        assert now.frame_ref == ref and ref in fake_frames.world  # the frame is kept, warm
        assert now.last_active_at > five_min_ago  # the idle clock starts at the release
    kept = await _now(workspace3, live_park.id)
    assert kept.state == MachineState.PARKED and kept.held_by == live_holder


async def test_a_released_park_loses_the_steps_egress(fake_frames: Any, silent_dispatcher: None) -> None:
    """Fix round 1, M1: an idle Machine's background processes reach nothing - a dead park's list
    (its `once` hosts included) goes with the hold, as `release_parked` narrows it."""
    five_min_ago = utcnow() - dt.timedelta(minutes=5)
    workspace, park = await _row(
        MachineState.PARKED, epoch=1, since=five_min_ago, egress_version=3, egress_hosts=["example.com"]
    )
    frame = await _machine_frame(fake_frames, park, 1, allow_domains=["example.com"], egress_version=3)
    holder = await _step(workspace, park.task_id, state="running", task_status="cancelled")
    await _hold(workspace, park, holder, frame_ref=frame.ref)
    await machines.reconcile_once()
    row = await _now(workspace, park.id)
    assert row.state == MachineState.IDLE and row.egress_version == 4 and row.egress_hosts == []
    live = fake_frames.world[frame.ref]
    assert live.egress_hosts == [] and live.egress_version == 4


async def test_a_released_park_under_a_driver_keeps_the_steps_list(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A person driving the browser of a Machine whose park died keeps the list the step ran under
    (a stepless Machine's driver gets its last step's list), and it becomes the row's
    `step_hosts` for the next driver; the lease's ending narrows it later."""
    five_min_ago = utcnow() - dt.timedelta(minutes=5)
    workspace, park = await _row(
        MachineState.PARKED, epoch=1, since=five_min_ago, egress_version=3, egress_hosts=["example.com"]
    )
    frame = await _machine_frame(fake_frames, park, 1, allow_domains=["example.com"], egress_version=3)
    holder = await _step(workspace, park.task_id, state="running", task_status="cancelled")
    await _hold(workspace, park, holder, frame_ref=frame.ref)
    async with session_scope(workspace_id=workspace.id) as s:
        task = await s.get(Task, park.task_id)
        assert task is not None
        now = await Machine.db_now(s)
        row = await s.get(Machine, park.id)
        assert row is not None
        row.set_lease(
            Lease(
                actor_id=task.creator_actor_id,
                conn="tab-1",
                private=False,
                since=now,
                until=now + dt.timedelta(minutes=1),
                cap_at=None,
            )
        )
        await s.commit()
    await machines.reconcile_once()
    row = await _now(workspace, park.id)
    assert row.state == MachineState.IDLE and row.held_by is None
    assert (row.egress_version, row.egress_hosts, row.step_hosts) == (3, ["example.com"], ["example.com"])
    assert fake_frames.world[frame.ref].egress_hosts == ["example.com"]


async def test_a_dead_seed_hold_is_released_and_its_frame_thawed(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed cancelled mid-copy (the boot's version-ghost sweep) never
    thaws or releases its parent, and `reset` refuses a running row - so the reconciler does. A seed
    whose job is still queued or in flight is let be, however long its copy has run."""
    thawed: list[str] = []

    async def thaw(self: Any) -> None:
        thawed.append(self.name)

    monkeypatch.setattr(fake_frames, "thaw", thaw)
    long_ago = utcnow() - dt.timedelta(hours=2)
    workspace, row = await _row(MachineState.RUNNING, epoch=3, since=long_ago, active=long_ago)
    frame = await _machine_frame(fake_frames, row, 3)
    await _hold(workspace, row, seed_holder(uuid.uuid4()), frame_ref=frame.ref)
    slow_copy = utcnow() - dt.timedelta(seconds=2 * settings.machines.stuck_s + 60)  # past the old clock
    workspace2, copying = await _row(MachineState.RUNNING, epoch=1, since=slow_copy)
    copying_frame = await _machine_frame(fake_frames, copying, 1)
    child = uuid.uuid4()
    await _hold(workspace2, copying, seed_holder(child), frame_ref=copying_frame.ref)

    await machines.reconcile_once(seeding_jobs=frozenset({child}))

    fixed = await _now(workspace, row.id)
    assert fixed.state == MachineState.IDLE and fixed.held_by is None and fixed.frame_ref == frame.ref
    assert thawed == [frame.ref] and frame.ref in fake_frames.world
    live = await _now(workspace2, copying.id)
    assert live.state == MachineState.RUNNING and live.held_by == seed_holder(child)  # a live seed is let be


# ── a stuck sleep ─────────────────────────────────────────────


async def test_a_stuck_sleep_with_no_job_goes_back_to_idle_its_frame_kept_and_thawed(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    thawed: list[str] = []

    async def thaw(self: Any) -> None:
        thawed.append(self.name)

    monkeypatch.setattr(fake_frames, "thaw", thaw)
    stuck_since = utcnow() - dt.timedelta(seconds=settings.machines.stuck_s + 60)
    idle_since = utcnow() - dt.timedelta(hours=1)
    workspace, row = await _row(MachineState.SLEEPING, epoch=2, since=stuck_since, active=idle_since)
    frame = await _machine_frame(fake_frames, row, 2)
    await _set(workspace, row.id, frame_ref=frame.ref)

    plan = await machines.reconcile_once(sleeping_jobs=frozenset())

    assert plan.destroy == () and frame.ref in fake_frames.world  # the only copy of /work lives on
    fixed = await _now(workspace, row.id)
    assert fixed.state == MachineState.IDLE and fixed.frame_ref == frame.ref
    assert fixed.last_active_at == idle_since  # the sweep's idle arm offers the sleep again
    assert thawed == [frame.ref]


async def test_a_sleep_whose_job_is_queued_or_unknown_is_left_alone(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    stuck_since = utcnow() - dt.timedelta(seconds=settings.machines.stuck_s + 60)
    workspace, row = await _row(MachineState.SLEEPING, epoch=2, since=stuck_since)
    frame = await _machine_frame(fake_frames, row, 2)
    await _set(workspace, row.id, frame_ref=frame.ref)
    before = await _now(workspace, row.id)
    for jobs in (frozenset({row.id}), None):  # queued behind a backlog; or the listing failed
        plan = await machines.reconcile_once(sleeping_jobs=jobs)
        assert plan.destroy == () and plan.fixes == ()
    assert _same(await _now(workspace, row.id), before) and frame.ref in fake_frames.world


# ── _apply: compare-and-set, and a busy row waits for the next pass (fix round 1, M4) ─────


async def test_a_fix_the_row_moved_past_is_refused(fake_frames: Any, silent_dispatcher: None) -> None:
    five_min_ago = utcnow() - dt.timedelta(minutes=5)
    workspace, row = await _row(MachineState.RUNNING, epoch=3, since=five_min_ago)
    await _hold(workspace, row, step_holder("taken-over", 2), frame_ref="machine-x-3")
    before = await _now(workspace, row.id)
    read = reconcile_ops.view_of(before)
    for judged in (
        replace(read, held_by=step_holder("taken-over", 1)),  # judged dead; a newer attempt took over
        replace(read, state=MachineState.PARKED),
        replace(read, state_since=five_min_ago - dt.timedelta(seconds=1)),  # it moved on (a wake's progress)
        replace(read, epoch=2),  # an older epoch
    ):
        await reconcile_ops._apply(RowFix.of(judged, MachineState.GONE))  # pyright: ignore[reportPrivateUsage]
        await reconcile_ops._apply(  # pyright: ignore[reportPrivateUsage]
            RowFix.of(judged, MachineState.IDLE, keep_frame=True, release_hold=True)
        )
    assert _same(await _now(workspace, row.id), before)


async def test_a_busy_row_is_skipped_for_the_next_pass(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(common, "LOCK_WAIT_S", 0.3)
    workspace, row = await _row(MachineState.IDLE, epoch=1, since=utcnow() - dt.timedelta(minutes=5))
    before = await _now(workspace, row.id)
    async with session_scope(workspace_id=workspace.id) as holder:
        assert await Machine.locked(holder, row.id) is not None  # a step's transaction holds the row
        started = time.monotonic()
        fix = RowFix.of(reconcile_ops.view_of(before), MachineState.GONE)
        await reconcile_ops._apply(fix)  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - started < 3.0  # never a hang
    assert _same(await _now(workspace, row.id), before)


# ── the orphan arm ────────────────────────────────────────────────────────────────────────


async def test_the_orphan_arm_deletes_only_old_unreferenced_objects(silent_dispatcher: None) -> None:
    ghost = uuid.uuid4()
    frame = FakeFrame("g")
    await frame.create()
    await frame.write_bytes("/work/a", b"1")
    await store.save_work(frame, f"machines/{ghost}/1.tar.gz", cap=1_000_000)
    deleted = await machines.orphans_once(min_age_s=0, prefix=f"machines/{ghost}/")
    assert deleted == 1
    assert await store.list_prefix(f"machines/{ghost}/") == []


async def test_the_orphan_arm_keeps_referenced_young_and_unclaimed_seed_objects(
    silent_dispatcher: None,
) -> None:
    frame = FakeFrame("g2")
    await frame.create()
    await frame.write_bytes("/work/a", b"1")
    workspace, row = await _row(MachineState.ASLEEP)
    kept = f"machines/{row.id}/1.tar.gz"
    await store.save_work(frame, kept, cap=1_000_000)
    await _set(workspace, row.id, checkpoint_object=kept)
    seed = f"machines/{row.id}/seed.tar.gz"  # the child's row exists; its seed has not landed yet
    await store.save_work(frame, seed, cap=1_000_000)
    assert await machines.orphans_once(min_age_s=0, prefix=f"machines/{row.id}/") == 0
    assert set(await store.list_prefix(f"machines/{row.id}/")) == {kept, seed}
    young = f"machines/{uuid.uuid4()}/1.tar.gz"  # a sleep's upload its row does not point at yet
    await store.save_work(frame, young, cap=1_000_000)
    assert await machines.orphans_once(prefix=young) == 0  # the daily default: an hour's grace
    assert await store.list_prefix(young) == [young]
    await store.delete_keys([kept, seed, young])


async def test_a_key_a_row_came_to_reference_after_the_index_was_read_is_kept(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 2, O1: the index is read once; by the time of the delete a row may point at the
    key (a sleep at the same epoch re-uploaded it and committed). The delete re-checks the row."""
    frame = FakeFrame("g3")
    await frame.create()
    await frame.write_bytes("/work/a", b"1")
    workspace, row = await _row(MachineState.ASLEEP)
    key = f"machines/{row.id}/2.tar.gz"
    await store.save_work(frame, key, cap=1_000_000)
    await _set(workspace, row.id, checkpoint_object=key)
    index = Machine.object_index.__func__

    async def stale(cls: type[Machine], session: Any) -> tuple[set[uuid.UUID], set[str]]:
        ids, keys = await index(cls, session)
        return ids, keys - {key}  # read before the row pointed at the key

    monkeypatch.setattr(Machine, "object_index", classmethod(stale))
    assert await machines.orphans_once(min_age_s=0, prefix=f"machines/{row.id}/") == 0
    assert await store.list_prefix(f"machines/{row.id}/") == [key]
    await store.delete_keys([key])


async def test_a_key_re_uploaded_after_the_listing_is_kept(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 2, O1: the listing's LastModified is old, but the object was written again since
    (a sleep re-uploading its epoch's key before it commits): its age is re-read before the delete."""
    frame = FakeFrame("g4")
    await frame.create()
    await frame.write_bytes("/work/a", b"1")
    owner = uuid.uuid4()
    key = f"machines/{owner}/1.tar.gz"
    await store.save_work(frame, key, cap=1_000_000)
    old = utcnow() - dt.timedelta(days=2)
    monkeypatch.setattr(storage, "list_objects", lambda prefix, bucket=None: [(key, old)])
    assert await machines.orphans_once(prefix=f"machines/{owner}/") == 0
    assert await store.list_prefix(f"machines/{owner}/") == [key]
    await store.delete_keys([key])


async def test_the_orphan_arm_refuses_a_prefix_outside_machines() -> None:
    """Fix round 2: an empty prefix would sweep the whole snapshots bucket."""
    for prefix in ("", "artifacts/", "machinesX/"):
        with pytest.raises(ValueError):
            await machines.orphans_once(prefix=prefix)


# ── every throwaway frame is labelled (fix round 1, M4) ───────────────────────────────────


class _Stop(Exception):
    """The recorder's: the test learned the labels; nothing past `create` needs to run."""


class _Recorder:
    made: ClassVar[list[dict[str, str]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def create(self, *args: Any, labels: Any = None, **kwargs: Any) -> None:
        _Recorder.made.append(dict(labels or {}))
        raise _Stop

    async def destroy(self) -> None:
        return None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str]]]:
    import prismoid.ai.frames as frames
    from prismoid.operations import verification

    _Recorder.made = []
    for module in (frames, verification):
        monkeypatch.setattr(module, "new_frame", _Recorder)
        monkeypatch.setattr(module, "ensure_ready", lambda profile="base": None)
    yield _Recorder.made


async def _task_with_step() -> tuple[Task, PlanStep]:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="labels")
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind="step",
            state="running",
            title="w",
            spec={},
            attempt=2,
        )
        s.add(step)
        await s.commit()
        return task, step


async def test_a_qa_frame_wears_its_task_step_and_attempt(recorder: list[dict[str, str]]) -> None:
    from prismoid.operations import qa

    task, step = await _task_with_step()
    digest = storage.put(b"# memo\n")
    deliverable = Artifact(
        id=uuid.uuid4(),
        task_id=task.id,
        role="deliverable",
        sha256=digest,
        provenance={"path": "/artifacts/memo.md"},
    )
    async with session_scope(workspace_id=task.workspace_id) as s:
        live_task, live_step = await s.get(Task, task.id), await s.get(PlanStep, step.id)
        assert live_task is not None and live_step is not None
        await qa.review(s, live_task, live_step, StepSpec(), [deliverable])  # never raises: PASS_UNREAD
    assert recorder == [
        {
            LABEL_KIND: "qa",
            LABEL_WORKSPACE: str(task.workspace_id),
            LABEL_TASK: str(task.id),
            LABEL_STEP: str(step.id),
            LABEL_ATTEMPT: "2",
        }
    ]


async def test_a_verify_frame_wears_its_task(recorder: list[dict[str, str]]) -> None:
    from prismoid.operations import verification

    task, _ = await _task_with_step()
    body = b"# memo\n"
    digest = storage.put(body)
    async with session_scope(workspace_id=task.workspace_id) as s:
        artifact = Artifact(
            id=uuid.uuid4(),
            lineage_id=uuid.uuid4(),
            task_id=task.id,
            type="document",
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
        s.add(artifact)
        await s.flush()
        with pytest.raises(_Stop):
            await verification.verify_artifact(s, artifact)
    assert recorder == [
        {LABEL_KIND: "verify", LABEL_WORKSPACE: str(task.workspace_id), LABEL_TASK: str(task.id)}
    ]


async def test_a_figures_frame_wears_its_task(recorder: list[dict[str, str]]) -> None:
    from prismoid.operations import figures

    task, step = await _task_with_step()
    async with session_scope(workspace_id=task.workspace_id) as s:
        live_task, live_step = await s.get(Task, task.id), await s.get(PlanStep, step.id)
        assert live_task is not None and live_step is not None
        with pytest.raises(_Stop):
            await figures._publish_batch(s, live_task, live_step, [("f1", b"png")], 1)  # pyright: ignore[reportPrivateUsage]
    assert recorder == [
        {LABEL_KIND: "figures", LABEL_WORKSPACE: str(task.workspace_id), LABEL_TASK: str(task.id)}
    ]


async def test_frame_drivers_are_the_drivers_rows_name_a_frame_on() -> None:
    """Only a row that names a frame makes its driver one to list: a retired driver whose Machines
    all sleep on archives never makes a pass ask a provider that may be gone."""
    workspace, named = await _row(MachineState.IDLE, frame_ref="machine-x-1")
    await _set(workspace, named.id, driver="zz-named")
    workspace, asleep = await _row(MachineState.ASLEEP)
    await _set(workspace, asleep.id, driver="zz-asleep")
    async with session_scope(platform=True) as s:
        found = await _REAL_FRAME_DRIVERS(s)
    assert "zz-named" in found and "zz-asleep" not in found


async def test_the_reconciler_lists_every_driver_its_rows_hold_a_frame_on(
    fake_frames: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that switched drivers keeps Machines on the old one; their
    frames are listed and judged, never taken for lost because this worker makes frames elsewhere."""
    workspace, row = await _row(MachineState.IDLE, since=utcnow() - dt.timedelta(minutes=5))
    frame = await _machine_frame(SandbarFakeFrame, row, 1)
    await _set(workspace, row.id, frame_ref=frame.ref, driver="sandbar")
    asked: list[set[str]] = []
    fake = frames.inventory

    async def listing(drivers: Any = None) -> list[FrameInfo]:
        asked.append(set(drivers))
        return await fake(drivers)

    monkeypatch.setattr(frames, "inventory", listing)
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert asked == [{"docker"}, {"sandbar"}], "one listing per driver"
    assert plan.destroy == () and plan.fixes == ()


async def test_a_disowned_frame_is_destroyed_by_the_driver_that_listed_it(
    fake_frames: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    nobody = uuid.uuid4()
    orphan = FrameInfo(
        ref=f"machine-{nobody}-1",
        labels={LABEL_KIND: "machine", LABEL_MACHINE: str(nobody), LABEL_EPOCH: "1"},
        created_at=utcnow(),
        driver="sandbar",
    )
    gone: list[tuple[str, str | None]] = []

    async def listing(drivers: Any = None) -> list[FrameInfo]:
        return [orphan]

    async def destroy_ref(ref: str, *, driver: str | None = None) -> None:
        gone.append((ref, driver))

    monkeypatch.setattr(frames, "inventory", listing)
    monkeypatch.setattr(frames, "destroy_ref", destroy_ref)
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert plan.destroy == (orphan.ref,) and gone == [(orphan.ref, "sandbar")]


async def test_one_ref_on_two_drivers_is_destroyed_only_where_it_is_disowned(
    fake_frames: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same frame name on two providers is two frames: the old docker copy is past its wall
    clock, the fresh sandbar one is not - only the first is destroyed, on its own driver."""
    labels = {LABEL_KIND: "seat", LABEL_STEP: str(uuid.uuid4()), LABEL_ATTEMPT: "1"}
    old = FrameInfo(
        ref="seat-same", labels=labels, created_at=utcnow() - dt.timedelta(days=3), driver="docker"
    )
    fresh = FrameInfo(ref="seat-same", labels=labels, created_at=utcnow(), driver="sandbar")
    gone: list[tuple[str, str | None]] = []

    async def listing(drivers: Any = None) -> list[FrameInfo]:
        return [old, fresh]

    async def destroy_ref(ref: str, *, driver: str | None = None) -> None:
        gone.append((ref, driver))

    monkeypatch.setattr(frames, "inventory", listing)
    monkeypatch.setattr(frames, "destroy_ref", destroy_ref)
    await machines.reconcile_once(frozenset(), frozenset())
    assert gone == [("seat-same", "docker")]


async def test_a_driver_that_does_not_answer_is_skipped_loudly_and_the_rest_are_still_judged(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This replaces the pin that one driver failing its listing fails the whole pass: a warm row on a driver
    that cannot answer cannot leave its warm state (its sleep binds through that driver), so the stall could
    last forever, and it took every healthy driver's hygiene with it. A listing is per driver now. sandbar
    does not answer: the row naming a frame on it is left exactly as it is and nothing is destroyed on it,
    with a warning naming it - while Docker, which answered, still has its disowned frame destroyed. A
    frame-bearing row on Docker is left too while sandbar is silent (its frame might be on sandbar: a stale
    stamp), and judged lost on the pass where every driver answers."""
    from prismoid.ai.frames import docker_driver, sandbar_driver

    away, on_sandbar = await _row(
        MachineState.IDLE, since=utcnow() - dt.timedelta(minutes=5), frame_ref="machine-y-1"
    )
    await _set(away, on_sandbar.id, driver="sandbar")
    here, lost = await _row(
        MachineState.IDLE, since=utcnow() - dt.timedelta(minutes=5), frame_ref="machine-z-1"
    )
    nobody = uuid.uuid4()
    orphan = FrameInfo(
        ref=f"machine-{nobody}-1",
        labels={LABEL_KIND: "machine", LABEL_MACHINE: str(nobody), LABEL_EPOCH: "1"},
        created_at=utcnow(),
        driver="docker",
    )

    async def down() -> list[FrameInfo]:
        raise ConnectionError("sandbar did not answer")

    monkeypatch.setattr(frames, "inventory", _REAL_INVENTORY)
    monkeypatch.setattr(docker_driver, "inventory", lambda: [orphan])
    monkeypatch.setattr(sandbar_driver, "inventory", down)
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    said = _warned(monkeypatch)
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert fake_frames.destroyed == [(orphan.ref, "docker")]
    assert plan.fixes == ()
    kept = await _now(away, on_sandbar.id)
    assert (kept.state, kept.frame_ref, kept.driver) == (MachineState.IDLE, "machine-y-1", "sandbar")
    assert (await _now(here, lost.id)).state == MachineState.IDLE
    assert [w["driver"] for w in said if "driver" in w] == ["sandbar"]


async def test_a_row_naming_a_driver_this_worker_does_not_know_is_skipped_not_a_failed_pass(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row naming a driver the factory does not know (a retired one, a rollback
    past a newer driver's rows, a dev box's leftover test row) does not fail the pass. The factory
    refuses the name; the pass skips it with a warning and runs silent: no frame-bearing row gets
    a fix that drops its frame while it is skipped, the rest is judged as ever."""
    from prismoid.ai.frames import docker_driver

    workspace, row = await _row(
        MachineState.IDLE, since=utcnow() - dt.timedelta(minutes=5), frame_ref="machine-q-1"
    )
    await _set(workspace, row.id, driver="zz-named")
    monkeypatch.setattr(frames, "inventory", _REAL_INVENTORY)
    monkeypatch.setattr(docker_driver, "inventory", lambda: [])
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    said = _warned(monkeypatch)
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert plan.fixes == () and plan.disowned == () and fake_frames.destroyed == []
    kept = await _now(workspace, row.id)
    assert (kept.state, kept.frame_ref) == (MachineState.IDLE, "machine-q-1")
    assert [w["driver"] for w in said if "driver" in w] == ["zz-named"]


async def test_a_stale_driver_stamp_is_re_stamped_where_its_frame_runs_and_never_destroyed(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork the API stamped `docker` (its default) was woken on a sandbar worker
    before the row named its driver, so its row names a sandbar frame under `docker` - and on a sandbar deployment
    Docker does not answer at all. sandbar lists the frame, wearing this Machine's labels, so the
    row is re-stamped `sandbar`: a logged row fix, never a destroy, and its next bind goes through
    sandbar. An archive sleeper on the same stale stamp names no frame, so it is left alone."""
    monkeypatch.setattr(settings.worker, "frame_driver", "sandbar")
    workspace, row = await _row(MachineState.IDLE, since=utcnow() - dt.timedelta(minutes=5))
    frame = await _machine_frame(SandbarFakeFrame, row, 1)
    await _set(workspace, row.id, frame_ref=frame.ref)  # the row still says docker
    asleep_in, asleep = await _row(MachineState.ASLEEP, since=utcnow() - dt.timedelta(minutes=5))
    fake = frames.inventory

    class NoDaemon(Exception):  # docker's `DockerException`: neither a RuntimeError nor an OSError
        pass

    async def listing(drivers: Any = None) -> list[FrameInfo]:
        if "docker" in set(drivers):
            raise NoDaemon("Error while fetching server API version")
        return await fake(drivers)

    monkeypatch.setattr(frames, "inventory", listing)
    said = _warned(monkeypatch)
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert plan.disowned == () and fake_frames.destroyed == []
    assert [(f.machine_id, f.driver) for f in plan.fixes] == [(row.id, "sandbar")]
    healed = await _now(workspace, row.id)
    assert (healed.driver, healed.state, healed.frame_ref, healed.epoch) == (
        "sandbar",
        MachineState.IDLE,
        frame.ref,
        1,
    )
    assert any(w.get("machine") == str(row.id) and w.get("was") == "docker" for w in said)
    left = await _now(asleep_in, asleep.id)
    assert (left.driver, left.state, left.frame_ref) == ("docker", MachineState.ASLEEP, None)

    async with session_scope(workspace_id=workspace.id) as s:
        step = PlanStep(
            id=uuid.uuid4(), task_id=row.task_id, ordinal=0, kind="step", state="running", title="w", spec={}
        )
        s.add(step)
        await s.commit()
    fake_frames.made.clear()
    async with session_scope(workspace_id=workspace.id) as s:
        task = await s.get(Task, row.task_id)
        live = await s.get(PlanStep, step.id)
        assert task is not None and live is not None
        got = await machines.acquire(s, task, live, profile="base", allow_domains=[])
    assert got.frame.ref == frame.ref and fake_frames.made == [("sandbar", frame.ref)]
    await machines.release(got, parked=False)
