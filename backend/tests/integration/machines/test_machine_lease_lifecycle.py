# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The control lease meets the Machine's lifecycle (ADR-065): its six columns on a real
row; nothing sleeps a Machine under its driver - not the sweep's claim, not cap eviction, not Sleep
now or Reset, not the profile upgrade or the airtight re-create; and a transition to `asleep` or
`gone` - a sleep's finish, a reset, the reconciler, a failed wake - clears the lease in its own
transaction, restarts the idle clock there, and records the ending once (`lost` when the frame went
from under a live lease, as a step that finds its frame dead says too)."""

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, ModelIncapable
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import reconcile as reconcile_ops
from prismoid.operations.machines import store
from prismoid.schemas.machines import MachineState, RowFix, SleepReason, frame_name, machine_prefix
from prismoid.schemas.screen import Lease
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration

CONN = "tab-ada-0001"


def _machine_jobs(dispatcher: RecordingDispatcher) -> list[str]:
    """The Machine's own jobs this test enqueued (`create_task` also enqueues `run_task`)."""
    return [job for job, _ in dispatcher.enqueues if job.startswith("machine_")]


async def _machine(
    *, profile: str = "browser", state: MachineState = MachineState.IDLE, idle_s: int = 0
) -> tuple[Actor, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ada's personal task with a warm Machine at epoch 1: (Ada, her workspace, the task, the Machine)."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="driven")
        then = utcnow() - dt.timedelta(seconds=idle_s)
        row = Machine(
            workspace_id=workspace.id,
            task_id=task.id,
            driver="docker",
            profile=profile,
            state=state,
            state_since=then,
            last_active_at=then,
            epoch=1,
        )
        s.add(row)
        await s.flush()
        row.frame_ref = frame_name(row.id, 1)
        await s.commit()
        return ada, workspace.id, task.id, row.id


async def _drive(
    workspace_id: uuid.UUID, machine_id: uuid.UUID, actor_id: uuid.UUID, *, live: bool = True
) -> Lease:
    """A lease on the row, held for 90 s: live (60 s left), or run out a second ago and not yet
    ended (the lease-end job has not run). Written straight to the row: claiming is tested elsewhere."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        now = await Machine.db_now(s)
        lease = Lease(
            actor_id=actor_id,
            conn=CONN,
            private=False,
            since=now - dt.timedelta(seconds=90),
            until=now + dt.timedelta(seconds=60) if live else now - dt.timedelta(seconds=1),
            cap_at=None,
        )
        row.set_lease(lease)
        await s.commit()
        return lease


async def _fresh(workspace_id: uuid.UUID, machine_id: uuid.UUID) -> Machine:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.current(s, machine_id)
        assert row is not None
        s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def _ended(task_id: uuid.UUID) -> list[dict[str, Any]]:
    """The payloads of the task's `machine.control_ended` events, in order."""
    async with row_scope(Task, task_id) as s:
        events = await s.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
        )
        return [e.payload for e in events.all() if e.type == "machine.control_ended"]


async def test_the_lease_survives_a_commit_and_the_sweep_sees_it(silent_dispatcher: None) -> None:
    ada, workspace_id, _, mid = await _machine()
    assert (await _fresh(workspace_id, mid)).lease() is None
    lease = await _drive(workspace_id, mid, ada.id)
    row = await _fresh(workspace_id, mid)
    assert row.lease() == lease
    view = reconcile_ops.view_of(row)
    assert (view.profile, view.control_since, view.control_until) == ("browser", lease.since, lease.until)


async def test_the_sweep_reads_leased_rows_and_running_browsers(silent_dispatcher: None) -> None:
    ada, workspace_id, _, leased = await _machine(state=MachineState.RUNNING, profile="base")
    await _drive(workspace_id, leased, ada.id)
    _, _, _, browser = await _machine(state=MachineState.RUNNING)
    _, _, _, plain = await _machine(state=MachineState.RUNNING, profile="base")
    async with session_scope(platform=True) as s:
        ids = {r.id for r in await Machine.sweepable(s)}
    assert leased in ids and browser in ids and plain not in ids


