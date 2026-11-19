# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Park-loop effects: cancel writes the row; silence must not reanimate a cancelled task.

The three forever-block bugs: budget cancel never wrote the row; budget
timeout auto-proceeded; pause timeout auto-resumed. These pin the effect layer the
workflow body calls - the loops themselves are in run_task and are exercised live.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.jobs import effects
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import TaskStatus

# Every test here rides the shared silent double - dispatch is incidental, not the subject.
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Park Tester")
    await db.commit()
    return row


async def _task_at(db: AsyncSession, actor: Actor, *path: TaskStatus) -> Task:
    task = await task_ops.create_task(db, actor, goal="park me")
    row = await db.get(Task, task.id)
    assert row is not None
    for status in path:
        row.transition(status)
    await db.commit()
    return row


async def _fresh(db: AsyncSession, task_id: UUID) -> Task:
    """Effects open their own session; expire the test session so get() sees the commit."""
    db.expire_all()
    row = await db.get(Task, task_id)
    assert row is not None
    return row


async def test_mark_cancelled_writes_the_row(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.WAITING)
    status = await effects.mark_cancelled(str(task.id))
    assert status == TaskStatus.CANCELLED.value
    refreshed = await _fresh(db, task.id)
    assert refreshed.state is TaskStatus.CANCELLED


async def test_mark_cancelled_is_idempotent(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED)
    status = await effects.mark_cancelled(str(task.id))
    assert status == TaskStatus.CANCELLED.value


async def test_is_cancelled_reads_the_row(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.PLANNING, TaskStatus.RUNNING)
    assert await effects.is_cancelled(str(task.id)) is False
    await effects.mark_cancelled(str(task.id))
    assert await effects.is_cancelled(str(task.id)) is True


async def test_resume_from_budget_refuses_a_cancelled_task(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED)
    status = await effects.resume_from_budget(str(task.id))
    assert status == TaskStatus.CANCELLED.value
    refreshed = await _fresh(db, task.id)
    assert refreshed.state is TaskStatus.CANCELLED


async def test_apply_clarification_cancel_writes_row(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.CLARIFYING)
    status = await effects.apply_clarification(str(task.id), {"action": "cancel", "content": None})
    assert status == "cancelled"
    refreshed = await _fresh(db, task.id)
    assert refreshed.state is TaskStatus.CANCELLED


async def test_apply_clarification_honours_already_cancelled(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.CLARIFYING, TaskStatus.CANCELLED)
    # A timed-out recv with no resolution must not reanimate a cancelled task into planning.
    status = await effects.apply_clarification(str(task.id), None)
    assert status == "cancelled"
    refreshed = await _fresh(db, task.id)
    assert refreshed.state is TaskStatus.CANCELLED


async def test_paused_is_true_only_while_paused(db: AsyncSession, actor: Actor) -> None:
    task = await _task_at(db, actor, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.PAUSED)
    assert await effects.paused(str(task.id)) is True
    await effects.mark_cancelled(str(task.id))
    assert await effects.paused(str(task.id)) is False
