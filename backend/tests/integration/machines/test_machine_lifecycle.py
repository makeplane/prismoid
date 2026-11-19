# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's life in the Machine: claim, bind or wake, the boundary, release."""

import asyncio
import contextlib
import datetime as dt
import hashlib
import io
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import logfire
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from prismoid import storage
from prismoid.ai.frames.driver import FrameUnreachable
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import acquired, egress_push, handback, lifecycle, store, wake, wake_wait
from prismoid.schemas.machines import (
    LABEL_KIND,
    MachineState,
    Segment,
    SleepReason,
    machine_prefix,
    step_holder,
)
from prismoid.schemas.screen import Lease
from tests.support.cast import member
from tests.support.fakes.fake_frames import NATIVE, FakeFrame, NativeFakeFrame

pytestmark = pytest.mark.integration


async def _task_and_step(name: str = "Ada") -> tuple[Any, Any, uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        actor, workspace = await member(s, name)
        task = await task_ops.create_task(s, actor, goal="machine work")
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="work", spec={}
        )
        s.add(step)
        await s.commit()
        return actor, workspace, task.id, step.id


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


async def _release(acquired: Any, *, parked: bool = False) -> Segment | None:
    """Its own session; the warm segment it closed is the caller's to bill."""
    return await machines.release(acquired, parked=parked)


async def _release_parked(workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID) -> bool:
    """Ids, and its own session: whether a Machine held the park. Its segment
    is the meter's subject (`test_machine_meter.py`)."""
    return (await machines.release_parked(task_id, step_id)).held


async def _machine(workspace_id: uuid.UUID, task_id: uuid.UUID) -> Machine:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def _events(workspace_id: uuid.UUID, task_id: uuid.UUID) -> list[TaskEvent]:
    async with session_scope(workspace_id=workspace_id) as s:
        events = await s.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
        )
        rows = list(events.all())
        s.expunge_all()
        return rows


async def _types(workspace_id: uuid.UUID, task_id: uuid.UUID) -> list[str]:
    return [e.type for e in await _events(workspace_id, task_id)]


async def _until(ready: Callable[[], object], within: float = 5.0) -> None:
    """Poll (letting the other tasks run) until `ready()` is truthy; fail after `within` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not ready():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def _run_sleep(kw: dict[str, Any]) -> str:
    """Run one enqueued `machine_sleep` the way the job does: claimed, with the claim's identity."""
    claim = (
        machines.SleepClaim.from_wire(claim_at=kw["claim_at"], after_s=kw["after_s"], since=kw["since"])
        if kw.get("claimed")
        else None
    )
    return await machines.sleep(
        uuid.UUID(kw["machine_id"]), kw["epoch"], SleepReason(kw["reason"]), claim=claim
    )


@pytest.fixture(autouse=True)
def quick_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.machines, "wait_s", 5)


@pytest.fixture
def run_machine_jobs() -> Iterator[list[tuple[str, str]]]:
    """A dispatcher that runs `machine_sleep` right away (as the worker would), so the cap,
    upgrade and re-creation paths - which acquire only ever dispatches - can be tested."""
    from prismoid.operations import dispatch

    ran: list[tuple[str, str]] = []

    class Inline:
        async def enqueue(self, job: str, key: str, **kw: Any) -> None:
            ran.append((job, key))
            if job == "machine_sleep":
                await _run_sleep(kw)

        async def send(self, *args: Any, **kw: Any) -> None:
            return None

    dispatch.set_dispatcher(Inline())
    yield ran
    dispatch.set_dispatcher(dispatch.NullDispatcher())


@pytest.fixture
def deferred_jobs() -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    """A dispatcher that DEFERS the Machine's jobs: it records each enqueue and runs nothing, so a
    test can look at the row between the hand-back and the job (the worker is slow, or down). Every
    other job (a new task's run) is swallowed."""
    from prismoid.operations import dispatch

    queued: list[tuple[str, str, dict[str, Any]]] = []

    class Deferred:
        async def enqueue(self, job: str, key: str, **kw: Any) -> None:
            if job.startswith("machine_"):
                queued.append((job, key, kw))

        async def send(self, *args: Any, **kw: Any) -> None:
            return None

    dispatch.set_dispatcher(Deferred())
    yield queued
    dispatch.set_dispatcher(dispatch.NullDispatcher())