async def test_cap_eviction_never_picks_a_leased_machine(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        rows: list[Machine] = []
        for i, hours in enumerate((3, 2, 1)):  # the oldest activity is the first candidate
            task = await task_ops.create_task(s, ada, goal=f"cap {i}")
            then = utcnow() - dt.timedelta(hours=hours)
            row = Machine(
                workspace_id=workspace.id,
                task_id=task.id,
                driver="docker",
                profile="browser",
                state=MachineState.IDLE,
                state_since=then,
                last_active_at=then,
                epoch=1,
                frame_ref=f"machine-cap-{uuid.uuid4().hex[:8]}",
            )
            s.add(row)
            rows.append(row)
        await s.commit()
        oldest, middle, newest = (r.id for r in rows)
    await _drive(workspace.id, oldest, ada.id)  # someone drives it
    await _drive(workspace.id, middle, ada.id, live=False)  # run out; the lease-end job has not run
    async with session_scope(workspace_id=workspace.id) as s:
        victim = await Machine.lru_idle(s)
        assert victim is not None and victim.id == newest


async def test_sleep_now_and_reset_refuse_while_someone_drives(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    ada, workspace_id, task_id, mid = await _machine()
    await _drive(workspace_id, mid, ada.id)
    for press in (machines.request_sleep, machines.reset):
        async with session_scope(workspace_id=workspace_id) as s:
            with pytest.raises(Conflict, match=r"^Ada is driving the browser\.$"):
                await press(s, ada, task_id)
    row = await _fresh(workspace_id, mid)
    assert row.state == MachineState.IDLE and row.lease() is not None
    assert _machine_jobs(recording_dispatcher) == []


async def test_a_run_out_lease_does_not_block_a_reset_and_its_ending_is_recorded(
    recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reset clears a lease whose clock ran out before the lease-end job got to it, in its own
    transaction (`move` to gone), restarts the idle clock there, and says the ending once, by the
    lease's clock - before it enqueues the reset job."""
    ada, workspace_id, task_id, mid = await _machine(idle_s=3600)
    await _drive(workspace_id, mid, ada.id, live=False)
    said_first: list[int] = []
    enqueue = recording_dispatcher.enqueue

    async def watching(job: str, key: str, **kwargs: Any) -> None:
        if job == "machine_reset":
            said_first.append(len(await _ended(task_id)))
        await enqueue(job, key, **kwargs)

    monkeypatch.setattr(recording_dispatcher, "enqueue", watching)
    async with session_scope(workspace_id=workspace_id) as s:
        await machines.reset(s, ada, task_id)
    row = await _fresh(workspace_id, mid)
    assert row.state == MachineState.GONE and row.lease() is None
    assert row.last_active_at == row.state_since  # the ending restarted the idle clock, in the move
    [ended] = await _ended(task_id)
    assert (ended["reason"], ended["actor"]["name"], ended["ended_by"]) == ("expired", "Ada", None)
    assert ended["held_s"] == 89  # held until its clock ran out, not until the reset noticed
    assert said_first == [1]


async def test_sleep_now_keeps_a_run_out_lease_until_the_machine_is_asleep(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sleep now claims a Machine whose lease ran out; the sleep's finish clears the lease with its
    move to `asleep`, restarts the idle clock in that transaction, and says the ending once."""
    ada, workspace_id, task_id, mid = await _machine(profile="base", idle_s=3600)
    frame = fake_frames(str(task_id), profile="base", name=frame_name(mid, 1))
    await frame.create()
    await frame.write_bytes("/work/notes.md", b"kept")
    lease = await _drive(workspace_id, mid, ada.id, live=False)
    sent: list[dict[str, Any]] = []
    enqueue = recording_dispatcher.enqueue

    async def keeping(job: str, key: str, **kwargs: Any) -> None:
        sent.append(kwargs)
        await enqueue(job, key, **kwargs)

    monkeypatch.setattr(recording_dispatcher, "enqueue", keeping)
    async with session_scope(workspace_id=workspace_id) as s:
        await machines.request_sleep(s, ada, task_id)
    row = await _fresh(workspace_id, mid)
    assert row.state == MachineState.SLEEPING and row.lease() == lease  # cleared at `asleep`, not before
    assert _machine_jobs(recording_dispatcher) == ["machine_sleep"]
    assert await _ended(task_id) == []
    kw = sent[-1]
    claim = machines.SleepClaim.from_wire(claim_at=kw["claim_at"], after_s=kw["after_s"], since=kw["since"])
    try:
        assert await machines.sleep(mid, 1, SleepReason.MANUAL, actor_id=ada.id, claim=claim) == "slept"
        row = await _fresh(workspace_id, mid)
        assert row.state == MachineState.ASLEEP and row.lease() is None
        assert row.last_active_at == row.state_since  # the ending restarted the idle clock
        [ended] = await _ended(task_id)
        assert (ended["reason"], ended["actor"]["name"], ended["held_s"]) == ("expired", "Ada", 89)
    finally:
        await store.delete_prefix(machine_prefix(mid))


async def test_the_sweeps_own_claim_never_takes_a_leased_machine(silent_dispatcher: None) -> None:
    ada, workspace_id, _, mid = await _machine(idle_s=3600)
    await _drive(workspace_id, mid, ada.id)
    assert await machines.claim_sleep(mid, 1) is None
    await _drive(workspace_id, mid, ada.id, live=False)
    assert await machines.claim_sleep(mid, 1) is None  # the lease-end job ends it first
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, mid)
        assert row is not None
        row.clear_lease()
        await s.commit()
    assert await machines.claim_sleep(mid, 1) is not None


async def test_a_lost_frame_ends_a_live_lease_as_lost(fake_frames: Any, silent_dispatcher: None) -> None:
    ada, workspace_id, task_id, mid = await _machine(idle_s=600)
    await _drive(workspace_id, mid, ada.id)
    fix = RowFix.of(reconcile_ops.view_of(await _fresh(workspace_id, mid)), MachineState.GONE)
    await reconcile_ops._apply(fix)  # pyright: ignore[reportPrivateUsage]
    row = await _fresh(workspace_id, mid)
    assert row.state == MachineState.GONE and row.lease() is None
    assert row.last_active_at == row.state_since  # the ending restarted the idle clock, in the move
    [ended] = await _ended(task_id)
    assert (ended["reason"], ended["actor"]["name"], ended["ended_by"]) == ("lost", "Ada", None)


# ── the two hand-backs that would sleep a Machine wait for its driver ───────────────────────


async def _task_and_step() -> tuple[Actor, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="machine work")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="work", spec={}
        )
        s.add(step)
        await s.commit()
        return ada, workspace.id, task.id, step.id


async def _acquire(
    workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID, *, attempt: int = 1, **kw: Any
) -> Any:
    async with session_scope(workspace_id=workspace_id) as session:
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = attempt
        kw.setdefault("profile", "base")
        kw.setdefault("allow_domains", [])
        return await machines.acquire(session, task, step, **kw)


async def test_a_profile_upgrade_waits_while_someone_drives(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base Machine meeting a browser step is re-imaged by a sleep and a wake - never under a
    driver: the step waits (bounded by `wait_s`) and the row is left as it was, unclaimed. (A real
    lease needs a browser Machine; the lease is written straight to this base row to reach the
    branch.)"""
    monkeypatch.setattr(settings.machines, "wait_s", 1)
    ada, workspace_id, tid, sid = await _task_and_step()
    first = await _acquire(workspace_id, tid, sid)
    await machines.release(first, parked=False)
    await _drive(workspace_id, first.machine_id, ada.id)
    with pytest.raises(ModelIncapable, match="someone is driving the browser"):
        await _acquire(workspace_id, tid, sid, attempt=2, profile="browser")
    row = await _fresh(workspace_id, first.machine_id)
    assert (row.state, row.held_by, row.profile) == (MachineState.IDLE, None, "base")
    assert "machine_sleep" not in _machine_jobs(recording_dispatcher)


async def test_an_airtight_re_create_waits_while_someone_drives(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.ai.frames.driver import EgressUnavailable

    monkeypatch.setattr(settings.machines, "wait_s", 1)
    ada, workspace_id, tid, sid = await _task_and_step()
    first = await _acquire(workspace_id, tid, sid, profile="browser")
    await machines.release(first, parked=False)
    await _drive(workspace_id, first.machine_id, ada.id)
    airtight = fake_frames.world[first.frame.ref]
    real_allow = type(airtight).allow

    async def refuse(self: Any, domains: list[str], version: int | None = None) -> bool:
        if self.ref == airtight.ref:
            raise EgressUnavailable("born airtight")
        return await real_allow(self, domains, version)

    monkeypatch.setattr(type(airtight), "allow", refuse)
    with pytest.raises(ModelIncapable, match="someone is driving the browser"):
        await _acquire(workspace_id, tid, sid, attempt=2, profile="browser", allow_domains=["docs.example"])
    row = await _fresh(workspace_id, first.machine_id)
    assert (row.state, row.epoch, row.held_by) == (MachineState.IDLE, first.epoch, None)
    assert row.lease() is not None and first.frame.ref in fake_frames.world  # never slept, never destroyed
    assert "machine_sleep" not in _machine_jobs(recording_dispatcher)


# ── a frame that dies under a lease, found by a step ────────────────────────────────────────


async def test_a_step_that_finds_its_frame_dead_ends_a_live_lease_as_lost(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The frame went while someone drove it, and the next step is the one to find out (`_rewake`,
    running → waking at a new epoch): the lease ends `lost` with that transition, as when the
    reconciler finds it - the new frame is not the browser they were driving."""
    ada, workspace_id, tid, sid = await _task_and_step()
    first = await _acquire(workspace_id, tid, sid)
    await machines.release(first, parked=False)
    await _drive(workspace_id, first.machine_id, ada.id)
    fake_frames.world.clear()  # the frame dies under the driver
    again = await _acquire(workspace_id, tid, sid, attempt=2)
    assert again.woke and again.epoch == first.epoch + 1
    row = await _fresh(workspace_id, first.machine_id)
    assert row.lease() is None
    [ended] = await _ended(tid)
    assert (ended["reason"], ended["actor"]["name"], ended["ended_by"]) == ("lost", "Ada", None)
    await machines.release(again, parked=False)


async def test_a_failed_wake_ends_a_lease_on_its_row_as_lost(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_abandon`: a wake that fails puts the Machine back to rest; a lease on the row (written here
    while the frame boots - no claim reaches a waking row) is cleared with that move, the idle
    clock restarts in it, and the ending is said once, after the commit."""
    ada, workspace_id, tid, sid = await _task_and_step()

    async def broken(self: Any, *args: Any, **kw: Any) -> None:
        async with session_scope(workspace_id=workspace_id) as s:
            row = await Machine.for_task(s, tid)
            assert row is not None and row.state == MachineState.WAKING
            machine_id = row.id
        await _drive(workspace_id, machine_id, ada.id)
        raise OSError("the provider is down")

    monkeypatch.setattr(fake_frames, "create", broken)
    with pytest.raises(ModelIncapable):
        await _acquire(workspace_id, tid, sid)
    async with session_scope(workspace_id=workspace_id) as s:
        found = await Machine.for_task(s, tid)
        assert found is not None
        mid = found.id
    row = await _fresh(workspace_id, mid)
    assert row.state == MachineState.GONE and row.lease() is None
    assert row.last_active_at == row.state_since
    [ended] = await _ended(tid)
    assert (ended["reason"], ended["actor"]["name"]) == ("lost", "Ada")
