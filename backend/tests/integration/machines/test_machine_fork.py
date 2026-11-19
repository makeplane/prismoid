# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Forks seed from the parent's /work, never its browser."""

import datetime as dt
import inspect
import uuid
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError

from prismoid import storage
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.jobs.machines import machine_seed
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import store
from prismoid.schemas.machines import (
    MachineState,
    RebuildReason,
    SleepReason,
    checkpoint_key,
    frame_name,
    machine_prefix,
    seed_holder,
    seed_key,
)
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


async def _parent(fake_frames: Any, state: MachineState) -> tuple[Any, Any, Task, Machine]:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="parent")
        row = Machine(
            workspace_id=workspace.id,
            task_id=task.id,
            driver="docker",
            profile="browser",
            state=MachineState.IDLE,
            state_since=utcnow(),
            last_active_at=utcnow(),
            epoch=1,
        )
        s.add(row)
        await s.flush()
        row.frame_ref = frame_name(row.id, 1)
        await s.commit()
    frame = fake_frames(str(task.id), profile="browser", name=row.frame_ref)
    await frame.create()
    await frame.write_bytes("/work/notes.db", b"rows")
    await frame.write_bytes("/browser/profile/Default/Cookies", b"sid=SECRET")
    if state == MachineState.ASLEEP:
        assert await machines.sleep(row.id, 1, SleepReason.IDLE) == "slept"
    return ada, workspace, task, row


async def _row(workspace_id: uuid.UUID, machine_id: uuid.UUID) -> Machine | None:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        if row is not None:
            s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def _fork(
    ada: Any, workspace: Any, parent: Task, *, client_token: str | None = None
) -> tuple[Task, Machine | None]:
    async with session_scope(workspace_id=workspace.id) as s:
        child = await task_ops.fork(s, ada, parent.id, client_token=client_token)
        s.expunge(child)  # readable after the scope, whatever its closing rollback expires
    async with session_scope(workspace_id=workspace.id) as s:
        row = await Machine.for_task(s, child.id)
        if row is not None:
            s.expunge(row)
        return child, row


async def _set(workspace_id: uuid.UUID, machine_id: uuid.UUID, **fields: Any) -> None:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Machine, machine_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        await s.commit()


