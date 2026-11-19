# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A device Machine's reset: a wake that beats a reset's cleanup
starts fresh and the late cleanup is a no-op, a cleanup that raises while the helper is away brings
nothing back, and a device task never boots early. The rig is `tests/support/rigs/device_lifecycle_rig.py`."""

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import early_boot, machines
from prismoid.schemas.machines import MachineState
from tests.support.fakes.fake_frames import DEVICE, FakeFrame
from tests.support.rigs.device_lifecycle_rig import (
    MINE,
    lifecycle_acquire,
    lifecycle_asleep,
    lifecycle_machine,
    task_and_step,
)
from tests.support.rigs.device_lifecycle_rig import cloud_worker as cloud_worker
from tests.support.rigs.device_lifecycle_rig import only_our_rows as only_our_rows
from tests.support.rigs.device_lifecycle_rig import quick_wait as quick_wait

pytestmark = pytest.mark.integration


@pytest.fixture
def reset_jobs() -> Iterator[list[dict[str, Any]]]:
    """Every `machine_reset` enqueued, with its kwargs: the test runs the job's body (`_cleanup`)
    when it chooses - late, after a wake, or while the helper is away."""
    from prismoid.operations import dispatch
    from tests.support.dispatchers import SilentDispatcher

    sent: list[dict[str, Any]] = []

    class Resets(SilentDispatcher):
        async def enqueue(self, job: str, key: str, **kwargs: object) -> None:
            if job == "machine_reset":
                sent.append(dict(kwargs))

    dispatch.set_dispatcher(Resets())
    yield sent
    dispatch.set_dispatcher(dispatch.NullDispatcher())


async def _reset(workspace_id: uuid.UUID, task_id: uuid.UUID, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Reset, as its creator clicks it; the `machine_reset` job it enqueued, not yet run."""
    from prismoid.db.models.actor import Actor

    async with session_scope(workspace_id=workspace_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.creator_actor_id is not None
        actor = await s.get(Actor, task.creator_actor_id)
        assert actor is not None
        await machines.reset(s, actor, task_id)
    (job,) = jobs
    return job


async def _cleanup(job: dict[str, Any]) -> None:
    """The `machine_reset` job's body, as its effect calls it."""
    await machines.cleanup(job["frame_ref"], job["keys"], job["driver"], uuid.UUID(job["machine_id"]))


async def test_a_wake_that_beats_a_resets_cleanup_starts_fresh_and_the_late_cleanup_spares_it(
    fake_frames: Any, reset_jobs: list[dict[str, Any]], cloud_worker: None
) -> None:
    """Outcome B: Reset, then a step wakes before the cleanup runs. The helper would re-create
    `device-{task_id}` over the old folder; the wake destroys it first, so the step gets a fresh
    frame. The cleanup, late, finds the row woken and leaves the step's frame alone."""
    workspace, tid, sid = await task_and_step()
    first, _ = await lifecycle_asleep(workspace.id, tid, sid)
    job = await _reset(workspace.id, tid, reset_jobs)
    assert job["frame_ref"] == first.frame.ref and job["driver"] == DEVICE

    again = await lifecycle_acquire(workspace.id, tid, sid, attempt=2)
    assert again.frame.ref == first.frame.ref, "the helper's name for it, as ever"
    assert "/work/notes.md" not in fake_frames.world[again.frame.ref].files, "the Reset's work is gone"
    assert fake_frames.destroyed == [(first.frame.ref, DEVICE)]
    await again.frame.write_bytes("/work/after.md", b"work after the reset")

    await _cleanup(job)
    assert fake_frames.destroyed == [(first.frame.ref, DEVICE)], "the late cleanup destroyed nothing"
    assert fake_frames.world[again.frame.ref].files["/work/after.md"][0] == b"work after the reset"
    row = await lifecycle_machine(workspace.id, tid)
    assert (row.state, row.frame_ref) == (MachineState.RUNNING, again.frame.ref)


async def test_a_resets_cleanup_raises_while_the_helper_is_away_and_nothing_brings_the_frame_back(
    fake_frames: Any,
    reset_jobs: list[dict[str, Any]],
    only_our_rows: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outcome C: the helper is down when the cleanup runs. The cleanup raises - the job's step
    retries it (`reset_retryable`) - and the frame stays. A reconciler pass, past `FRESH_S`, neither
    adopts nor destroys it: the row stays `gone`. Once the helper answers, the retried cleanup
    destroys the old frame. The worker's own driver is the device (local mode), so the pass asks
    the helper."""
    from prismoid.ai.frames.driver import FrameUnreachable

    monkeypatch.setattr(settings.worker, "frame_driver", DEVICE)
    workspace, tid, sid = await task_and_step()
    first, asleep = await lifecycle_asleep(workspace.id, tid, sid)
    MINE.add(asleep.id)
    job = await _reset(workspace.id, tid, reset_jobs)
    answering = frames.destroy_ref

    async def away(ref: str, *, driver: str | None = None) -> None:
        raise FrameUnreachable("the device helper did not answer")

    monkeypatch.setattr(frames, "destroy_ref", away)
    with pytest.raises(FrameUnreachable) as failed:
        await _cleanup(job)
    assert machines.reset_retryable(failed.value)
    assert first.frame.ref in fake_frames.world
    monkeypatch.setattr(frames, "destroy_ref", answering)

    async with session_scope(workspace_id=workspace.id) as s:
        aged = await s.get(Machine, asleep.id)
        assert aged is not None
        aged.state_since = utcnow() - dt.timedelta(minutes=5)  # past `FRESH_S`
        await s.commit()
    plan = await machines.reconcile_once(frozenset(), frozenset())
    assert plan.disowned == () and plan.fixes == ()
    row = await lifecycle_machine(workspace.id, tid)
    assert (row.state, row.frame_ref) == (MachineState.GONE, None), "never given the old frame back"
    assert first.frame.ref in fake_frames.world and fake_frames.destroyed == []

    await _cleanup(job)  # the step's retry, the helper answering
    assert first.frame.ref not in fake_frames.world
    assert fake_frames.destroyed == [(first.frame.ref, DEVICE)]
    assert (await machines.reconcile_once(frozenset(), frozenset())).fixes == ()
    assert (await lifecycle_machine(workspace.id, tid)).state == MachineState.GONE


async def test_a_wake_after_a_reset_whose_helper_is_away_fails_before_it_creates(
    fake_frames: Any, reset_jobs: list[dict[str, Any]], cloud_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wake from `gone` cannot destroy the old frame (the helper does not answer): it fails as
    infra before it creates, so nothing is ever made over the old folder. The row stays `gone`,
    holding nobody; the old frame is the cleanup's to take."""
    from prismoid.ai.frames.driver import FrameUnreachable

    workspace, tid, sid = await task_and_step()
    first, _ = await lifecycle_asleep(workspace.id, tid, sid)
    await _reset(workspace.id, tid, reset_jobs)

    async def away(drivers: Any = None) -> list[Any]:
        raise FrameUnreachable("the device helper did not answer")

    monkeypatch.setattr(frames, "inventory", away)
    with pytest.raises(ModelIncapable, match="could not start"):
        await lifecycle_acquire(workspace.id, tid, sid, attempt=2)
    assert fake_frames.created == [first.frame.ref], "never created over the old folder"
    row = await lifecycle_machine(workspace.id, tid)
    assert (row.state, row.frame_ref, row.held_by) == (MachineState.GONE, None, None)


async def test_a_wake_from_a_reset_that_dies_before_clearing_never_brings_the_old_frame_back(
    fake_frames: Any,
    reset_jobs: list[dict[str, Any]],
    only_our_rows: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash window (round 5): Reset, then a wake claims the row (`waking`, naming no frame) and
    its worker dies before `_clear_gone`. The cleanup finds the row not `gone` and skips it. Past
    `stuck_s`, two reconciler passes leave the row `gone` and the pre-Reset frame unadopted and
    undestroyed. The next wake's `_clear_gone` destroys it and makes a fresh one. The worker's own
    driver is the device (local mode), so the passes ask the helper."""
    from prismoid.operations.machines import acquired, lifecycle
    from prismoid.schemas.machines import step_holder

    monkeypatch.setattr(settings.worker, "frame_driver", DEVICE)
    workspace, tid, sid = await task_and_step()
    first, asleep = await lifecycle_asleep(workspace.id, tid, sid)
    MINE.add(asleep.id)
    job = await _reset(workspace.id, tid, reset_jobs)
    async with session_scope(workspace_id=workspace.id) as session:
        task = await session.get(Task, tid)
        step = await session.get(PlanStep, sid)
        assert task is not None and step is not None
        claim = lifecycle._claim  # pyright: ignore[reportPrivateUsage]
        wake = await claim(session, task, step, step_holder(str(sid), 2), "base", None)  # then the crash
    assert isinstance(wake, acquired.WakePlan) and wake.native_ref is None
    claimed = await lifecycle_machine(workspace.id, tid)
    assert (claimed.state, claimed.frame_ref) == (MachineState.WAKING, None)

    await _cleanup(job)
    assert fake_frames.destroyed == [], "the row is not gone: the cleanup leaves it to the wake"
    async with session_scope(workspace_id=workspace.id) as s:  # the dead attempt's hold, past `stuck_s`
        stuck = await s.get(Machine, asleep.id)
        assert stuck is not None
        stuck.state_since = utcnow() - dt.timedelta(seconds=settings.machines.stuck_s + 60)
        await s.commit()
    for _ in range(2):
        plan = await machines.reconcile_once(frozenset(), frozenset())
        assert plan.disowned == ()
    row = await lifecycle_machine(workspace.id, tid)
    assert (row.state, row.frame_ref) == (MachineState.GONE, None)
    assert fake_frames.world[first.frame.ref].files["/work/notes.md"][0] == b"the user's own file"
    assert fake_frames.destroyed == []

    again = await lifecycle_acquire(workspace.id, tid, sid, attempt=3)
    assert again.frame.ref == first.frame.ref
    assert "/work/notes.md" not in fake_frames.world[again.frame.ref].files, "the Reset's work is gone"
    assert fake_frames.destroyed == [(first.frame.ref, DEVICE)]


@pytest.mark.parametrize("worker_driver", ["remote", "device"])
async def test_a_device_task_never_boots_early(
    fake_frames: FakeFrame,
    recording_dispatcher: Any,
    monkeypatch: pytest.MonkeyPatch,
    worker_driver: str,
) -> None:
    """The prewarm runs before a step's checks, so it never runs for a task on the device
    (ADR-078): the rule reads the TASK's driver, as a frame does, never the worker's - even a
    worker whose own driver boots early (`remote`). No job, no row, no frame: the first step meets
    its helper and its folders itself."""
    monkeypatch.setattr(frames, "EARLY_BOOT_DRIVERS", frozenset({"remote"}))
    monkeypatch.setattr(settings.worker, "frame_driver", worker_driver)
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    workspace, tid, step_id = await task_and_step()
    async with row_scope(Task, tid) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        step.state, step.spec = "ready", {"title": "work", "agent": "maker"}  # computer work, committed
        await session.commit()
        assert await early_boot.for_plan(session, tid) is False
    assert [e for e in recording_dispatcher.enqueues if e[0] == "machine_boot"] == []
    async with session_scope(workspace_id=workspace.id) as s:
        assert await Machine.for_task(s, tid) is None
    assert fake_frames.world == {}
