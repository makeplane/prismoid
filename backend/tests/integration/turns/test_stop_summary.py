# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task stopped after one of two steps says what got done: the cancel's `task.status{cancelled}` carries
an account naming the finished step and what was left."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import steering
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.plan import StepKind, StepState, TaskStatus
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


async def test_a_cancel_after_one_of_two_steps_names_what_was_done(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = Task(
            id=uuid.uuid4(), goal="Prices then a table", creator_actor_id=ada.id, criteria={}, inputs=[]
        )
        task.status = TaskStatus.RUNNING.value
        s.add(task)
        await s.flush()
        done = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            kind=StepKind.STEP.value,
            state=StepState.DONE.value,
            title="Gather prices",
        )
        left = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            kind=StepKind.STEP.value,
            state=StepState.PENDING.value,
            title="Write the table",
        )
        s.add_all([done, left])
        await s.flush()
        await TaskEvent.append(
            s,
            StepStateChanged(
                seq=await task.next_seq(s),
                task_id=TaskId(task.id),
                step_id=StepId(done.id),
                at=utcnow(),
                state=StepState.DONE,
            ),
        )
        await s.commit()
        last = await task.next_seq(s)

        assert await steering.cancel(s, ada, task.id) == TaskStatus.CANCELLED.value

        after = [e for e in await TaskEvent.replay(s, task.id, since=0) if e.seq >= last]
        said = " ".join(e.model_dump_json() for e in after)
        # Expected: the stop names what finished and what never ran.
        assert "Gather prices" in said, f"no stop summary; events after the cancel: {[e.type for e in after]}"
        assert "Write the table" in said
