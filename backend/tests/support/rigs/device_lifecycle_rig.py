# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for a device Machine's life tests, shared by the
`test_machine_device_lifecycle*` files: a task with a step, a Machine row acquired on the device, and
`NoneSleepFakeFrame` standing in for `DeviceFrame`. A plain module, not a test file."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.schemas.machines import FrameDriverName, SleepReason
from tests.support.cast import member
from tests.support.fakes.fake_frames import DEVICE

REAL_INVENTORY = frames.inventory  # the factory's own listing, before `fake_frames` installs the fake's


@pytest.fixture(autouse=True)
def quick_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.machines, "wait_s", 5)


@pytest.fixture
def cloud_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's own driver is Docker: only the task's `machine_driver` can pick the device."""
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")


async def task_and_step(driver: FrameDriverName | None = DEVICE) -> tuple[Any, uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="device work")
        task.machine_driver = driver
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="work", spec={}
        )
        s.add(step)
        await s.commit()
        return workspace, task.id, step.id


async def lifecycle_acquire(
    workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID, *, attempt: int = 1, **kw: Any
) -> Any:
    async with session_scope(workspace_id=workspace_id) as session:
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = attempt
        kw.setdefault("profile", "base")
        return await machines.acquire(session, task, step, allow_domains=[], **kw)


async def lifecycle_machine(workspace_id: uuid.UUID, task_id: uuid.UUID) -> Machine:
    async with session_scope(workspace_id=workspace_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def lifecycle_asleep(
    workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID
) -> tuple[Any, Machine]:
    """A device Machine that ran a step (leaving a file in `/work`), went idle, then slept."""
    first = await lifecycle_acquire(workspace_id, task_id, step_id)
    await first.frame.write_bytes("/work/notes.md", b"the user's own file")
    await machines.release(first, parked=False)
    row = await lifecycle_machine(workspace_id, task_id)
    assert await machines.sleep(row.id, row.epoch, SleepReason.MANUAL) == "slept"
    return first, await lifecycle_machine(workspace_id, task_id)


MINE: set[uuid.UUID] = set()


@pytest.fixture
def only_our_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reconciler reads only this test's Machines (as `test_machine_reconcile.py` scopes it)."""
    for_reconcile = Machine.for_reconcile.__func__

    async def ours(cls: type[Machine], session: Any, ids: Any) -> list[Machine]:
        return [r for r in await for_reconcile(cls, session, ids) if r.id in MINE]

    async def our_drivers(cls: type[Machine], session: Any) -> set[str]:
        rows = await session.scalars(select(Machine).where(Machine.id.in_(list(MINE))))
        return {r.driver for r in rows.all() if r.frame_ref is not None}

    MINE.clear()
    monkeypatch.setattr(Machine, "for_reconcile", classmethod(ours))
    monkeypatch.setattr(Machine, "frame_drivers", classmethod(our_drivers))
    yield
    MINE.clear()
