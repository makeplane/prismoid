# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A settled turn's typed ending rides its settle event: a delivery with a step that ended partly
done reads `partial` on `task.delivered`, one without reads `done`, an agent's could-not failure
reads `could_not` on `task.status`, and a system fault carries none."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution
from prismoid.schemas.events import StepStateChanged, TaskDelivered, TaskStatusChanged
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.outputs import OutcomeStatus
from prismoid.schemas.plan import StepKind, StepState, TaskEnding, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _ran(outcome: OutcomeStatus | None) -> uuid.UUID:
    """A running task whose one step is done, its done frame wearing `outcome`."""
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = Task(
            id=uuid.uuid4(), goal="Find three flights", creator_actor_id=ada.id, criteria={}, inputs=[]
        )
        task.status = TaskStatus.RUNNING.value
        s.add(task)
        await s.flush()
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            kind=StepKind.STEP.value,
            state=StepState.DONE.value,
            title="find",
        )
        s.add(step)
        await s.flush()
        frame = StepStateChanged(
            seq=await task.next_seq(s),
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            at=utcnow(),
            state=StepState.DONE,
            outcome=outcome,
        )
        await TaskEvent.append(s, frame)
        await s.commit()
        return task.id


async def _settled[E: TaskDelivered | TaskStatusChanged](task_id: uuid.UUID, kind: type[E]) -> tuple[str, E]:
    """The task's status, and its last settle frame of `kind`."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        frames = [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, kind)]
        return task.status, frames[-1]


@pytest.mark.parametrize(("outcome", "ending"), [("partial", TaskEnding.PARTIAL), (None, TaskEnding.DONE)])
async def test_a_delivery_carries_its_turns_ending(outcome: OutcomeStatus | None, ending: TaskEnding) -> None:
    task_id = await _ran(outcome)
    async with row_scope(Task, task_id) as s:
        assert (await execution.deliver(s, task_id, "Partly done.", turn=1)).verdict == "delivered"
        await s.commit()
    status, frame = await _settled(task_id, TaskDelivered)
    assert (status, frame.ending) == (TaskStatus.DELIVERED, ending)


async def test_an_agents_could_not_fails_the_task_reading_could_not() -> None:
    task_id = await _ran(None)
    async with row_scope(Task, task_id) as s:
        await execution.fail(
            s, task_id, "operator could not do this: 429", turn=1, ended=TaskEnding.COULD_NOT
        )
        await s.commit()
    status, frame = await _settled(task_id, TaskStatusChanged)
    assert (status, frame.status, frame.ending) == (
        TaskStatus.FAILED,
        TaskStatus.FAILED,
        TaskEnding.COULD_NOT,
    )


async def test_a_system_fault_fails_with_no_ending() -> None:
    task_id = await _ran(None)
    async with row_scope(Task, task_id) as s:
        await execution.fail(s, task_id, "RuntimeError: boom", turn=1)
        await s.commit()
    status, frame = await _settled(task_id, TaskStatusChanged)
    assert (status, frame.ending) == (TaskStatus.FAILED, None)
