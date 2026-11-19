# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Machine: one per task, workspace-fenced by a composite FK, cascading with its task (ADR-063)."""

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from prismoid.db.base import utcnow
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import TenantUnbound
from prismoid.operations import tasks as task_ops
from prismoid.schemas.machines import MachineState
from tests.support.cast import a_workspace, member

pytestmark = pytest.mark.integration


def _row(task: Task, **kw: object) -> Machine:
    fields: dict[str, object] = {
        "workspace_id": task.workspace_id,
        "task_id": task.id,
        "driver": "docker",
        "profile": "base",
        "state": MachineState.IDLE,
        "state_since": utcnow(),
        "last_active_at": utcnow(),
    }
    fields.update(kw)
    return Machine(**fields)


async def test_one_machine_per_task(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="one machine")
        s.add(_row(task))
        await s.commit()
        s.add(_row(task))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_a_machine_cannot_point_at_another_workspaces_task(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="fenced")
        await s.commit()
        _, other = await a_workspace(s, "Other")
        s.add(_row(task, workspace_id=other.id))  # the composite (task_id, workspace_id) FK refuses it
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_deleting_the_task_deletes_its_machine(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="cascade")
        row = _row(task)
        s.add(row)
        await s.commit()
        machine_id = row.id
        await s.execute(delete(Task).where(Task.id == task.id))
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        assert await s.get(Machine, machine_id) is None


async def test_for_task_is_fenced_and_warm_count_counts_warm_states(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        tasks = [await task_ops.create_task(s, ada, goal=f"t{i}") for i in range(4)]
        for task, state in zip(tasks, ("idle", "running", "asleep", "gone"), strict=True):
            s.add(_row(task, state=state))
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        assert (await Machine.for_task(s, tasks[0].id)) is not None
        assert await Machine.warm_count(s) == 2
        lru = await Machine.lru_idle(s)
        assert lru is not None and lru.task_id == tasks[0].id
    async with session_scope(workspace_id=uuid.uuid4()) as s:  # a stranger workspace sees nothing
        assert (await Machine.for_task(s, tasks[0].id)) is None
        assert await Machine.warm_count(s) == 0
        assert (await Machine.lru_idle(s)) is None
    async with session_scope() as s:  # an unbound unit of work reads nothing: it fails closed
        with pytest.raises(TenantUnbound):
            await Machine.warm_count(s)
        with pytest.raises(TenantUnbound):
            await Machine.lru_idle(s)


async def test_lru_idle_rereads_the_row_it_locks(silent_dispatcher: None) -> None:
    """The cap's eviction pick is `FOR UPDATE` and must see the committed row, never a stale
    identity-map copy (the session keeps objects across commits: expire_on_commit=False)."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="stale")
        s.add(_row(task, epoch=1))
        await s.commit()
    async with session_scope(workspace_id=workspace.id) as s:
        stale = await Machine.lru_idle(s)
        assert stale is not None and stale.epoch == 1
        await s.commit()
        async with session_scope(workspace_id=workspace.id) as other:  # another worker moves the epoch on
            moved = await Machine.for_task(other, task.id)
            assert moved is not None
            moved.epoch = 2
            await other.commit()
        fresh = await Machine.lru_idle(s)
        assert fresh is not None and fresh is stale and fresh.epoch == 2