async def test_an_idle_parent_is_seeded_live_and_keeps_its_idle_clock(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    record = recording_dispatcher.enqueue

    async def enqueue(job: str, key: str, **kwargs: Any) -> None:
        sent.append(kwargs)
        await record(job, key, **kwargs)

    monkeypatch.setattr(recording_dispatcher, "enqueue", enqueue)
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    before = row.last_active_at
    child, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None and child_row.state == MachineState.SLEEPING
    assert child_row.epoch == 0 and child_row.profile == "browser"
    assert ("machine_seed", f"machine-seed-{child.id}") in recording_dispatcher.enqueues
    assert sent[-1] == {"parent_machine_id": str(row.id), "child_machine_id": str(child_row.id)}
    inspect.signature(machine_seed).bind(**sent[-1])  # the kwargs are the workflow's parameters
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        seeded = await _row(workspace.id, child_row.id)
        again = await _row(workspace.id, row.id)
        assert (
            seeded is not None
            and seeded.state == MachineState.ASLEEP
            and seeded.checkpoint_object == seed_key(child_row.id)
        )
        assert seeded.browser_object is None, "a fork never carries the browser profile"
        assert again is not None and again.state == MachineState.IDLE and again.last_active_at == before
        assert again.held_by is None
        assert not fake_frames.world[row.frame_ref].frozen
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_an_asleep_parent_is_copied_server_side_and_the_fork_wakes_with_its_files(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.ASLEEP)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        restored = fake_frames("restored")
        await restored.create()
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.checkpoint_sha256 and seeded.checkpoint_object
        await store.load_work(restored, seeded.checkpoint_object, seeded.checkpoint_sha256)
        assert await restored.read_bytes("/work/notes.db") == b"rows"
        assert "/browser/profile/Default/Cookies" not in restored.files
    finally:
        await store.delete_prefix(machine_prefix(row.id))
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_running_parent_with_no_checkpoint_gives_a_fresh_fork(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    await _set(workspace.id, row.id, state=MachineState.RUNNING, held_by="step:x:1")
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    assert await machines.run_seed(row.id, child_row.id) == "fresh"
    fresh = await _row(workspace.id, child_row.id)
    assert fresh is not None and fresh.state == MachineState.GONE and fresh.gone_reason is None
    live = await _row(workspace.id, row.id)
    assert live is not None and live.held_by == "step:x:1", "a step's hold is never the seed's"


async def test_a_parent_without_a_machine_forks_without_one(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        parent = await task_ops.create_task(s, ada, goal="no machine")
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is None
    assert not any(job == "machine_seed" for job, _ in recording_dispatcher.enqueues)


async def test_a_seed_whose_worker_died_mid_copy_resumes_its_own_hold(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """The parent was left `running` under `seed:<child>`; the re-run copies live
    and gives the parent back `idle`, unheld - a seed hold nobody releases would block it forever."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    before = row.last_active_at
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    await _set(workspace.id, row.id, state=MachineState.RUNNING, held_by=seed_holder(child_row.id))
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.IDLE and again.held_by is None
        assert again.last_active_at == before
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.state == MachineState.ASLEEP
        assert seeded.checkpoint_object == seed_key(child_row.id)
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_leased_idle_parent_is_seeded_from_its_checkpoint_and_never_frozen(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone is driving the idle parent's browser, so the seed never freezes
    its frame under them (nor holds it): it copies the last checkpoint, the running/parked rule, and
    the fork is "as of" that checkpoint."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.ASLEEP)  # a checkpoint exists
    slept = await _row(workspace.id, row.id)
    assert slept is not None and slept.checkpoint_at is not None
    ref = frame_name(row.id, 2)
    frame = fake_frames(str(parent.id), profile="browser", name=ref)
    await frame.create()
    await frame.write_bytes("/work/notes.db", b"rows since the checkpoint")
    now = utcnow()
    await _set(
        workspace.id,
        row.id,
        state=MachineState.IDLE,
        epoch=2,
        frame_ref=ref,
        control_actor_id=ada.id,
        control_conn="tab-ada-0001",
        control_since=now - dt.timedelta(seconds=10),
        control_until=now + dt.timedelta(seconds=60),
    )
    froze: list[str] = []
    real_freeze = fake_frames.freeze

    async def freeze(self: Any) -> None:
        froze.append(self.ref)
        await real_freeze(self)

    monkeypatch.setattr(fake_frames, "freeze", freeze)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        assert froze == [] and not fake_frames.world[ref].frozen
        again = await _row(workspace.id, row.id)
        assert again is not None and (again.state, again.held_by) == (MachineState.IDLE, None)
        assert again.lease() is not None  # still driven
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.state == MachineState.ASLEEP
        assert seeded.checkpoint_at == slept.checkpoint_at  # as of the parent's last checkpoint
        restored = fake_frames("restored-leased")
        await restored.create()
        assert seeded.checkpoint_object and seeded.checkpoint_sha256
        await store.load_work(restored, seeded.checkpoint_object, seeded.checkpoint_sha256)
        assert await restored.read_bytes("/work/notes.db") == b"rows"
    finally:
        await store.delete_prefix(machine_prefix(row.id))
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_late_seed_leaves_a_child_the_reconciler_moved_on_alone(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """The child was given up on and a step has woken it since. The late seed
    changes nothing on it, says `gone`, and leaves no `seed.tar.gz` behind."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    await _set(
        workspace.id,
        child_row.id,
        epoch=1,
        state=MachineState.RUNNING,
        held_by="step:y:1",
        frame_ref=frame_name(child_row.id, 1),
    )
    try:
        assert await machines.run_seed(row.id, child_row.id) == "gone"
        moved = await _row(workspace.id, child_row.id)
        assert moved is not None and moved.state == MachineState.RUNNING and moved.epoch == 1
        assert moved.held_by == "step:y:1" and moved.checkpoint_object is None
        assert await store.list_prefix(machine_prefix(child_row.id)) == []
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_late_seed_still_gives_back_the_hold_its_dead_attempt_left(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """Both at once: the attempt that held the parent died, and the child moved on before
    the re-run. The re-run keeps nothing, but the parent is `idle` and unheld again."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    await _set(workspace.id, row.id, state=MachineState.RUNNING, held_by=seed_holder(child_row.id))
    await _set(workspace.id, child_row.id, epoch=1, state=MachineState.RUNNING, held_by="step:y:1")
    try:
        assert await machines.run_seed(row.id, child_row.id) == "gone"
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.IDLE and again.held_by is None
        moved = await _row(workspace.id, child_row.id)
        assert moved is not None and moved.state == MachineState.RUNNING and moved.checkpoint_object is None
        assert await store.list_prefix(machine_prefix(child_row.id)) == []
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_replayed_fork_returns_the_same_child_and_one_machine(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """The same send gesture replayed (client_token) returns the same fork - and never tries to
    give it a second Machine (a unique-constraint 500 on an idempotent retry)."""
    ada, workspace, parent, _ = await _parent(fake_frames, MachineState.IDLE)
    child, first = await _fork(ada, workspace, parent, client_token="fork-once")
    again, second = await _fork(ada, workspace, parent, client_token="fork-once")
    assert again.id == child.id
    assert first is not None and second is not None and second.id == first.id
    seeds = [key for job, key in recording_dispatcher.enqueues if job == "machine_seed"]
    assert set(seeds) == {f"machine-seed-{child.id}"}


async def test_a_seed_that_runs_again_after_it_landed_keeps_the_childs_checkpoint(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """DBOS re-runs a step whose output it never recorded. A second run must not overwrite the
    `seed.tar.gz` the child already points at: its bytes would no longer match the row's hash."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        await fake_frames.world[row.frame_ref].write_bytes("/work/notes.db", b"rows, and more since")
        assert await machines.run_seed(row.id, child_row.id) == "gone"
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.checkpoint_object and seeded.checkpoint_sha256
        restored = fake_frames("restored")
        await restored.create()
        await store.load_work(restored, seeded.checkpoint_object, seeded.checkpoint_sha256)
        assert await restored.read_bytes("/work/notes.db") == b"rows"
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_held_parent_the_reconciler_put_to_rest_is_let_go_and_copied(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """The seed's worker died mid-copy and the parent's frame died too: the reconciler moved the
    row to `asleep` and kept the hold (it never judges a seed's). The re-run lets go of it - no
    one else ever will - and copies the checkpoint."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.ASLEEP)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    await _set(workspace.id, row.id, held_by=seed_holder(child_row.id))
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.ASLEEP and again.held_by is None
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.state == MachineState.ASLEEP
        assert seeded.checkpoint_object == seed_key(child_row.id)
    finally:
        await store.delete_prefix(machine_prefix(row.id))
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_live_copy_that_fails_starts_the_fork_fresh_as_lost(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """A seed that fails leaves the child `gone`, and its first wake says `lost`. The
    parent is given back `idle` with its idle clock where it was."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    before = row.last_active_at
    assert row.frame_ref is not None
    fake_frames.world.pop(row.frame_ref)  # the parent's frame died between steps
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    assert await machines.run_seed(row.id, child_row.id) == "fresh"
    fresh = await _row(workspace.id, child_row.id)
    assert fresh is not None and fresh.state == MachineState.GONE
    assert fresh.gone_reason == RebuildReason.LOST
    again = await _row(workspace.id, row.id)
    assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    assert again.last_active_at == before
    assert await store.list_prefix(machine_prefix(child_row.id)) == []


async def test_a_parent_over_the_cap_starts_the_fork_fresh_as_oversize(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    monkeypatch.setattr(settings.machines, "checkpoint_max_bytes", 1)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    assert await machines.run_seed(row.id, child_row.id) == "fresh"
    fresh = await _row(workspace.id, child_row.id)
    assert fresh is not None and fresh.state == MachineState.GONE
    assert fresh.gone_reason == RebuildReason.OVERSIZE
    again = await _row(workspace.id, row.id)
    assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    assert await store.list_prefix(machine_prefix(child_row.id)) == []


async def test_a_parent_mid_transition_is_not_ready(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    await _set(workspace.id, row.id, state=MachineState.SLEEPING)
    with pytest.raises(machines.SeedNotReady):
        await machines.run_seed(row.id, child_row.id)
    waiting = await _row(workspace.id, child_row.id)
    assert waiting is not None and waiting.state == MachineState.SLEEPING


# ── fix round 1 ─────────────────────────────────────────────────────────────────────────────


async def test_a_second_fork_waits_while_another_seed_holds_the_parent(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """I1: seed A holds the idle parent; seed B must not fall through to "copy the last
    checkpoint" (none, or a stale one) - it waits for A to hand the parent back."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, first = await _fork(ada, workspace, parent)
    _, second = await _fork(ada, workspace, parent)
    assert first is not None and second is not None and first.id != second.id
    await _set(workspace.id, row.id, state=MachineState.RUNNING, held_by=seed_holder(first.id))
    with pytest.raises(machines.SeedNotReady):
        await machines.run_seed(row.id, second.id)
    waiting = await _row(workspace.id, second.id)
    assert waiting is not None and waiting.state == MachineState.SLEEPING
    held = await _row(workspace.id, row.id)
    assert held is not None and held.state == MachineState.RUNNING and held.held_by == seed_holder(first.id)


async def test_the_rollback_lever_gives_a_fork_no_machine(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I2: with the lever pulled no new Machine is made - a fork's included."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    monkeypatch.setattr(settings.machines, "enabled", False)
    child, child_row = await _fork(ada, workspace, parent)
    assert child_row is None and child.parent_task_id == parent.id
    assert not any(job == "machine_seed" for job, _ in recording_dispatcher.enqueues)
    untouched = await _row(workspace.id, row.id)
    assert untouched is not None and untouched.state == MachineState.IDLE and untouched.held_by is None


async def test_a_forks_machine_carries_its_parents_driver_never_the_api_processes(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork runs in the API, whose `PRISMOID_WORKER_FRAME_DRIVER` production leaves
    unset (it defaults to docker). The child's row is stamped with its parent row's driver - where
    the parent's frames run - never the process's setting, so the column never names a driver
    nothing ran on (the reconciler, the bind and the meter all route by it)."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    await _set(workspace.id, row.id, driver="sandbar")
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")  # the API process's default
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None and child_row.driver == "sandbar"


async def test_a_storage_error_during_the_live_copy_retries_and_gives_the_parent_back(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The object store failing for a moment is no reason to start the fork
    fresh. The error propagates for the step to retry, after the parent is given back."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    before = row.last_active_at
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None

    def down(*args: Any, **kwargs: Any) -> None:
        raise EndpointConnectionError(endpoint_url="minio")

    monkeypatch.setattr(storage, "put_file", down)
    with pytest.raises(EndpointConnectionError) as failed:
        await machines.run_seed(row.id, child_row.id)
    assert machines.seed_retryable(failed.value)
    again = await _row(workspace.id, row.id)
    assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    assert again.last_active_at == before
    waiting = await _row(workspace.id, child_row.id)
    assert waiting is not None and waiting.state == MachineState.SLEEPING


async def test_a_freeze_that_fails_partway_is_still_thawed_and_retried(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: a freeze that times out may have stopped some processes: it is thawed all the same."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    thawed: list[str] = []

    async def half(self: Any) -> None:
        raise TimeoutError("freeze timed out partway")

    async def thaw(self: Any) -> None:
        thawed.append(self.name)

    monkeypatch.setattr(fake_frames, "freeze", half)
    monkeypatch.setattr(fake_frames, "thaw", thaw)
    with pytest.raises(TimeoutError) as failed:
        await machines.run_seed(row.id, child_row.id)
    assert machines.seed_retryable(failed.value)
    assert thawed == [row.frame_ref]
    again = await _row(workspace.id, row.id)
    assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    waiting = await _row(workspace.id, child_row.id)
    assert waiting is not None and waiting.state == MachineState.SLEEPING


async def test_a_thaw_that_fails_keeps_the_good_seed(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the seed was uploaded whole; a thaw that fails afterwards is logged, never a `lost`."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None

    async def stuck(self: Any) -> None:
        raise RuntimeError("thaw timed out")

    monkeypatch.setattr(fake_frames, "thaw", stuck)
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.state == MachineState.ASLEEP
        assert seeded.checkpoint_object == seed_key(child_row.id) and seeded.gone_reason is None
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_failed_copy_never_deletes_the_seed_the_child_points_at(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """M2: whatever a run finds, the `seed.tar.gz` the child's row vouches for survives it - here
    a leftover seed hold sends a run to a parent whose frame has died since."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.IDLE)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None and row.frame_ref is not None
    try:
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        await _set(workspace.id, row.id, state=MachineState.RUNNING, held_by=seed_holder(child_row.id))
        fake_frames.world.pop(row.frame_ref)
        assert await machines.run_seed(row.id, child_row.id) == "gone"
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.checkpoint_object and seeded.checkpoint_sha256
        restored = fake_frames("restored")
        await restored.create()
        await store.load_work(restored, seeded.checkpoint_object, seeded.checkpoint_sha256)
        assert await restored.read_bytes("/work/notes.db") == b"rows"
        again = await _row(workspace.id, row.id)
        assert again is not None and again.held_by is None
    finally:
        await store.delete_prefix(machine_prefix(child_row.id))


def test_the_seed_job_retries_what_is_transient_never_a_bug() -> None:
    """M3: `should_retry` - the parent mid-transition and infrastructure failing for a moment
    retry; a bug fails the job at once instead of 20 times."""
    assert machines.seed_retryable(machines.SeedNotReady("parent"))
    assert machines.seed_retryable(RuntimeError("sandbar archive 502"))
    assert machines.seed_retryable(EndpointConnectionError(endpoint_url="minio"))
    assert machines.seed_retryable(ConnectionResetError())
    assert machines.seed_retryable(TimeoutError())
    assert not machines.seed_retryable(TypeError("a bug"))
    assert not machines.seed_retryable(KeyError("a bug"))
    assert not machines.seed_retryable(AttributeError("a bug"))
    assert not machines.seed_retryable(NotImplementedError())


async def test_a_checkpoint_replaced_mid_copy_is_decided_again(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: the parent slept again between the decision and the copy, deleting the checkpoint the
    seed chose. That is a race, not a loss: the step decides again, once."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.ASLEEP)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    rested = await _row(workspace.id, row.id)
    assert rested is not None and rested.checkpoint_object is not None
    good = rested.checkpoint_object
    await _set(workspace.id, row.id, checkpoint_object=checkpoint_key(row.id, 9))  # what the seed will choose
    copy = store.copy

    async def racing(src: str, dst: str) -> None:
        await _set(workspace.id, row.id, checkpoint_object=good)  # a newer sleep replaced it meanwhile
        await copy(src, dst)

    monkeypatch.setattr(store, "copy", racing)
    try:
        with pytest.raises(machines.SeedNotReady):
            await machines.run_seed(row.id, child_row.id)
        monkeypatch.setattr(store, "copy", copy)
        assert await machines.run_seed(row.id, child_row.id) == "seeded"
        seeded = await _row(workspace.id, child_row.id)
        assert seeded is not None and seeded.checkpoint_object and seeded.checkpoint_sha256
        restored = fake_frames("restored")
        await restored.create()
        await store.load_work(restored, seeded.checkpoint_object, seeded.checkpoint_sha256)
        assert await restored.read_bytes("/work/notes.db") == b"rows"
    finally:
        await store.delete_prefix(machine_prefix(row.id))
        await store.delete_prefix(machine_prefix(child_row.id))


async def test_a_checkpoint_that_is_really_gone_starts_the_fork_fresh_as_lost(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """M3's other half: the same checkpoint, still missing, is a loss - decided once, not again."""
    ada, workspace, parent, row = await _parent(fake_frames, MachineState.ASLEEP)
    _, child_row = await _fork(ada, workspace, parent)
    assert child_row is not None
    await store.delete_prefix(machine_prefix(row.id))
    assert await machines.run_seed(row.id, child_row.id) == "fresh"
    fresh = await _row(workspace.id, child_row.id)
    assert fresh is not None and fresh.state == MachineState.GONE and fresh.gone_reason == RebuildReason.LOST


async def test_a_seed_never_crosses_workspaces(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """M4 (a reference never crosses workspaces): a seed job pointed at a child in another workspace
    lands it `gone` with nothing copied into it."""
    _, workspace, _, row = await _parent(fake_frames, MachineState.IDLE)
    async with session_scope() as s:
        eve, other = await member(s, "Eve")  # an unbound session: Eve founds her own workspace
        stranger = await task_ops.create_task(s, eve, goal="elsewhere")
        alien = Machine(
            workspace_id=other.id,
            task_id=stranger.id,
            driver="docker",
            profile="browser",
            state=MachineState.SLEEPING,
            state_since=utcnow(),
            last_active_at=utcnow(),
            epoch=0,
        )
        s.add(alien)
        await s.flush()
        alien_id = alien.id
        await s.commit()
    assert other.id != workspace.id
    try:
        assert await machines.run_seed(row.id, alien_id) == "fresh"
        landed = await _row(other.id, alien_id)
        assert landed is not None and landed.state == MachineState.GONE and landed.checkpoint_object is None
        assert await store.list_prefix(machine_prefix(alien_id)) == []
        again = await _row(workspace.id, row.id)
        assert again is not None and again.state == MachineState.IDLE and again.held_by is None
    finally:
        await store.delete_prefix(machine_prefix(alien_id))
