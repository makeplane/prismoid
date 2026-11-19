# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A device Machine's life: nothing is archived, so a sleep
stops what the task left running and keeps the frame, and a wake binds it - never a rebuild from a
checkpoint. The task names its driver (`tasks.machine_driver`), which wins over the worker's, and
the row names the device it runs on. `NoneSleepFakeFrame` stands in for `DeviceFrame`. The rig is
`tests/support/rigs/device_lifecycle_rig.py`."""

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from prismoid.ai import frames
from prismoid.api.serializers.machines import machine_out
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import session_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import meter
from prismoid.operations.machines.sleep import Forgotten, forget
from prismoid.schemas.machines import MachineState, SleepReason
from tests.support.cast import member
from tests.support.fakes.fake_frames import DEVICE, FakeFrame, NoneSleepFakeFrame
from tests.support.rigs.device_lifecycle_rig import (
    MINE,
    REAL_INVENTORY,
    lifecycle_acquire,
    lifecycle_asleep,
    lifecycle_machine,
    task_and_step,
)
from tests.support.rigs.device_lifecycle_rig import cloud_worker as cloud_worker
from tests.support.rigs.device_lifecycle_rig import only_our_rows as only_our_rows
from tests.support.rigs.device_lifecycle_rig import quick_wait as quick_wait

pytestmark = pytest.mark.integration


async def test_none_sleep_writes_no_checkpoint_and_keeps_ref(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    """The work is the user's real folder: a sleep stops the task's leftovers, keeps the frame and
    its files, and moves the row to asleep naming it - no freeze, no archive, no object."""
    workspace, tid, sid = await task_and_step()
    first, row = await lifecycle_asleep(workspace.id, tid, sid)
    live = fake_frames.world[first.frame.ref]
    assert live.calls == ["stop_services"], "stopped, never frozen or archived"
    assert live.files["/work/notes.md"][0] == b"the user's own file"
    assert row.state == MachineState.ASLEEP and row.frame_ref == first.frame.ref
    assert row.checkpoint_object is None and row.browser_object is None and row.gone_reason is None
    assert row.metered_until is None, "an asleep device is not warm"


async def test_a_deleted_tasks_forget_takes_its_quarantine_too(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quarantine outlives a frame and goes with the task; a cloud frame has none."""
    forgotten: list[str] = []

    async def spy(self: NoneSleepFakeFrame) -> None:
        forgotten.append(str(self.ref))

    async def nothing(_prefix: str) -> None:
        return None

    monkeypatch.setattr(NoneSleepFakeFrame, "forget_quarantine", spy)
    monkeypatch.setattr("prismoid.operations.machines.store.delete_prefix", nothing)
    await forget(Forgotten(uuid.uuid4(), "device-gone", DEVICE))
    await forget(Forgotten(uuid.uuid4(), "machine-gone", "docker"))
    assert forgotten == ["device-gone"]
    assert ("device-gone", DEVICE) in fake_frames.destroyed