async def test_the_first_step_creates_the_machine_and_draws_the_boundary(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    assert got.kind == "machine" and got.label == "Starting the Machine" and not got.woke
    frame = fake_frames.world[got.frame.ref]
    assert frame.labels[LABEL_KIND] == "machine" and "/task/.step-start" in frame.files
    assert frame.egress_hosts == ["docs.example"]
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.RUNNING and row.held_by == step_holder(str(sid), 1) and row.epoch == 1
    assert "machine.created" in await _types(workspace.id, tid)


async def test_the_next_step_reuses_the_frame_and_release_narrows_egress(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    await first.frame.write_bytes("/work/notes.db", b"rows")
    await first.frame.write_bytes("/artifacts/manifest.json", b'{"entries": []}')
    await _release(first)
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.held_by is None
    assert fake_frames.world[first.frame.ref].egress_hosts == []
    assert row.step_hosts == ["docs.example"], "the list the step ran under, kept for a driver"
    second = await _acquire(workspace.id, tid, sid, attempt=2)
    assert second.frame.ref == first.frame.ref and len(fake_frames.created) == 1
    assert await second.frame.read_bytes("/work/notes.db") == b"rows"
    assert "/artifacts/manifest.json" not in fake_frames.world[second.frame.ref].files  # the publish boundary


async def test_a_retry_is_shown_what_the_failed_attempt_changed(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/half.md", b"half written")
    await _release(first)
    retry = await _acquire(workspace.id, tid, sid, attempt=2)
    assert retry.changed == ["/work/half.md"]


async def test_a_live_widen_moves_the_rows_egress_with_the_frame(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The row learns a live widen as it happens, so a crash-recovered attempt's
    boundary pushes PAST it - never at a version the frame already has (a real proxy drops that
    as stale, and the frame would keep the dead attempt's `once` host)."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    assert await machines.widen(got, ["docs.example", "once.example"])
    widened = got.frame.egress_version
    row = await _machine(workspace.id, tid)
    assert row.egress_version == widened and row.egress_hosts == ["docs.example", "once.example"]
    # the worker dies here, holding the Machine; the recovered attempt takes it over
    again = await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])
    assert again.frame.egress_version > widened, "the boundary's push is newer than the widen"
    assert fake_frames.world[again.frame.ref].egress_hosts == ["docs.example"]


async def test_a_proxy_ahead_of_the_row_is_re_synced_at_the_next_boundary(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A push whose row update never committed (a worker that died between a
    widen's push and its commit) leaves the proxy ahead of the row. The next boundary's push at
    row+1 is refused as stale, and the frame would keep the dead attempt's `once` host. It adopts
    the proxy's version and pushes once past it: the frame has the step's list, the row catches up."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    row = await _machine(workspace.id, tid)
    # the lost commit: the proxy took the next version, with a `once` host; the row never heard
    assert await first.frame.allow(["docs.example", "once.example"], version=row.egress_version + 1)
    again = await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])
    assert fake_frames.world[again.frame.ref].egress_hosts == ["docs.example"]
    assert again.frame.egress_version == row.egress_version + 2
    assert (await _machine(workspace.id, tid)).egress_version == again.frame.egress_version


async def test_a_list_the_frame_will_not_take_fails_the_attempt_as_infra(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step never runs on a list it did not ask for. When the push is refused
    even after the re-sync, the attempt fails as infra and the Machine is handed back."""
    _, workspace, tid, sid = await _task_and_step()
    await _release(await _acquire(workspace.id, tid, sid))

    async def refused(self: Any, domains: list[str], version: int | None = None) -> bool:
        return False  # dropped as stale every time, and the transport never says why

    monkeypatch.setattr(fake_frames, "allow", refused)
    with pytest.raises(ModelIncapable) as failed:
        await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])
    assert failed.value.context.get("infra")
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.held_by is None


async def test_a_widen_behind_the_proxy_re_syncs_once(fake_frames: Any, silent_dispatcher: None) -> None:
    """The holder's live widen is the truth for the list; a proxy ahead of its
    driver (a version the row never heard of) is adopted and passed, never a silent refusal."""
    _, workspace, tid, sid = await _task_and_step()
    await _release(await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"]))
    got = await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])
    live = fake_frames.world[got.frame.ref]
    assert await live.allow(["x.example"], version=got.frame.egress_version + 5)  # the proxy moved on
    assert await machines.widen(got, ["docs.example", "once.example"])
    assert live.egress_hosts == ["docs.example", "once.example"]
    assert (await _machine(workspace.id, tid)).egress_version == got.frame.egress_version


async def test_a_seats_live_widen_only_pushes(fake_frames: Any, silent_dispatcher: None) -> None:
    _, workspace, tid, sid = await _task_and_step()
    seat = await _acquire(workspace.id, tid, sid, seat=True)
    assert await machines.widen(seat, ["once.example"])
    assert fake_frames.world[seat.frame.ref].egress_hosts == ["once.example"]
    async with session_scope(workspace_id=workspace.id) as s:
        assert await Machine.for_task(s, tid) is None


async def test_a_newer_attempt_of_the_same_step_takes_over_a_dead_hold(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    await _acquire(workspace.id, tid, sid, attempt=1)  # the worker "dies" holding it
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert again.kind == "machine" and (await _machine(workspace.id, tid)).held_by == step_holder(str(sid), 2)


async def test_another_steps_hold_makes_the_step_wait_then_fail_as_infra(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    await _acquire(workspace.id, tid, sid)
    async with session_scope(workspace_id=workspace.id) as s:
        other = PlanStep(
            id=uuid.uuid4(), task_id=tid, ordinal=1, kind="step", state="running", title="b", spec={}
        )
        s.add(other)
        await s.commit()
    with pytest.raises(ModelIncapable) as busy:
        await _acquire(workspace.id, tid, other.id)
    assert busy.value.context.get("infra")


async def test_a_vanished_frame_wakes_from_the_checkpoint(fake_frames: Any, silent_dispatcher: None) -> None:
    """An operator removed an idle Machine's frame between steps."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
        woke = await _acquire(workspace.id, tid, sid, attempt=2)  # wakes normally: epoch 2
        await _release(woke)
        fake_frames.world.clear()  # the frame vanishes while idle
        again = await _acquire(workspace.id, tid, sid, attempt=3)
        assert again.woke and await again.frame.read_bytes("/work/kept.md") == b"saved"
        assert (await _machine(workspace.id, tid)).epoch == 3
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_stopped_frame_is_started_with_its_unsaved_work(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A Docker or host restart stopped an idle Machine's container. Its writable layer, `/work`
    with it, is all there: the next step's bind starts it and goes on with everything written since
    the last checkpoint (there is none here). Only its processes were lost."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/unsaved.md", b"never checkpointed")
    await _release(first)
    fake_frames.world[first.frame.ref].stop()  # the restart
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert not again.woke and again.frame.ref == first.frame.ref
    assert await again.frame.read_bytes("/work/unsaved.md") == b"never checkpointed"
    assert "machine.rebuilt" not in await _types(workspace.id, tid)
    assert (await _machine(workspace.id, tid)).epoch == 1


async def test_a_provider_that_cannot_say_costs_one_infra_attempt_never_a_rebuild(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """Fix wave B5 (lane C I3): gate 2's safety half where it matters. Sandbar's `bind()` raises
    `FrameUnreachable` (a ConnectionError, never a RuntimeError) when it cannot say whether the
    frame runs; the lifecycle must read that as one infra attempt, never as a gone frame - a
    rebuild would wipe `/work` back to the last checkpoint on every Engine hiccup."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
        woke = await _acquire(workspace.id, tid, sid, attempt=2)
        await woke.frame.write_bytes("/work/unsaved.md", b"since the checkpoint")
        await _release(woke)
        before, seen, made = (
            await _machine(workspace.id, tid),
            len(await _types(workspace.id, tid)),
            len(fake_frames.created),
        )

        fake_frames.unreachable = True  # the Engine hiccups: sandbar cannot say whether it runs
        with pytest.raises(ModelIncapable) as failed:
            await _acquire(workspace.id, tid, sid, attempt=3)
        assert failed.value.context.get("infra")
        after = await _machine(workspace.id, tid)
        assert (after.frame_ref, after.epoch, after.checkpoint_object, after.checkpoint_sha256) == (
            before.frame_ref,
            before.epoch,
            before.checkpoint_object,
            before.checkpoint_sha256,
        )
        assert after.state == MachineState.IDLE and after.held_by is None  # handed back, holding nothing
        new = (await _types(workspace.id, tid))[seen:]
        assert "machine.rebuilt" not in new and "machine.woke" not in new
        assert len(fake_frames.created) == made  # no frame was made to replace it

        fake_frames.unreachable = False  # the Engine answers again: the same frame, its work intact
        again = await _acquire(workspace.id, tid, sid, attempt=4)
        assert not again.woke and again.frame.ref == before.frame_ref
        assert await again.frame.read_bytes("/work/unsaved.md") == b"since the checkpoint"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_vanished_frame_with_no_checkpoint_rebuilds_and_says_lost(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)
    fake_frames.world.clear()
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert again.woke and "machine.rebuilt" in await _types(workspace.id, tid)


async def _rebuilt(workspace_id: uuid.UUID, task_id: uuid.UUID) -> list[str]:
    return [e.payload["reason"] for e in await _events(workspace_id, task_id) if e.type == "machine.rebuilt"]


async def test_a_dead_frame_says_its_unsaved_work_was_lost_once(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A bound Machine whose frame died lost what `/work` held since
    its last checkpoint. The rewake says so - with a checkpoint to wake from too - once, before the
    wake's own event, and never again at a later wake."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.IDLE) == "slept"
        woke = await _acquire(workspace.id, tid, sid, attempt=2)
        await woke.frame.write_bytes("/work/unsaved.md", b"never checkpointed")
        await _release(woke)
        fake_frames.world.clear()  # the frame dies while idle
        again = await _acquire(workspace.id, tid, sid, attempt=3)
        assert await again.frame.read_bytes("/work/kept.md") == b"saved"
        assert await _rebuilt(workspace.id, tid) == ["lost"]
        types = await _types(workspace.id, tid)
        assert types.index("machine.rebuilt") < len(types) - 1 and types[-1] == "machine.woke"
    finally:
        await store.delete_prefix(machine_prefix(row.id))
    # with no checkpoint at all: still one line, not one from the rewake and one from the wake
    _, workspace2, tid2, sid2 = await _task_and_step()
    await _release(await _acquire(workspace2.id, tid2, sid2))
    fake_frames.world.clear()
    await _release(await _acquire(workspace2.id, tid2, sid2, attempt=2))
    assert await _rebuilt(workspace2.id, tid2) == ["lost"]


async def test_an_asleep_machine_wakes_with_its_files_and_names_the_turn_author(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    ada, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/notes.db", b"rows")
    await _release(first)
    row = await _machine(workspace.id, tid)
    try:
        await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
        woke = await _acquire(workspace.id, tid, sid, attempt=2)
        assert woke.woke and woke.label == "Waking the Machine"
        assert await woke.frame.read_bytes("/work/notes.db") == b"rows"
        last = (await _events(workspace.id, tid))[-1]
        assert last.type == "machine.woke" and last.actor_id == ada.id  # the turn's author
        assert last.payload["actor"]["id"] == str(ada.id) and last.payload["turn"] == 1
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_checkpoint_that_will_not_unpack_rebuilds_as_lost(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The object downloads and matches its hash, and is not an archive. The
    wake builds fresh and says the checkpoint was lost - not one infra attempt after another."""
    _, workspace, tid, sid = await _task_and_step()
    await _release(await _acquire(workspace.id, tid, sid))
    row = await _machine(workspace.id, tid)
    try:
        await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
        slept = await _machine(workspace.id, tid)
        assert slept.checkpoint_object is not None
        garbage = b"\x1f\x8b not really a gzip"
        await asyncio.to_thread(
            storage.put_file, slept.checkpoint_object, io.BytesIO(garbage), settings.worker.snapshot_bucket
        )
        async with session_scope(workspace_id=workspace.id) as s:
            fresh = await s.get(Machine, row.id)
            assert fresh is not None
            fresh.checkpoint_sha256 = hashlib.sha256(garbage).hexdigest()
            await s.commit()
        woke = await _acquire(workspace.id, tid, sid, attempt=2)
        assert woke.woke and "machine.rebuilt" in await _types(workspace.id, tid)
        assert (await _machine(workspace.id, tid)).checkpoint_object is None
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_wake_past_stuck_s_is_left_to_its_running_attempt(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake restoring a large checkpoint can run past `stuck_s`. Its attempt is running, so a
    reconciler pass in the middle of the restore destroys nothing and fixes nothing - it used to
    destroy the frame, fail the wake's final compare-and-set, and leave the uncapped infra retry to
    restore the same checkpoint into the same end, for ever. A finished phase is progress: the
    wake's `state_since` moves once its frame is up, and again once `/work` is restored (before the
    browser's restore)."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, profile="browser")
    await first.frame.write_bytes("/work/notes.db", b"rows")
    await first.frame.write_bytes("/browser/profile/Default/Cookies", b"sid=1")
    await _release(first)
    row = await _machine(workspace.id, tid)
    for_reconcile = Machine.for_reconcile.__func__

    async def only_ours(cls: type[Machine], session: Any, ids: Any) -> list[Machine]:
        return [r for r in await for_reconcile(cls, session, ids) if r.id == row.id]

    monkeypatch.setattr(Machine, "for_reconcile", classmethod(only_ours))
    restore, create, restore_browser = store.load_work, fake_frames.create, store.load_browser
    since: list[dt.datetime] = []
    passes: list[Any] = []

    async def booting(frame: Any, *args: Any, **kw: Any) -> None:
        since.append((await _machine(workspace.id, tid)).state_since)
        await create(frame, *args, **kw)

    async def slow(frame: Any, key: str, sha256: str) -> None:
        since.append((await _machine(workspace.id, tid)).state_since)
        async with session_scope(workspace_id=workspace.id) as s:  # the restore has now run past stuck_s
            waking = await s.get(Machine, row.id)
            assert waking is not None
            waking.state_since -= dt.timedelta(seconds=settings.machines.stuck_s + 60)
            await s.commit()
        passes.append(await machines.reconcile_once(sleeping_jobs=frozenset(), seeding_jobs=frozenset()))
        await restore(frame, key, sha256)
        since.append((await _machine(workspace.id, tid)).state_since)  # the backdated one

    async def browsing(frame: Any, key: str) -> None:
        since.append((await _machine(workspace.id, tid)).state_since)
        await restore_browser(frame, key)

    try:
        await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
        monkeypatch.setattr(fake_frames, "create", booting)
        monkeypatch.setattr(store, "load_work", slow)
        monkeypatch.setattr(store, "load_browser", browsing)
        woke = await _acquire(workspace.id, tid, sid, attempt=2)
        assert passes[0].destroy == () and passes[0].fixes == (), "a running attempt's wake is its own"
        assert await woke.frame.read_bytes("/work/notes.db") == b"rows"
        assert since[1] > since[0], "the frame coming up is progress"
        assert since[3] > since[2], "so is /work restored"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_wake_and_a_park_are_activity_so_retention_runs_from_them(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """Retention counts from `last_active_at`. A Machine woken after 13 dormant
    days whose step parks, then sleeps past its hold, must not expire hours later: acquire stamps
    the activity, and so does the park."""
    _, workspace, tid, sid = await _task_and_step()
    await _release(await _acquire(workspace.id, tid, sid))
    row = await _machine(workspace.id, tid)
    try:
        await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
        dormant = utcnow() - dt.timedelta(days=13)
        async with session_scope(workspace_id=workspace.id) as s:
            await s.execute(
                text("UPDATE machines SET last_active_at = :at WHERE id = :id"), {"at": dormant, "id": row.id}
            )
            await s.commit()
        woke = await _acquire(workspace.id, tid, sid, attempt=2)
        assert (await _machine(workspace.id, tid)).last_active_at > utcnow() - dt.timedelta(minutes=1)
        await _release(woke, parked=True)
        parked = await _machine(workspace.id, tid)
        assert parked.state == MachineState.PARKED
        assert await machines.sleep(row.id, parked.epoch, SleepReason.PARK_HOLD) == "slept"
        slept = await _machine(workspace.id, tid)
        assert slept.state == MachineState.ASLEEP
        assert slept.last_active_at > utcnow() - dt.timedelta(minutes=1)
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_an_expired_machine_says_so_when_it_wakes(fake_frames: Any, silent_dispatcher: None) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)
    row = await _machine(workspace.id, tid)
    await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
    await machines.expire(row.id, row.epoch)
    await _acquire(workspace.id, tid, sid, attempt=2)
    assert "machine.rebuilt" in await _types(workspace.id, tid)
    assert (await _machine(workspace.id, tid)).gone_reason is None


async def test_at_the_cap_the_workspaces_least_recent_idle_machine_sleeps(
    fake_frames: Any, run_machine_jobs: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.machines, "workspace_warm_cap", 1)
    ada, workspace, tid_a, sid_a = await _task_and_step()
    a = await _acquire(workspace.id, tid_a, sid_a)
    await _release(a)
    _, other_workspace, tid_x, sid_x = await _task_and_step("Xena")  # another workspace: never the victim
    x = await _acquire(other_workspace.id, tid_x, sid_x)
    await _release(x)
    async with session_scope(workspace_id=workspace.id) as s:
        task_b = await task_ops.create_task(s, ada, goal="second task")
        step_b = PlanStep(
            id=uuid.uuid4(), task_id=task_b.id, ordinal=0, kind="step", state="running", title="b", spec={}
        )
        s.add(step_b)
        await s.commit()
    row_a = await _machine(workspace.id, tid_a)
    try:
        b = await _acquire(workspace.id, task_b.id, step_b.id)
        assert b.kind == "machine"
        assert (await _machine(workspace.id, tid_a)).state == MachineState.ASLEEP
        assert (await _machine(other_workspace.id, tid_x)).state == MachineState.IDLE
    finally:
        await store.delete_prefix(machine_prefix(row_a.id))


async def test_at_the_cap_with_everything_busy_the_step_fails_as_infra(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.machines, "workspace_warm_cap", 1)
    ada, workspace, tid, sid = await _task_and_step()
    await _acquire(workspace.id, tid, sid)  # running, not idle
    async with session_scope(workspace_id=workspace.id) as s:
        task_b = await task_ops.create_task(s, ada, goal="second task")
        step_b = PlanStep(
            id=uuid.uuid4(), task_id=task_b.id, ordinal=0, kind="step", state="running", title="b", spec={}
        )
        s.add(step_b)
        await s.commit()
    phases: list[str] = []

    async def on_phase(label: str) -> None:
        phases.append(label)

    with pytest.raises(ModelIncapable, match="no Machine free"):
        await _acquire(workspace.id, task_b.id, step_b.id, on_phase=on_phase)
    assert phases == ["Waiting for a Machine"]


async def test_a_base_machine_meeting_a_browser_step_moves_to_the_browser_image(
    fake_frames: Any, run_machine_jobs: list[tuple[str, str]]
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    row = await _machine(workspace.id, tid)
    try:
        browser = await _acquire(workspace.id, tid, sid, attempt=2, profile="browser")
        assert browser.woke and fake_frames.world[browser.frame.ref].profile == "browser"
        assert await browser.frame.read_bytes("/work/kept.md") == b"saved"
        assert (await _machine(workspace.id, tid)).profile == "browser"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_park_keeps_the_hold_and_a_resume_binds_without_a_boundary(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/artifacts/manifest.json", b'{"entries": [1]}')
    await _release(first, parked=True)
    assert (await _machine(workspace.id, tid)).state == MachineState.PARKED
    resumed = await _acquire(workspace.id, tid, sid, resuming=True, allow_domains=["approved.example"])
    assert resumed.frame.ref == first.frame.ref and not resumed.woke_from_park
    frame = fake_frames.world[resumed.frame.ref]
    assert "/artifacts/manifest.json" in frame.files and frame.egress_hosts == ["approved.example"]


async def test_a_resume_on_a_restarted_frame_is_told_its_page_was_reset(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A restart stopped the parked Machine's container: the resume's bind starts it, its files
    intact, but Chromium and its page died with the stop - so the resumed browser call is told, as
    when a park outlives its hold and wakes."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first, parked=True)
    fake_frames.world[first.frame.ref].stop()  # the restart
    resumed = await _acquire(workspace.id, tid, sid, resuming=True)
    assert resumed.frame.ref == first.frame.ref and not resumed.woke
    assert resumed.woke_from_park, "the page the park left is gone"


async def test_a_park_that_slept_past_its_hold_wakes_from_park(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first, parked=True)
    row = await _machine(workspace.id, tid)
    try:
        assert await machines.sleep(row.id, row.epoch, SleepReason.PARK_HOLD) == "slept"
        assert (await _machine(workspace.id, tid)).held_by == step_holder(str(sid), 1)
        resumed = await _acquire(workspace.id, tid, sid, resuming=True)
        assert resumed.woke and resumed.woke_from_park
    finally:
        await store.delete_prefix(machine_prefix(row.id))


@pytest.mark.parametrize("ahead", ["sweep", "step"])
async def test_the_sweep_and_a_step_race_to_one_frame(
    ahead: str, fake_frames: Any, silent_dispatcher: None
) -> None:
    """An idle Machine is put to sleep at the moment a follow-up's step acquires it.
    Whichever is scheduled first reaches the row first (both interleavings are pinned): the sweep's
    sleep lands and the step wakes from its checkpoint, or the step binds and the sleep skips."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/notes.db", b"rows")
    await _release(first)
    row = await _machine(workspace.id, tid)
    try:
        sweep = machines.sleep(row.id, row.epoch, SleepReason.IDLE)
        step = _acquire(workspace.id, tid, sid, attempt=2)
        if ahead == "sweep":
            slept, got = await asyncio.gather(sweep, step)
        else:
            got, slept = await asyncio.gather(step, sweep)
        assert slept in ("slept", "skipped") and got.woke == (slept == "slept")
        assert len(fake_frames.world) == 1 and got.frame.ref in fake_frames.world
        assert await got.frame.read_bytes("/work/notes.db") == b"rows"
        after = await _machine(workspace.id, tid)
        assert after.state == MachineState.RUNNING and after.frame_ref == got.frame.ref
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_frame_that_cannot_be_widened_is_re_created_with_the_list(
    fake_frames: Any, run_machine_jobs: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandbar sandbox born airtight refuses a widen (EgressUnavailable); the
    Machine is slept and woken born with the step's list."""
    from prismoid.ai.frames.driver import EgressUnavailable

    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    airtight = fake_frames.world[first.frame.ref]
    real_allow = type(airtight).allow

    async def refuse(self: Any, domains: list[str], version: int | None = None) -> bool:
        # The SANDBOX is airtight, whichever driver object `bind()` attached to it: sandbar refuses
        # every push to it (an empty list included; the Machine treats that one as already true).
        if self.ref == airtight.ref:
            raise EgressUnavailable("born airtight")
        return await real_allow(self, domains, version)

    monkeypatch.setattr(type(airtight), "allow", refuse)
    row = await _machine(workspace.id, tid)
    try:
        again = await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])
        assert again.frame.ref != first.frame.ref and again.woke
        assert fake_frames.world[again.frame.ref].egress_hosts == ["docs.example"]
        assert await again.frame.read_bytes("/work/kept.md") == b"saved"
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_an_airtight_frame_runs_a_step_that_needs_no_egress(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandbar sandbox born with no domains refuses even an empty push; a step that needs no
    egress already has what it needs, so the Machine is bound, not re-created."""
    from prismoid.ai.frames.driver import EgressUnavailable

    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)
    airtight = fake_frames.world[first.frame.ref]
    real_allow = type(airtight).allow

    async def refuse(self: Any, domains: list[str], version: int | None = None) -> bool:
        if self.ref == airtight.ref:
            raise EgressUnavailable("born airtight")
        return await real_allow(self, domains, version)

    monkeypatch.setattr(type(airtight), "allow", refuse)
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert again.frame.ref == first.frame.ref and not again.woke
    await _release(again)
    assert (await _machine(workspace.id, tid)).state == MachineState.IDLE


async def test_a_failed_wake_hands_the_machine_back(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot that fails is one infra attempt: the row goes back to rest and holds nobody, so the
    next step is never blocked behind an attempt that already ended."""
    _, workspace, tid, sid = await _task_and_step()
    real_create = fake_frames.create

    async def broken(self: Any, *args: Any, **kw: Any) -> None:
        raise OSError("the provider is down")

    monkeypatch.setattr(fake_frames, "create", broken)
    with pytest.raises(ModelIncapable) as down:
        await _acquire(workspace.id, tid, sid)
    assert down.value.context.get("infra") and fake_frames.world == {}
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.GONE and row.held_by is None and row.frame_ref is None
    monkeypatch.setattr(fake_frames, "create", real_create)
    async with session_scope(workspace_id=workspace.id) as s:
        other = PlanStep(
            id=uuid.uuid4(), task_id=tid, ordinal=1, kind="step", state="running", title="b", spec={}
        )
        s.add(other)
        await s.commit()
    got = await _acquire(workspace.id, tid, other.id)
    assert got.woke and (await _machine(workspace.id, tid)).state == MachineState.RUNNING


async def test_a_frame_that_dies_under_the_boundary_hands_the_machine_back(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm frame stops answering as the step boundary runs: one infra attempt, and the Machine
    is idle again, held by nobody (the next attempt, or the next step, binds or wakes it)."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)

    async def dead(self: Any, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
        raise OSError("the frame stopped answering")

    monkeypatch.setattr(fake_frames, "exec", dead)
    with pytest.raises(ModelIncapable) as down:
        await _acquire(workspace.id, tid, sid, attempt=2)
    assert down.value.context.get("infra")
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.held_by is None


async def test_seats_and_the_rollback_lever_get_throwaway_frames(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    seat = await _acquire(workspace.id, tid, sid, seat=True)
    assert seat.kind == "seat" and seat.machine_id is None
    await _release(seat)
    assert fake_frames.world == {}
    monkeypatch.setattr(settings.machines, "enabled", False)
    off = await _acquire(workspace.id, tid, sid, attempt=2)
    assert off.kind == "throwaway"
    async with session_scope(workspace_id=workspace.id) as s:
        assert await Machine.for_task(s, tid) is None


async def test_a_legacy_park_binds_its_old_frame_and_leaves_no_row(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    legacy = fake_frames(f"{tid}-{sid}-1")
    await legacy.create()
    got = await _acquire(workspace.id, tid, sid, resuming=True, legacy_ref=legacy.ref)
    assert got.kind == "legacy" and got.frame.ref == legacy.ref
    await _release(got)
    assert fake_frames.world == {}
    async with session_scope(workspace_id=workspace.id) as s:
        assert await Machine.for_task(s, tid) is None


# ── fix round 1: claimed hand-backs (T7-I1), release on its own session (T7-I2), M1-M3 ─────────


async def _stop(pending: "asyncio.Task[Any]") -> None:
    if not pending.done():
        pending.cancel()
    with contextlib.suppress(BaseException):
        await pending


async def test_an_airtight_hand_back_claims_its_sleep_so_no_step_rebinds_it(
    fake_frames: Any, deferred_jobs: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """T7-I1: the Machine is handed back ALREADY `sleeping` (claimed in the same commit), so the
    loop's next claim cannot re-bind it while the job waits in the queue, and the job can never
    find it busy and skip."""
    from prismoid.ai.frames.driver import EgressUnavailable

    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    airtight = fake_frames.world[first.frame.ref]
    real_allow = type(airtight).allow

    async def refuse(self: Any, domains: list[str], version: int | None = None) -> bool:
        if self.ref == airtight.ref:
            raise EgressUnavailable("born airtight")
        return await real_allow(self, domains, version)

    monkeypatch.setattr(type(airtight), "allow", refuse)
    row = await _machine(workspace.id, tid)
    pending = asyncio.create_task(_acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"]))
    try:
        await _until(lambda: deferred_jobs)
        job, key, kw = deferred_jobs[0]
        assert job == "machine_sleep" and kw["claimed"] and kw["reason"] == "upgrade"
        assert key == f"machine-sleep-{row.id}-{row.epoch}~{kw['claim_at']}"  # claim-scoped
        assert (await _machine(workspace.id, tid)).state == MachineState.SLEEPING
        await asyncio.sleep(1.5)  # the loop has claimed again: a sleeping row is never bound
        held = await _machine(workspace.id, tid)
        assert held.state == MachineState.SLEEPING and held.held_by is None and not pending.done()
        assert await _run_sleep(kw) == "slept"
        again = await pending
        assert again.woke and again.frame.ref != first.frame.ref
        assert fake_frames.world[again.frame.ref].egress_hosts == ["docs.example"]
        assert await again.frame.read_bytes("/work/kept.md") == b"saved"
        assert len(deferred_jobs) == 1
    finally:
        await _stop(pending)
        await store.delete_prefix(machine_prefix(row.id))


async def test_an_upgrade_claims_the_sleep_as_it_hands_the_machine_back(
    fake_frames: Any, deferred_jobs: list[tuple[str, str, dict[str, Any]]]
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/kept.md", b"saved")
    await _release(first)
    row = await _machine(workspace.id, tid)
    pending = asyncio.create_task(_acquire(workspace.id, tid, sid, attempt=2, profile="browser"))
    try:
        await _until(lambda: deferred_jobs)
        assert deferred_jobs[0][2]["claimed"] and deferred_jobs[0][2]["reason"] == "upgrade"
        assert (await _machine(workspace.id, tid)).state == MachineState.SLEEPING
        assert await _run_sleep(deferred_jobs[0][2]) == "slept"
        browser = await pending
        assert browser.woke and (await _machine(workspace.id, tid)).profile == "browser"
        assert await browser.frame.read_bytes("/work/kept.md") == b"saved"
    finally:
        await _stop(pending)
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_cap_victim_is_claimed_at_once_and_one_is_enough(
    fake_frames: Any, deferred_jobs: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """T7-I1 at the cap: the victim is `sleeping` from the eviction's own commit, so its own step
    cannot re-bind it while the job waits; and the waiting step evicts exactly one Machine, however
    many times it polls before that sleep lands."""
    monkeypatch.setattr(settings.machines, "workspace_warm_cap", 2)
    monkeypatch.setattr(settings.machines, "wait_s", 10)
    ada, workspace, tid_a, sid_a = await _task_and_step()
    a = await _acquire(workspace.id, tid_a, sid_a)
    await _release(a)  # the least recently active
    async with session_scope(workspace_id=workspace.id) as s:
        task_a2 = await task_ops.create_task(s, ada, goal="a second warm task")
        task_b = await task_ops.create_task(s, ada, goal="the waiting task")
        steps = [
            PlanStep(
                id=uuid.uuid4(), task_id=t.id, ordinal=0, kind="step", state="running", title="s", spec={}
            )
            for t in (task_a2, task_b)
        ]
        s.add_all(steps)
        await s.commit()
    a2 = await _acquire(workspace.id, task_a2.id, steps[0].id)
    await _release(a2)
    row_a = await _machine(workspace.id, tid_a)
    waiting = asyncio.create_task(_acquire(workspace.id, task_b.id, steps[1].id))
    try:
        await _until(lambda: deferred_jobs)
        assert deferred_jobs[0][2]["machine_id"] == str(row_a.id) and deferred_jobs[0][2]["claimed"]
        assert (await _machine(workspace.id, tid_a)).state == MachineState.SLEEPING
        monkeypatch.setattr(settings.machines, "wait_s", 1)
        with pytest.raises(ModelIncapable, match="going to sleep"):
            await _acquire(workspace.id, tid_a, sid_a, attempt=2)  # its own step: it is sleeping for B
        await asyncio.sleep(2.5)  # B has polled twice more with its victim still in the queue
        assert (
            len(deferred_jobs) == 1 and (await _machine(workspace.id, task_a2.id)).state == MachineState.IDLE
        )
        assert await _run_sleep(deferred_jobs[0][2]) == "slept"
        b = await waiting
        assert b.kind == "machine" and (await _machine(workspace.id, tid_a)).state == MachineState.ASLEEP
        assert len(deferred_jobs) == 1
    finally:
        await _stop(waiting)
        await store.delete_prefix(machine_prefix(row_a.id))


async def test_a_waiting_step_never_blocks_on_its_victims_row(
    fake_frames: Any, deferred_jobs: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: under the workspace's lock, the pending victim is read WITHOUT a row lock. Whoever holds the
    victim's row (its sleep's commit, its own step's claim - which then wants the workspace's lock) must
    never be waited on while the workspace's lock is held."""
    monkeypatch.setattr(settings.machines, "workspace_warm_cap", 1)
    monkeypatch.setattr(settings.machines, "wait_s", 2)
    ada, workspace, tid_a, sid_a = await _task_and_step()
    a = await _acquire(workspace.id, tid_a, sid_a)
    await _release(a)
    async with session_scope(workspace_id=workspace.id) as s:
        task_b = await task_ops.create_task(s, ada, goal="the waiting task")
        step_b = PlanStep(
            id=uuid.uuid4(), task_id=task_b.id, ordinal=0, kind="step", state="running", title="b", spec={}
        )
        s.add(step_b)
        await s.commit()
    waiting = asyncio.create_task(_acquire(workspace.id, task_b.id, step_b.id))
    try:
        await _until(lambda: deferred_jobs)
        async with session_scope(workspace_id=workspace.id) as holder:
            assert await Machine.locked_for_task(holder, tid_a) is not None  # held until this scope ends
            await _until(waiting.done, within=4)
        with pytest.raises(ModelIncapable, match="go to sleep"):
            await waiting
    finally:
        await _stop(waiting)


async def test_a_release_runs_on_its_own_session(fake_frames: Any, silent_dispatcher: None) -> None:
    """T7-I2: the step's own session may be poisoned by the very failure that ends it; the release
    takes none, so the Machine is never left running and held by a step that is done."""
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    async with session_scope(workspace_id=workspace.id) as poisoned:
        with pytest.raises(DBAPIError):
            await poisoned.execute(text("select 1/0"))
        await machines.release(got, parked=False)
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.held_by is None


async def test_a_failed_resume_gives_back_what_it_found(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: a resume whose push fails puts back the park it took, hold and all - and a Machine that
    was idle goes back idle, never a park it never was."""
    _, workspace, tid, sid = await _task_and_step()
    parked = await _acquire(workspace.id, tid, sid)
    await _release(parked, parked=True)
    _, workspace2, tid2, sid2 = await _task_and_step("Bea")
    idle = await _acquire(workspace2.id, tid2, sid2)
    await _release(idle)

    async def down(self: Any, domains: list[str], version: int | None = None) -> bool:
        raise OSError("the egress proxy is down")

    monkeypatch.setattr(fake_frames, "allow", down)
    for workspace_id, task_id, step_id in ((workspace.id, tid, sid), (workspace2.id, tid2, sid2)):
        with pytest.raises(ModelIncapable) as failed:
            await _acquire(workspace_id, task_id, step_id, resuming=True, allow_domains=["approved.example"])
        assert failed.value.context.get("infra")
    kept = await _machine(workspace.id, tid)
    assert kept.state == MachineState.PARKED and kept.held_by == step_holder(str(sid), 1)
    back = await _machine(workspace2.id, tid2)
    assert back.state == MachineState.IDLE and back.held_by is None


async def test_taking_over_a_dead_wake_keeps_why_it_was_gone(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """M3: an attempt died while it woke an expired Machine; the next attempt's wake still says
    `rebuilt{expired}`."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)
    row = await _machine(workspace.id, tid)
    await machines.sleep(row.id, row.epoch, SleepReason.IDLE)
    await machines.expire(row.id, row.epoch)
    async with session_scope(
        workspace_id=workspace.id
    ) as s:  # attempt 1 claimed the wake, then its worker died
        dead = await Machine.locked_for_task(s, tid)
        assert dead is not None and dead.gone_reason == "expired"
        dead.state, dead.epoch, dead.held_by = MachineState.WAKING, dead.epoch + 1, step_holder(str(sid), 1)
        await s.commit()
    got = await _acquire(workspace.id, tid, sid, attempt=2)
    assert got.woke
    rebuilt = [e.payload["reason"] for e in await _events(workspace.id, tid) if e.type == "machine.rebuilt"]
    assert rebuilt == ["expired"]


async def test_a_cancelled_park_hands_the_machine_back(fake_frames: Any, silent_dispatcher: None) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first, parked=True)
    assert await _release_parked(workspace.id, tid, sid) is True
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.held_by is None
    assert await _release_parked(workspace.id, tid, sid) is False  # nothing holds it now
    second = await _acquire(workspace.id, tid, sid, attempt=2)
    await _release(second, parked=True)
    try:
        assert await machines.sleep(row.id, second.epoch, SleepReason.PARK_HOLD) == "slept"
        assert await _release_parked(workspace.id, tid, sid) is True
        slept = await _machine(workspace.id, tid)
        assert slept.state == MachineState.ASLEEP and slept.held_by is None  # it stays asleep
    finally:
        await store.delete_prefix(machine_prefix(row.id))


async def test_a_cancelled_park_narrows_the_idle_machines_egress(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """An idle Machine's background processes reach nothing - a cancelled park's list (its
    `once` hosts included) goes with it, at a version past the park's, so the next boundary is
    newer still."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example", "once.example"])
    await _release(first, parked=True)
    parked = await _machine(workspace.id, tid)
    assert await _release_parked(workspace.id, tid, sid) is True
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.egress_hosts == []
    assert row.egress_version > parked.egress_version
    assert fake_frames.world[first.frame.ref].egress_hosts == []
    assert fake_frames.world[first.frame.ref].egress_version == row.egress_version


async def _drive(workspace_id: uuid.UUID, task_id: uuid.UUID) -> None:
    """A live control lease on the task's Machine, its creator driving (claiming is the lease's
    own suite's): a person at the browser."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.for_task(s, task_id)
        task = await s.get(Task, task_id)
        assert row is not None and task is not None
        now = await Machine.db_now(s)
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


async def test_a_step_handing_back_under_a_driver_leaves_them_its_list(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A person driving the browser of a Machine no step holds reaches what its last
    step could - the frame is not narrowed under them; the lease's ending does that."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    await _drive(workspace.id, tid)
    await _release(first)
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.held_by is None
    assert (row.egress_hosts, row.step_hosts) == (["docs.example"], ["docs.example"])
    assert fake_frames.world[first.frame.ref].egress_hosts == ["docs.example"]


async def test_a_cancelled_park_under_a_driver_leaves_them_its_list(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example", "once.example"])
    await _release(first, parked=True)
    await _drive(workspace.id, tid)
    parked = await _machine(workspace.id, tid)
    assert await _release_parked(workspace.id, tid, sid) is True
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.IDLE and row.egress_version == parked.egress_version
    assert row.egress_hosts == row.step_hosts == ["docs.example", "once.example"]
    assert fake_frames.world[first.frame.ref].egress_hosts == ["docs.example", "once.example"]


async def test_the_egress_job_pushes_the_rows_list_pinned_to_its_version(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The worker's half of a lease that moved an idle Machine's egress in the API: the
    row's list, at exactly the version the move took - never an older move's, never once a step
    holds the Machine, and a re-run the frame has already passed is refused."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    await _release(first)
    await _drive(workspace.id, tid)
    async with session_scope(workspace_id=workspace.id) as s:  # what a claim does under the row's lock
        locked = await Machine.locked_for_task(s, tid)
        assert locked is not None
        version = locked.stepless_egress(await Machine.db_now(s))
        await s.commit()
    assert version is not None
    live = fake_frames.world[first.frame.ref]
    assert live.egress_hosts == []
    row = await _machine(workspace.id, tid)
    assert await machines.push_egress(row.id, version - 1) is False, "an older move is not this one"
    assert await machines.push_egress(row.id, version) is True
    assert (live.egress_hosts, live.egress_version) == (["docs.example"], version)
    assert await machines.push_egress(row.id, version) is False, "a re-run the proxy has passed"
    second = await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["new.example"])
    assert await machines.push_egress(row.id, (await _machine(workspace.id, tid)).egress_version) is False
    assert fake_frames.world[second.frame.ref].egress_hosts == ["new.example"], "a step holds it"


async def test_a_cancelled_park_never_starts_a_stopped_frame_to_narrow_it(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A stopped frame reaches nothing, and its next start forgets what its address held: the
    hand-back has nothing to narrow, and starting it would only race the next step's own start."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    await _release(first, parked=True)
    fake_frames.world[first.frame.ref].stop()
    assert await _release_parked(workspace.id, tid, sid) is True
    assert (await _machine(workspace.id, tid)).state == MachineState.IDLE
    assert not fake_frames.world[first.frame.ref].running, "left stopped for the next bind"


async def test_a_zombie_attempt_never_widens_the_newer_attempts_frame(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    zombie = await _acquire(workspace.id, tid, sid, attempt=1, allow_domains=["docs.example"])
    await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])  # takes it over
    assert await machines.widen(zombie, ["docs.example", "evil.example"]) is False
    assert fake_frames.world[zombie.frame.ref].egress_hosts == ["docs.example"]
    assert (await _machine(workspace.id, tid)).egress_hosts == ["docs.example"]


async def test_a_held_row_lock_never_hangs_a_release_a_widen_or_a_hand_back(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They wait on the Machine's row at most the lock wait, log, and leave the hold to
    the reconciler - a wait across connections Postgres cannot see must never become a hang."""
    from prismoid.operations.machines import common

    monkeypatch.setattr(common, "LOCK_WAIT_S", 0.3)
    _, workspace, tid, sid = await _task_and_step()
    got = await _acquire(workspace.id, tid, sid)
    async with session_scope(workspace_id=workspace.id) as holder:
        assert await Machine.locked_for_task(holder, tid) is not None  # another connection holds it
        assert await asyncio.wait_for(machines.widen(got, ["late.example"]), 5) is False
        # A release that cannot lock its row closes no segment: the watermark stays, the next edge bills it.
        assert await asyncio.wait_for(machines.release(got, parked=True), 5) is None
        await asyncio.wait_for(_release_parked(workspace.id, tid, sid), 5)
        await holder.rollback()
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.RUNNING and row.held_by == step_holder(str(sid), 1)


async def test_a_cancelled_park_leaves_a_running_or_waking_resume_alone(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The resume already has the Machine: its own release (or its failed wake) hands it back."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first, parked=True)
    await _acquire(workspace.id, tid, sid, resuming=True)  # the resume is running
    assert await _release_parked(workspace.id, tid, sid) is True
    running = await _machine(workspace.id, tid)
    assert running.state == MachineState.RUNNING and running.held_by == step_holder(str(sid), 1)
    async with session_scope(workspace_id=workspace.id) as s:
        row = await Machine.locked_for_task(s, tid)
        assert row is not None
        row.state = MachineState.WAKING
        await s.commit()
    assert await _release_parked(workspace.id, tid, sid) is True
    waking = await _machine(workspace.id, tid)
    assert waking.state == MachineState.WAKING and waking.held_by == step_holder(str(sid), 1)
    async with session_scope(workspace_id=workspace.id) as s:
        other = PlanStep(
            id=uuid.uuid4(), task_id=tid, ordinal=1, kind="step", state="running", title="b", spec={}
        )
        s.add(other)
        await s.commit()
    assert await _release_parked(workspace.id, tid, other.id) is False  # never its Machine


async def test_holds_and_the_parked_frame(fake_frames: Any, silent_dispatcher: None) -> None:
    _, workspace, tid, sid = await _task_and_step()
    async with session_scope(workspace_id=workspace.id) as s:
        other = PlanStep(
            id=uuid.uuid4(), task_id=tid, ordinal=1, kind="step", state="running", title="b", spec={}
        )
        s.add(other)
        await s.commit()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first, parked=True)
    legacy = fake_frames(f"{tid}-{other.id}-0")  # a park from before 1b, by its old name
    await legacy.create()
    async with session_scope(workspace_id=workspace.id) as s:
        task = await s.get(Task, tid)
        step = await s.get(PlanStep, sid)
        pre = await s.get(PlanStep, other.id)
        assert task is not None and step is not None and pre is not None
        assert await machines.holds(s, tid, sid) and not await machines.holds(s, tid, other.id)
        mine = await machines.parked_frame(s, task, step, profile="base")
        assert mine is not None and mine.ref == first.frame.ref
        old = await machines.parked_frame(s, task, pre, profile="base")
        assert old is not None and old.ref == legacy.ref
        fake_frames.world.pop(first.frame.ref)  # the parked frame died
        assert await machines.parked_frame(s, task, step, profile="base") is None


async def test_a_zombie_boundary_never_re_syncs_over_the_newer_holders_list(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The boundary's second push lands for sure, so
    it is fenced by the hold. A zombie attempt whose Machine a newer attempt took over is refused as
    stale on its first push, and must not push again: the frame keeps the newer holder's list."""
    _, workspace, tid, sid = await _task_and_step()
    zombie = await _acquire(workspace.id, tid, sid, attempt=1, allow_domains=["docs.example", "once.example"])
    known = zombie.frame.egress_version  # what the zombie's driver knew before it lost the Machine
    await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])  # takes it over
    live = fake_frames.world[zombie.frame.ref]
    assert live.egress_hosts == ["docs.example"]
    # The zombie's boundary runs on a driver bound to the same frame, one version behind the proxy
    # (the creating driver IS the fake's live frame, so it cannot stand for a stale one).
    stale = fake_frames(str(tid), name=zombie.frame.ref)
    stale.egress_version = known
    hold = egress_push.Hold(zombie.machine_id, zombie.epoch, zombie.holder)
    with pytest.raises(RuntimeError, match="could not be applied"):
        await egress_push.push(stale, ["docs.example", "once.example"], hold=hold)
    assert live.egress_hosts == ["docs.example"]


async def _set_driver(workspace_id: uuid.UUID, task_id: uuid.UUID, driver: str) -> None:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        row.driver = driver
        await s.commit()


async def test_an_existing_machines_frame_is_reached_through_its_rows_driver(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """The driver is the Machine's, not the process's. A Machine its row says sandbar is
    bound through sandbar even on a worker configured for Docker (a deployment that switched)."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)
    await _set_driver(workspace.id, tid, "sandbar")
    fake_frames.made.clear()
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert again.frame.ref == first.frame.ref
    assert fake_frames.made == [("sandbar", first.frame.ref)]


async def test_a_wake_makes_its_frame_on_this_workers_driver_and_the_row_says_so(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """P1 and the kill switch: a new frame is the configured driver's, and the row records
    the driver that made it, so the screen door, the meter and the next bind follow the frame."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/notes.md", b"kept")
    await _release(first)
    row = await _machine(workspace.id, tid)
    assert await machines.sleep(row.id, row.epoch, SleepReason.MANUAL) == "slept"
    await _set_driver(workspace.id, tid, "sandbar")  # it slept on a sandbar worker
    fake_frames.made.clear()
    woke = await _acquire(workspace.id, tid, sid, attempt=2)
    assert woke.woke and fake_frames.made[0] == (None, woke.frame.ref), "this worker's driver"
    assert (await _machine(workspace.id, tid)).driver == "docker"
    assert await woke.frame.read_bytes("/work/notes.md") == b"kept"


async def test_an_idle_machines_egress_push_goes_through_its_rows_driver(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid, allow_domains=["docs.example"])
    await _release(first)
    row = await _machine(workspace.id, tid)
    await _set_driver(workspace.id, tid, "sandbar")
    fake_frames.made.clear()
    await machines.push_egress(row.id, row.egress_version)
    assert fake_frames.made == [("sandbar", first.frame.ref)]


async def test_a_parked_step_binds_through_its_rows_driver_and_never_an_asleep_machine(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """P14: the parked frame is reached by the row's driver; an asleep row (a native driver keeps a
    ref while suspended) yields none, so a parked step cannot wake a computer outside the wake path."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first, parked=True)
    await _set_driver(workspace.id, tid, "sandbar")
    fake_frames.made.clear()
    async with session_scope(workspace_id=workspace.id) as s:
        task = await s.get(Task, tid)
        step = await s.get(PlanStep, sid)
        assert task is not None and step is not None
        assert await machines.parked_frame(s, task, step, profile="base") is not None
        assert fake_frames.made == [("sandbar", first.frame.ref)]
        row = await Machine.for_task(s, tid)
        assert row is not None
        row.state = MachineState.ASLEEP  # asleep, ref kept, still named as the hold
        await s.flush()
        fake_frames.made.clear()
        assert await machines.parked_frame(s, task, step, profile="base") is None
        assert fake_frames.made == []
        row.state = MachineState.WAKING  # a cancelled park whose wake died mid-resume
        await s.flush()
        assert await machines.parked_frame(s, task, step, profile="base") is None
        assert fake_frames.made == []


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> None:
    """This worker makes native frames (NativeFakeFrame): its Machines sleep natively."""
    monkeypatch.setattr(settings.worker, "frame_driver", NATIVE)


async def _natively_asleep(
    workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID
) -> tuple[Any, Machine]:
    first = await _acquire(workspace_id, task_id, step_id)
    await first.frame.write_bytes("/work/tab.txt", b"the page left open")
    await _release(first)
    row = await _machine(workspace_id, task_id)
    assert await machines.sleep(row.id, row.epoch, SleepReason.MANUAL) == "slept"
    return first, await _machine(workspace_id, task_id)


async def test_a_natively_asleep_machine_wakes_by_resume_with_everything_in_it(
    fake_frames: Any, silent_dispatcher: None, native: None
) -> None:
    """The computer kept its memory; the wake resumes it - nothing is rebuilt - and
    the boundary pushes the step's list (a woken computer reaches nothing new until `allow`)."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)
    assert asleep.frame_ref == first.frame.ref and fake_frames.world[first.frame.ref].suspended
    again = await _acquire(workspace.id, tid, sid, attempt=2, allow_domains=["docs.example"])
    assert again.woke and again.frame.ref == first.frame.ref
    assert fake_frames.created == [first.frame.ref], "resumed, never rebuilt"
    live = fake_frames.world[first.frame.ref]
    assert not live.suspended and live.egress_hosts == ["docs.example"]
    row = await _machine(workspace.id, tid)
    assert row.state == MachineState.RUNNING and row.epoch == asleep.epoch + 1
    assert row.frame_ref == first.frame.ref and row.driver == NATIVE
    assert "machine.woke" in await _types(workspace.id, tid)


async def test_a_native_machine_whose_computer_is_gone_wakes_from_its_checkpoint(
    fake_frames: Any, silent_dispatcher: None, native: None
) -> None:
    """The host lost it, or its memory window ran out - `resume` meets a gone frame
    and the archive path runs: `/work` comes back from the checkpoint. A pin (D2d): today's wake
    already rebuilt every asleep row, so this passed before the resume existed."""
    _, workspace, tid, sid = await _task_and_step()
    first, _ = await _natively_asleep(workspace.id, tid, sid)
    del fake_frames.world[first.frame.ref]
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert again.woke and again.frame.ref != first.frame.ref
    assert await again.frame.read_bytes("/work/tab.txt") == b"the page left open"


@pytest.mark.parametrize("change", ["profile", "driver"])
async def test_a_native_machine_that_must_change_is_rebuilt_and_its_computer_destroyed(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A base computer never wakes as a browser Machine, and a switched deployment
    never resumes a computer on the driver it left (the kill switch): both rebuild from the
    checkpoint, and the computer left behind is destroyed, never resumed."""
    _, workspace, tid, sid = await _task_and_step()
    first, _ = await _natively_asleep(workspace.id, tid, sid)
    extra: dict[str, Any] = {}
    if change == "profile":
        extra["profile"] = "browser"
    else:
        monkeypatch.setattr(settings.worker, "frame_driver", "docker")  # the operator's switch
    again = await _acquire(workspace.id, tid, sid, attempt=2, **extra)
    assert again.woke and again.frame.ref != first.frame.ref
    assert first.frame.ref not in fake_frames.world, "the computer it left is destroyed"
    assert fake_frames.destroyed == [(first.frame.ref, NATIVE)], "through the driver that made it"
    assert await again.frame.read_bytes("/work/tab.txt") == b"the page left open"
    assert (await _machine(workspace.id, tid)).driver == ("docker" if change == "driver" else NATIVE)


async def test_a_computer_left_behind_that_will_not_die_never_fails_the_wake(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: the wake already named its new frame, so a destroy that fails is logged and left to the
    reconciler (the row no longer names that computer) - never swallowed silently, never the step's."""
    _, workspace, tid, sid = await _task_and_step()
    first, _ = await _natively_asleep(workspace.id, tid, sid)

    asked: list[tuple[str, str | None]] = []

    async def stuck(ref: str, *, driver: str | None = None) -> None:
        asked.append((ref, driver))
        raise OSError("the host would not let it go")

    warned: list[str] = []
    monkeypatch.setattr(wake.frames, "destroy_ref", stuck)
    monkeypatch.setattr(logfire, "warn", lambda message, **_: warned.append(message))
    again = await _acquire(workspace.id, tid, sid, attempt=2, profile="browser")
    assert again.woke and again.frame.ref != first.frame.ref
    assert (
        first.frame.ref in fake_frames.world
        and (await _machine(workspace.id, tid)).frame_ref == again.frame.ref
    )
    assert any("could not be destroyed" in w for w in warned)
    assert asked == [(first.frame.ref, NATIVE)], "asked through the driver that made it"


def _fast_wake_retries(monkeypatch: pytest.MonkeyPatch, budget_s: int = 1) -> None:
    """Q20's retry budget, shrunk to a test's second: the backoff starts at 10 ms."""
    monkeypatch.setattr(settings.machines, "wake_retry_s", budget_s)
    monkeypatch.setattr(wake_wait, "RETRY_FIRST_S", 0.01)


async def _still_asleep_naming(
    workspace_id: uuid.UUID, task_id: uuid.UUID, asleep: Machine, ref: str
) -> None:
    """The rule (ADR-063): a wake the provider did not refuse as gone leaves the Machine as the
    claim found it - asleep, naming its computer, holding nobody - and the computer itself
    untouched, still suspended with its memory."""
    row = await _machine(workspace_id, task_id)
    assert row.state == MachineState.ASLEEP and row.held_by is None
    assert row.frame_ref == ref and row.driver == NATIVE and row.profile == asleep.profile
    assert row.checkpoint_object == asleep.checkpoint_object
    assert ref in FakeFrame.world and FakeFrame.world[ref].suspended, "the computer is never destroyed"


async def test_a_resume_that_is_refused_is_one_infra_attempt_and_the_computer_stays_asleep(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that refuses to resume (not "gone", which rebuilds; not "did not answer", which
    is retried) costs one infra attempt, as a failed boot does, and the Machine stays asleep naming
    its computer: Prismoid never destroys a computer its provider has not said is gone (Q20, replacing
    the Q5 interim's destroy). The next attempt resumes it, memory and all."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)
    real = NativeFakeFrame.resume
    asked: list[str] = []

    async def refuse(self: Any) -> None:
        asked.append(self.ref)
        raise OSError("the host would not resume it")

    monkeypatch.setattr(NativeFakeFrame, "resume", refuse)
    _fast_wake_retries(monkeypatch)
    with pytest.raises(ModelIncapable) as down:
        await _acquire(workspace.id, tid, sid, attempt=2)
    assert down.value.context.get("infra") and "could not wake" in str(down.value)
    assert "still asleep" in str(down.value)
    assert asked == [first.frame.ref], "a refusal is not retried"
    await _still_asleep_naming(workspace.id, tid, asleep, first.frame.ref)
    monkeypatch.setattr(NativeFakeFrame, "resume", real)
    again = await _acquire(workspace.id, tid, sid, attempt=3)
    assert again.woke and again.frame.ref == first.frame.ref
    assert fake_frames.created == [first.frame.ref], "resumed, never rebuilt"


async def test_a_wake_the_provider_does_not_answer_is_retried_and_resumes_the_same_computer(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q20: a Wake that does not answer (the provider restarting, its API unreachable) is not the
    computer being gone. The wake retries it with backoff inside `wake_retry_s`, tells the step the
    Machine is taking longer to wake, and resumes the same computer when the provider answers."""
    _, workspace, tid, sid = await _task_and_step()
    first, _ = await _natively_asleep(workspace.id, tid, sid)
    real = NativeFakeFrame.resume
    asked: list[str] = []

    async def flaky(self: Any) -> None:
        asked.append(self.ref)
        if len(asked) < 3:
            raise FrameUnreachable("the provider did not answer wake (unavailable)")
        await real(self)

    monkeypatch.setattr(NativeFakeFrame, "resume", flaky)
    _fast_wake_retries(monkeypatch, budget_s=5)
    phases: list[str] = []

    async def on_phase(label: str) -> None:
        phases.append(label)

    again = await _acquire(workspace.id, tid, sid, attempt=2, on_phase=on_phase)
    assert again.woke and again.frame.ref == first.frame.ref
    assert asked == [first.frame.ref] * 3
    assert fake_frames.created == [first.frame.ref], "resumed, never rebuilt"
    assert phases == ["Waking the Machine", wake_wait.WAKE_SLOW], "said once, honestly"
    assert (await _machine(workspace.id, tid)).state == MachineState.RUNNING


@pytest.mark.parametrize("how", ["unavailable", "hangs"])
async def test_a_wake_that_never_answers_leaves_the_computer_asleep_and_fails_with_the_reason(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Q20: when the budget runs out - every Wake unanswered, or one Wake that never returns - the
    attempt fails as infra with a reason the user can read, and the Machine is LEFT ASLEEP naming
    its computer, so the ladder's next rung (or a later step) wakes it with its memory."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)
    real = NativeFakeFrame.resume
    asked: list[str] = []

    async def silent(self: Any) -> None:
        asked.append(self.ref)
        if how == "hangs":
            await asyncio.sleep(3600)
        raise FrameUnreachable("the provider did not answer wake (unavailable)")

    monkeypatch.setattr(NativeFakeFrame, "resume", silent)
    _fast_wake_retries(monkeypatch)
    loop = asyncio.get_running_loop()
    began = loop.time()
    with pytest.raises(ModelIncapable) as down:
        await _acquire(workspace.id, tid, sid, attempt=2)
    assert loop.time() - began < 5, "the budget bounds the wake"
    reason = str(down.value)
    assert down.value.context.get("infra") and "could not wake" in reason and "still asleep" in reason
    assert len(asked) > 1 if how == "unavailable" else asked == [first.frame.ref]
    await _still_asleep_naming(workspace.id, tid, asleep, first.frame.ref)
    monkeypatch.setattr(NativeFakeFrame, "resume", real)
    again = await _acquire(workspace.id, tid, sid, attempt=3)
    assert again.woke and again.frame.ref == first.frame.ref
    assert await again.frame.read_bytes("/work/tab.txt") == b"the page left open"
    assert fake_frames.created == [first.frame.ref], "resumed, never rebuilt"


async def test_a_wake_that_answers_gone_after_a_retry_rebuilds_from_the_checkpoint(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the provider saying gone takes the rebuild: an unanswered Wake retried that then answers
    "gone" (deleted, `host_lost`) rebuilds `/work` from the checkpoint, as a first "gone" does."""
    _, workspace, tid, sid = await _task_and_step()
    first, _ = await _natively_asleep(workspace.id, tid, sid)
    asked: list[str] = []

    async def lost(self: Any) -> None:
        asked.append(self.ref)
        if len(asked) == 1:
            raise FrameUnreachable("the provider did not answer wake (host_unavailable)")
        FakeFrame.world.pop(self.ref, None)  # its host came back without it
        raise RuntimeError("the computer is gone at wake (host_lost)")

    monkeypatch.setattr(NativeFakeFrame, "resume", lost)
    _fast_wake_retries(monkeypatch, budget_s=5)
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert len(asked) == 2 and again.woke and again.frame.ref != first.frame.ref
    assert await again.frame.read_bytes("/work/tab.txt") == b"the page left open"


async def test_a_cancelled_wake_leaves_the_computer_asleep(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation stays a cancellation, and it is not the provider saying gone either: the Machine
    stays asleep naming its computer (Q20, replacing the Q5 interim's destroy)."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)

    async def cancelled(self: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(NativeFakeFrame, "resume", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _acquire(workspace.id, tid, sid, attempt=2)
    await _still_asleep_naming(workspace.id, tid, asleep, first.frame.ref)


async def test_a_rebuild_that_fails_never_disowns_the_computer_it_would_have_replaced(
    fake_frames: Any, silent_dispatcher: None, native: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native Machine that must change (a browser step meets a base computer) rebuilds and
    destroys the old computer only once the new one is up. A rebuild the provider does not answer puts
    the row back as it found it - asleep, naming the old computer with its old profile - so the
    reconciler never takes it as disowned, and a later base step resumes it."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)
    real_create = NativeFakeFrame.create

    async def unanswered(self: Any, *args: Any, **kw: Any) -> None:
        raise FrameUnreachable("the provider did not answer create (unavailable)")

    monkeypatch.setattr(NativeFakeFrame, "create", unanswered)
    with pytest.raises(ModelIncapable) as down:
        await _acquire(workspace.id, tid, sid, attempt=2, profile="browser")
    assert down.value.context.get("infra") and "could not start" in str(down.value)
    await _still_asleep_naming(workspace.id, tid, asleep, first.frame.ref)
    assert fake_frames.destroyed == []
    monkeypatch.setattr(NativeFakeFrame, "create", real_create)
    again = await _acquire(workspace.id, tid, sid, attempt=3)
    assert again.woke and again.frame.ref == first.frame.ref
    assert fake_frames.created == [first.frame.ref], "resumed, never rebuilt"


async def test_an_archive_wake_the_provider_does_not_answer_is_unchanged(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker and sandbar sleep by archive: an asleep row names no frame, so a wake makes a new one
    and nothing asleep can be lost. A create the provider does not answer is still one infra attempt
    (never retried in place - the ladder paces the next rung), its half-made frame torn down, the
    row asleep on its checkpoint naming nothing."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/notes.md", b"kept")
    await _release(first)
    row = await _machine(workspace.id, tid)
    assert await machines.sleep(row.id, row.epoch, SleepReason.MANUAL) == "slept"
    asleep = await _machine(workspace.id, tid)
    assert asleep.frame_ref is None
    tried: list[str] = []

    async def unanswered(self: Any, *args: Any, **kw: Any) -> None:
        tried.append(self.ref)
        FakeFrame.world[self.ref] = self  # half-made
        raise FrameUnreachable("the Engine did not answer")

    monkeypatch.setattr(fake_frames, "create", unanswered)
    _fast_wake_retries(monkeypatch, budget_s=5)
    with pytest.raises(ModelIncapable) as down:
        await _acquire(workspace.id, tid, sid, attempt=2)
    assert down.value.context.get("infra") and "could not start" in str(down.value)
    assert len(tried) == 1 and fake_frames.world == {}, "one attempt; the half-made frame torn down"
    after = await _machine(workspace.id, tid)
    assert after.state == MachineState.ASLEEP and after.held_by is None and after.frame_ref is None
    assert after.checkpoint_object == asleep.checkpoint_object


async def test_an_archive_driver_is_never_asked_to_resume(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wake branches on the driver's own declaration (`sleep_kind`), never on
    the row alone. An archive driver's asleep row names no frame; one that does anyway is rebuilt
    from its checkpoint, and its driver's `resume` - NotImplementedError, a RuntimeError that the
    "gone" rule would swallow - is never asked."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await first.frame.write_bytes("/work/notes.md", b"kept")
    await _release(first)
    row = await _machine(workspace.id, tid)
    assert await machines.sleep(row.id, row.epoch, SleepReason.MANUAL) == "slept"
    stray = fake_frames(str(tid), name="a-frame-an-archive-row-should-not-name")
    await stray.create()
    async with session_scope(workspace_id=workspace.id) as s:
        found = await Machine.for_task(s, tid)
        assert found is not None and found.state == MachineState.ASLEEP
        found.frame_ref = stray.ref
        await s.commit()
    asked: list[str] = []

    async def resume(self: Any) -> None:
        asked.append(self.ref)
        raise NotImplementedError("a FakeFrame sleeps by archive, never natively")

    monkeypatch.setattr(fake_frames, "resume", resume)
    again = await _acquire(workspace.id, tid, sid, attempt=2)
    assert asked == [], "an archive driver is never asked to resume"
    assert again.woke and again.frame.ref not in (first.frame.ref, stray.ref)
    assert await again.frame.read_bytes("/work/notes.md") == b"kept"
    assert stray.ref not in fake_frames.world, "the row names the new frame: the old one is destroyed"


async def test_a_runtime_error_from_the_phase_publisher_is_not_the_computer_being_gone(
    fake_frames: Any, silent_dispatcher: None, native: None
) -> None:
    """Only `resume`/`bind` saying RuntimeError means "gone" (rebuild, destroying the old computer):
    the same error from `on_phase` is one infra attempt (as any failed wake), never a silent rebuild
    from the checkpoint, and the computer stays asleep (ADR-063)."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)

    async def on_phase(label: str) -> None:
        raise RuntimeError("the publisher fell over")

    with pytest.raises(ModelIncapable, match="publisher") as down:
        await _acquire(workspace.id, tid, sid, attempt=2, on_phase=on_phase)
    assert down.value.context.get("infra") and "could not wake" in str(down.value)
    assert fake_frames.created == [first.frame.ref], "no rebuild was made"
    await _still_asleep_naming(workspace.id, tid, asleep, first.frame.ref)


async def test_a_resumed_computer_takes_the_rows_egress_as_the_truth(
    fake_frames: Any, silent_dispatcher: None, native: None
) -> None:
    """T9, as `_bind` takes it: a provider's `bind` syncs no list (the real drivers report none), so
    the resumed frame carries the row's version AND hosts into the boundary - never what its bind
    reported."""
    _, workspace, tid, sid = await _task_and_step()
    first, asleep = await _natively_asleep(workspace.id, tid, sid)
    async with session_scope(workspace_id=workspace.id) as s:
        found = await Machine.for_task(s, tid)
        assert found is not None
        found.egress_version, found.egress_hosts = asleep.egress_version + 3, ["kept.example"]
        await s.commit()
    live = fake_frames.world[first.frame.ref]
    live.egress_version, live.egress_hosts = 0, []  # what a real driver's bind reports
    async with session_scope(workspace_id=workspace.id) as s:
        task, step = await s.get(Task, tid), await s.get(PlanStep, sid)
        assert task is not None and step is not None
        step.attempt = 2
        holder = step_holder(str(step.id), step.attempt)
        plan = await lifecycle._claim(s, task, step, holder, "base", None)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(plan, acquired.WakePlan) and plan.resume
        frame = await wake._resume(  # pyright: ignore[reportPrivateUsage]
            s, task, step, plan, holder, label="Waking the Machine", on_phase=None
        )
    assert frame is not None and frame.ref == first.frame.ref
    assert (frame.egress_version, frame.egress_hosts) == (asleep.egress_version + 3, ["kept.example"])


async def test_a_release_closes_what_the_step_held_open_on_its_frame(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """A remote browser session closes when the step releases the Machine,
    idle and parked alike; the frame itself stays warm."""
    _, workspace, tid, sid = await _task_and_step()
    first = await _acquire(workspace.id, tid, sid)
    await _release(first)
    assert fake_frames.world[first.frame.ref].closes == 1
    second = await _acquire(workspace.id, tid, sid, attempt=2)
    await _release(second, parked=True)
    assert fake_frames.world[first.frame.ref].closes == 2
    assert (await _machine(workspace.id, tid)).state == MachineState.PARKED


async def test_a_release_that_lost_its_machine_still_closes_its_frame(
    fake_frames: Any, silent_dispatcher: None
) -> None:
    """`close` runs on every path: a zombie attempt whose Machine a newer attempt took over loses its
    compare-and-set, and still lets go of what it held open."""
    _, workspace, tid, sid = await _task_and_step()
    zombie = await _acquire(workspace.id, tid, sid, attempt=1)
    await _acquire(workspace.id, tid, sid, attempt=2)  # takes the Machine over
    await _release(zombie)
    assert fake_frames.world[zombie.frame.ref].closes == 1


@pytest.mark.parametrize("parked", [True, False])
async def test_a_throwaway_frames_release_closes_it(
    fake_frames: Any, silent_dispatcher: None, parked: bool
) -> None:
    """A parked seat keeps its frame and a finished one destroys it: either way `close` is called."""
    frame = fake_frames("throwaway")
    await frame.create()
    held = acquired.Acquired(frame, acquired.AcquiredKind.THROWAWAY, "x", task_id=uuid.uuid4())
    await machines.release(held, parked=parked)
    assert fake_frames.world.get(frame.ref, frame).closes == 1
    assert (frame.ref in fake_frames.world) is parked


async def test_a_frame_whose_close_fails_never_fails_its_release(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`close` never raises into a release: the result is the hand-back's, and the failure leaves a
    breadcrumb naming the driver and the exception type only."""
    _, workspace, tid, sid = await _task_and_step()
    held = await _acquire(workspace.id, tid, sid)
    warned: list[dict[str, Any]] = []
    monkeypatch.setattr(handback.logfire, "warn", lambda msg, **kw: warned.append(kw))

    async def broken() -> None:
        raise ConnectionError("secret detail")

    monkeypatch.setattr(held.frame, "close", broken)
    await _release(held)  # returns the hand-back's own result, never the close's failure
    assert warned == [{"driver": held.frame.driver, "error": "ConnectionError"}]
    assert (await _machine(workspace.id, tid)).state == MachineState.IDLE


async def test_a_release_that_raises_still_closes_its_frame(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`close` is in a `finally`: a hand-back that raises (the database went away) propagates its own
    error, and the frame is still let go."""
    _, workspace, tid, sid = await _task_and_step()
    held = await _acquire(workspace.id, tid, sid)

    async def boom(acquired: Any, *, parked: bool) -> None:
        raise OSError("db gone")

    monkeypatch.setattr(handback, "_release", boom)
    with pytest.raises(OSError, match="db gone"):
        await machines.release(held, parked=False)
    assert fake_frames.world[held.frame.ref].closes == 1