async def test_none_wake_binds_never_rebuilds(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    workspace, tid, sid = await task_and_step()
    first, asleep = await lifecycle_asleep(workspace.id, tid, sid)
    again = await lifecycle_acquire(workspace.id, tid, sid, attempt=2)
    assert again.woke and again.frame.ref == first.frame.ref
    assert fake_frames.created == [first.frame.ref], "bound, never rebuilt"
    assert await again.frame.read_bytes("/work/notes.md") == b"the user's own file"
    row = await lifecycle_machine(workspace.id, tid)
    assert row.state == MachineState.RUNNING and row.epoch == asleep.epoch + 1
    assert row.frame_ref == first.frame.ref and row.driver == DEVICE


async def test_none_wake_gone_creates_fresh_frame(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    """The helper has never heard of the frame (its Prismoid folder went): the wake makes a fresh frame
    of the same kind - on the device, never on the worker's driver - with no checkpoint to load.
    The helper names it as it named the first (`device-{task_id}`)."""
    workspace, tid, sid = await task_and_step()
    first, _ = await lifecycle_asleep(workspace.id, tid, sid)
    del fake_frames.world[first.frame.ref]
    again = await lifecycle_acquire(workspace.id, tid, sid, attempt=2)
    assert again.woke and again.frame.ref == first.frame.ref == f"device-{tid}"
    assert isinstance(again.frame, NoneSleepFakeFrame)
    assert fake_frames.created == [first.frame.ref, again.frame.ref]
    assert "/work/notes.md" not in fake_frames.world[again.frame.ref].files, "a fresh frame"
    row = await lifecycle_machine(workspace.id, tid)
    assert row.state == MachineState.RUNNING and row.frame_ref == again.frame.ref and row.driver == DEVICE


async def test_none_wake_unreachable_stays_asleep_naming_its_frame(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    """A helper that is down is away, not gone: one infra attempt, and the Machine stays as the
    claim found it - asleep, naming its frame, holding nobody. Nothing is rebuilt or destroyed."""
    workspace, tid, sid = await task_and_step()
    first, asleep = await lifecycle_asleep(workspace.id, tid, sid)
    FakeFrame.unreachable = True
    with pytest.raises(ModelIncapable):
        await lifecycle_acquire(workspace.id, tid, sid, attempt=2)
    row = await lifecycle_machine(workspace.id, tid)
    assert row.state == MachineState.ASLEEP and row.held_by is None
    assert row.frame_ref == asleep.frame_ref == first.frame.ref
    assert fake_frames.created == [first.frame.ref] and first.frame.ref in fake_frames.world


async def test_task_machine_driver_picks_driver_over_worker_setting(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    workspace, tid, sid = await task_and_step()
    got = await lifecycle_acquire(workspace.id, tid, sid)
    assert isinstance(got.frame, NoneSleepFakeFrame)
    assert [driver for driver, _ in fake_frames.made] == [DEVICE], "made through the task's driver"
    assert (await lifecycle_machine(workspace.id, tid)).driver == DEVICE

    workspace, tid, sid = await task_and_step(driver=None)  # a cloud task keeps the worker's
    got = await lifecycle_acquire(workspace.id, tid, sid)
    assert not isinstance(got.frame, NoneSleepFakeFrame)
    assert (await lifecycle_machine(workspace.id, tid)).driver == "docker"


async def test_create_task_names_the_device_only_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as s:
        actor, _ = await member(s, "Ada")
        cloud = await task_ops.create_task(s, actor, goal="in the cloud")
        monkeypatch.setattr(settings, "environment", "local")  # local mode makes "local" a valid value
        local = await task_ops.create_task(s, actor, goal="on my Mac")
        assert (cloud.machine_driver, local.machine_driver) == (None, DEVICE)


async def test_acquire_records_device_name_and_machine_out_shows_it(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    workspace, tid, sid = await task_and_step()
    await machines.release(await lifecycle_acquire(workspace.id, tid, sid), parked=False)
    assert (await lifecycle_machine(workspace.id, tid)).device_name == "Test Mac"
    async with session_scope(workspace_id=workspace.id) as s:
        found = await meter.summary(s, tid)
    assert found is not None and machine_out(found).device_name == "Test Mac"
    assert machine_out(found).driver == "device", "the page shows its browser as a window there"

    workspace, tid, sid = await task_and_step(driver=None)  # a cloud driver names no device
    await machines.release(await lifecycle_acquire(workspace.id, tid, sid), parked=False)
    assert (await lifecycle_machine(workspace.id, tid)).device_name is None
    async with session_scope(workspace_id=workspace.id) as s:
        found = await meter.summary(s, tid)
    assert found is not None and machine_out(found).device_name is None
    assert machine_out(found).driver is None


async def _device_row(state: MachineState, ref: str) -> tuple[Any, Machine]:
    workspace, tid, _ = await task_and_step()
    async with session_scope(workspace_id=workspace.id) as s:
        row = Machine(
            workspace_id=workspace.id,
            task_id=tid,
            driver=DEVICE,
            profile="base",
            state=state,
            state_since=utcnow() - dt.timedelta(minutes=5),
            last_active_at=utcnow() - dt.timedelta(minutes=5),
            epoch=1,
            frame_ref=ref,
        )
        s.add(row)
        await s.commit()
        MINE.add(row.id)
        return workspace, row


async def test_reconciler_leaves_device_rows_when_helper_silent(
    fake_frames: Any,
    silent_dispatcher: None,
    cloud_worker: None,
    only_our_rows: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The real device listing, with no helper running (no key, no socket): the driver is silent,
    so no device row loses its frame - an asleep one keeps naming it, an idle one stays warm."""
    from prismoid.ai.frames import docker_driver

    monkeypatch.setattr(settings.local, "home", tmp_path)
    monkeypatch.setattr(frames, "inventory", REAL_INVENTORY)
    monkeypatch.setattr(docker_driver, "inventory", lambda: [])
    asleep_ws, asleep = await _device_row(MachineState.ASLEEP, "device-a")
    idle_ws, idle = await _device_row(MachineState.IDLE, "device-b")
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert plan.fixes == () and plan.disowned == () and fake_frames.destroyed == []
    kept = await lifecycle_machine(asleep_ws.id, asleep.task_id)
    assert (kept.state, kept.frame_ref) == (MachineState.ASLEEP, "device-a")
    warm = await lifecycle_machine(idle_ws.id, idle.task_id)
    assert (warm.state, warm.frame_ref) == (MachineState.IDLE, "device-b")


async def test_device_segment_bills_zero(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    """Warm seconds on the device are metered as anywhere (`Unit.SECOND`, family `machine`), under
    provider `device`, at the price the conf gives it: 0."""
    workspace, tid, sid = await task_and_step()
    got = await lifecycle_acquire(workspace.id, tid, sid)
    async with session_scope(workspace_id=workspace.id) as s:
        row = await Machine.locked_for_task(s, tid)
        assert row is not None and row.metered_until is not None
        row.metered_until -= dt.timedelta(seconds=120)
        await s.commit()
    segment = await machines.release(got, parked=False)
    assert segment is not None and segment.seconds >= 120 and segment.driver == DEVICE
    async with session_scope(workspace_id=workspace.id) as s:
        await machines.bill(s, segment)
        await s.commit()
        rows = (await s.scalars(select(UsageEvent).where(UsageEvent.task_id == tid))).all()
        assert [(u.provider, u.family, u.units["second"] >= 120) for u in rows] == [(DEVICE, "machine", True)]
        assert rows[0].cost_usd == Decimal(0)


async def _move_row(workspace_id: uuid.UUID, machine_id: uuid.UUID, state: MachineState) -> None:
    """Another transition took the row: a newer epoch, in `state` (a reset, a reconciler's fix)."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        row.epoch += 1
        row.move(state, utcnow())
        row.held_by = row.held_since = None
        await s.commit()


async def test_a_wake_whose_row_moved_on_leaves_the_device_frame(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_wake`'s compare-and-set loses (the row moved to another epoch while it bound): the step
    fails as infra, and the frame - the task's own, whichever epoch woke it - stays."""
    from prismoid.operations.machines import wake as machine_wake

    workspace, tid, sid = await task_and_step()
    first, asleep = await lifecycle_asleep(workspace.id, tid, sid)
    real = machine_wake._progressed  # pyright: ignore[reportPrivateUsage]

    async def raced(plan: Any, holder: str) -> None:
        await real(plan, holder)
        await _move_row(workspace.id, asleep.id, MachineState.WAKING)

    monkeypatch.setattr(machine_wake, "_progressed", raced)
    with pytest.raises(ModelIncapable, match="changed while it woke"):
        await lifecycle_acquire(workspace.id, tid, sid, attempt=2)
    assert fake_frames.world[first.frame.ref].files["/work/notes.md"][0] == b"the user's own file"
    assert fake_frames.destroyed == []


async def test_a_rebuild_that_fails_over_the_kept_frame_leaves_it_and_its_work(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base device Machine meets a browser step: the wake makes the browser frame, which the
    helper puts over the same folders (`device-{task_id}`). It fails after the create: `_give_up`
    leaves that frame - it IS the kept one - and the row goes back asleep naming it."""
    from prismoid.operations.machines import wake as machine_wake

    workspace, tid, sid = await task_and_step()
    first, _ = await lifecycle_asleep(workspace.id, tid, sid)

    async def broken(plan: Any, holder: str) -> None:
        raise RuntimeError("the wake lost its way")

    monkeypatch.setattr(machine_wake, "_progressed", broken)
    with pytest.raises(ModelIncapable, match="could not start"):
        await lifecycle_acquire(workspace.id, tid, sid, attempt=2, profile="browser")
    assert fake_frames.world[first.frame.ref].files["/work/notes.md"][0] == b"the user's own file"
    row = await lifecycle_machine(workspace.id, tid)
    assert (row.state, row.frame_ref, row.profile) == (MachineState.ASLEEP, first.frame.ref, "base")


async def test_a_wake_that_changes_profile_lands_on_the_same_frame_and_destroys_nothing(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    """Review Minor 6: the frame the rebuild made has the ref the row named (`native_ref ==
    frame.ref`), so nothing is destroyed as "the computer it left"."""
    workspace, tid, sid = await task_and_step()
    first, _ = await lifecycle_asleep(workspace.id, tid, sid)
    again = await lifecycle_acquire(workspace.id, tid, sid, attempt=2, profile="browser")
    assert again.woke and again.frame.ref == first.frame.ref
    assert await again.frame.read_bytes("/work/notes.md") == b"the user's own file"
    assert fake_frames.destroyed == [] and first.frame.ref in fake_frames.world


@pytest.mark.parametrize("fails", [False, True], ids=["finishes", "fails"])
async def test_a_sleep_whose_row_moved_on_leaves_the_device_frame(
    fake_frames: Any,
    silent_dispatcher: None,
    cloud_worker: None,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    """The row went to a newer epoch while the sleep stopped its services - with the sleep then
    finishing, or failing: on an archive driver the frame is debris; a device frame is the task's."""
    workspace, tid, sid = await task_and_step()
    first = await lifecycle_acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/notes.md", b"the user's own file")
    await machines.release(first, parked=False)
    row = await lifecycle_machine(workspace.id, tid)

    async def raced(self: NoneSleepFakeFrame) -> None:
        await _move_row(workspace.id, row.id, MachineState.ASLEEP)
        if fails:
            raise RuntimeError("the helper refused")

    monkeypatch.setattr(NoneSleepFakeFrame, "stop_services", raced)
    assert await machines.sleep(row.id, row.epoch, SleepReason.MANUAL) == "gone"
    assert fake_frames.world[first.frame.ref].files["/work/notes.md"][0] == b"the user's own file"


async def test_the_reconciler_puts_a_stuck_device_wake_back_to_sleep_and_keeps_its_frame(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None, only_our_rows: None
) -> None:
    """The review's Important: a worker died between `_claim`'s commit and `_wake`'s compare-and-
    set, leaving a `waking` row naming the kept frame. The helper answers and lists it. Past
    `stuck_s` the row goes back asleep naming it - never `gone` - and nothing is destroyed. A device
    frame no row owns at all is still destroyed."""
    from prismoid.schemas.machines import LABEL_EPOCH, LABEL_KIND, LABEL_MACHINE, step_holder

    workspace, row = await _device_row(MachineState.WAKING, "")
    kept = NoneSleepFakeFrame(str(row.task_id))
    await kept.create(labels={LABEL_KIND: "machine", LABEL_MACHINE: str(row.id), LABEL_EPOCH: "1"})
    await kept.write_bytes("/work/notes.md", b"the user's own file")
    async with session_scope(workspace_id=workspace.id) as s:
        fresh = await s.get(Machine, row.id)
        assert fresh is not None
        fresh.epoch, fresh.frame_ref = 2, kept.ref
        fresh.held_by, fresh.held_since = step_holder(str(uuid.uuid4()), 1), fresh.state_since
        fresh.state_since = utcnow() - dt.timedelta(seconds=settings.machines.stuck_s + 60)
        await s.commit()
    nobody = uuid.uuid4()
    orphan = NoneSleepFakeFrame(str(uuid.uuid4()))
    await orphan.create(labels={LABEL_KIND: "machine", LABEL_MACHINE: str(nobody), LABEL_EPOCH: "1"})

    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert [f.ref for f in plan.disowned] == [orphan.ref], "only the frame no row owns"
    assert fake_frames.destroyed == [(orphan.ref, DEVICE)]
    after = await lifecycle_machine(workspace.id, row.task_id)
    assert (after.state, after.frame_ref) == (MachineState.ASLEEP, kept.ref)
    assert fake_frames.world[kept.ref].files["/work/notes.md"][0] == b"the user's own file"


async def test_a_device_seat_bills_under_the_device_at_zero(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None
) -> None:
    workspace, tid, sid = await task_and_step()
    got = await lifecycle_acquire(workspace.id, tid, sid, seat=True)
    got.started -= 90
    segment = await machines.release(got, parked=False)
    assert segment is not None and segment.driver == DEVICE and segment.seconds >= 90
    async with session_scope(workspace_id=workspace.id) as s:
        await machines.bill(s, segment)
        await s.commit()
        rows = (await s.scalars(select(UsageEvent).where(UsageEvent.task_id == tid))).all()
        assert [(u.provider, u.cost_usd) for u in rows] == [(DEVICE, Decimal(0))]


async def _stuck_waking(workspace_id: uuid.UUID, machine_id: uuid.UUID, **values: Any) -> None:
    """The worker died after `_claim`'s commit: the row sits `waking`, under a dead hold, past
    `stuck_s`."""
    from prismoid.schemas.machines import step_holder

    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        for name, value in values.items():
            setattr(row, name, value)
        row.held_by, row.held_since = step_holder(str(uuid.uuid4()), 1), utcnow()
        row.state_since = utcnow() - dt.timedelta(seconds=settings.machines.stuck_s + 60)
        await s.commit()


async def test_a_stuck_wake_that_names_no_device_frame_goes_gone_and_destroys_nothing(
    fake_frames: Any, silent_dispatcher: None, only_our_rows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5, replacing round 2's adoption (`frame_ref or` the listed frame), which could hand a
    Reset's old frame back: a `waking` device row naming no frame woke from `gone` (a Reset, a lost
    frame, or `_rewake` after its frame went), so past `stuck_s` it goes back to `gone` - while the
    helper lists a frame wearing this Machine's labels. Nothing is destroyed: the reconciler never
    takes a persistent frame while its row exists, so it waits for the next wake's `_clear_gone` or
    for `forget`. The pass asks the device because the worker's own driver is the device (local
    mode): a row naming no frame makes no pass ask its driver."""
    from prismoid.schemas.machines import LABEL_EPOCH, LABEL_KIND, LABEL_MACHINE

    monkeypatch.setattr(settings.worker, "frame_driver", DEVICE)

    workspace, row = await _device_row(MachineState.WAKING, "")
    kept = NoneSleepFakeFrame(str(row.task_id))
    await kept.create(labels={LABEL_KIND: "machine", LABEL_MACHINE: str(row.id), LABEL_EPOCH: "1"})
    await kept.write_bytes("/work/notes.md", b"the user's own file")
    await _stuck_waking(workspace.id, row.id, epoch=2, frame_ref=None)
    for _ in range(2):
        plan = await machines.reconcile_once(frozenset(), frozenset())
        assert plan.disowned == ()
    assert fake_frames.destroyed == []
    after = await lifecycle_machine(workspace.id, row.task_id)
    assert (after.state, after.frame_ref) == (MachineState.GONE, None), "never given the frame"
    assert kept.ref in fake_frames.world, "nor destroyed while its row exists"


async def test_a_profile_change_claim_keeps_the_device_frame_named_and_a_crash_after_it_loses_nothing(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None, only_our_rows: None
) -> None:
    """The shape `_claim` leaves now: an asleep base device Machine claimed for a browser step stays
    naming its frame, with its own profile, while it wakes. The worker dies there; two reconciler
    passes later the row is asleep as it was - base, naming the frame - and the frame is whole."""
    from prismoid.operations.machines import acquired, lifecycle
    from prismoid.schemas.machines import step_holder

    workspace, tid, sid = await task_and_step()
    first, asleep = await lifecycle_asleep(workspace.id, tid, sid)
    MINE.add(asleep.id)
    async with session_scope(workspace_id=workspace.id) as session:
        task = await session.get(Task, tid)
        step = await session.get(PlanStep, sid)
        assert task is not None and step is not None
        step.attempt = 2
        claim = lifecycle._claim  # pyright: ignore[reportPrivateUsage]
        wake = await claim(session, task, step, step_holder(str(sid), 2), "browser", None)
    assert isinstance(wake, acquired.WakePlan)
    assert wake.profile == "browser" and wake.native_ref == first.frame.ref and not wake.resume
    claimed = await lifecycle_machine(workspace.id, tid)
    assert (claimed.state, claimed.frame_ref, claimed.profile) == (
        MachineState.WAKING,
        first.frame.ref,
        "base",
    )

    await _stuck_waking(workspace.id, asleep.id)  # the worker died right after the claim
    for _ in range(2):
        plan = await machines.reconcile_once(frozenset(), frozenset())
        assert plan.disowned == ()
    assert fake_frames.destroyed == []
    after = await lifecycle_machine(workspace.id, tid)
    assert (after.state, after.frame_ref, after.profile) == (MachineState.ASLEEP, first.frame.ref, "base")
    assert fake_frames.world[first.frame.ref].files["/work/notes.md"][0] == b"the user's own file"


async def test_a_retried_wake_whose_rebuild_fails_leaves_the_row_asleep_naming_its_frame(
    fake_frames: Any, silent_dispatcher: None, cloud_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser step claims an asleep base device Machine and its worker dies (the row: `waking`,
    still naming the frame). The step's next attempt re-claims it - a dead attempt's `waking` row on
    a persistent driver names its frame like an asleep one - and its rebuild fails: the row goes
    back asleep naming the frame, base, and the frame and its work are whole."""
    from prismoid.operations.machines import lifecycle
    from prismoid.operations.machines import wake as machine_wake
    from prismoid.schemas.machines import step_holder

    workspace, tid, sid = await task_and_step()
    first, _ = await lifecycle_asleep(workspace.id, tid, sid)
    async with session_scope(workspace_id=workspace.id) as session:
        task = await session.get(Task, tid)
        step = await session.get(PlanStep, sid)
        assert task is not None and step is not None
        claim = lifecycle._claim  # pyright: ignore[reportPrivateUsage]
        await claim(
            session, task, step, step_holder(str(sid), step.attempt), "browser", None
        )  # then the crash

    async def broken(plan: Any, holder: str) -> None:
        raise RuntimeError("the wake lost its way")

    monkeypatch.setattr(machine_wake, "_progressed", broken)
    with pytest.raises(ModelIncapable, match="could not start"):
        await lifecycle_acquire(workspace.id, tid, sid, attempt=2, profile="browser")
    row = await lifecycle_machine(workspace.id, tid)
    assert (row.state, row.frame_ref, row.profile) == (MachineState.ASLEEP, first.frame.ref, "base")
    assert fake_frames.destroyed == []
    assert fake_frames.world[first.frame.ref].files["/work/notes.md"][0] == b"the user's own file"


async def test_a_gone_device_row_is_never_given_its_listed_frame_and_a_rowless_frame_still_goes(
    fake_frames: Any, silent_dispatcher: None, only_our_rows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gone means gone (round 4, replacing round 3's adoption, which let a late or failed Reset
    cleanup hand the old work back): a `gone` device row whose frame the helper still lists stays
    `gone` naming nothing, and the frame stays too - a Reset's `cleanup` or the next wake's own
    destroy takes it, never the reconciler while the row exists. The frame of a Machine whose row is
    gone from Postgres is destroyed. The worker's own driver is the device (local mode), so the pass
    asks the helper though no row names a frame."""
    from prismoid.schemas.machines import LABEL_EPOCH, LABEL_KIND, LABEL_MACHINE

    monkeypatch.setattr(settings.worker, "frame_driver", DEVICE)

    workspace, row = await _device_row(MachineState.GONE, "")
    kept = NoneSleepFakeFrame(str(row.task_id))
    await kept.create(labels={LABEL_KIND: "machine", LABEL_MACHINE: str(row.id), LABEL_EPOCH: "1"})
    await kept.write_bytes("/work/notes.md", b"the user's own file")
    async with session_scope(workspace_id=workspace.id) as s:
        gone = await s.get(Machine, row.id)
        assert gone is not None
        gone.epoch, gone.frame_ref = 3, None
        await s.commit()
    rowless = NoneSleepFakeFrame(str(uuid.uuid4()))
    await rowless.create(labels={LABEL_KIND: "machine", LABEL_MACHINE: str(uuid.uuid4()), LABEL_EPOCH: "1"})

    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert [f.ref for f in plan.disowned] == [rowless.ref]
    assert fake_frames.destroyed == [(rowless.ref, DEVICE)]
    after = await lifecycle_machine(workspace.id, row.task_id)
    assert (after.state, after.frame_ref, after.profile) == (MachineState.GONE, None, "base")
    assert kept.ref in fake_frames.world, "nor destroyed while its row exists"
