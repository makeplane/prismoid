# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step starts as tainted as the task's other steps read: a Drive read in step 1 makes step 2's mail
send ask, while a step that only read the person's own files starts clean."""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution.equip import taint_floor
from prismoid.schemas.egress import Taint
from prismoid.schemas.events import ToolCalled
from prismoid.schemas.ids import StepId, TaskId
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _call(s: Any, task: Task, step: PlanStep, tool: str, *, ok: bool = True) -> None:
    await TaskEvent.append(
        s,
        ToolCalled(
            seq=await task.next_seq(s),
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            at=utcnow(),
            tool=tool,
            ok=ok,
        ),
    )


async def test_a_step_starts_at_what_the_tasks_other_steps_read(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        actor, _ = await member(s, "Ada")
        made = await task_ops.create_task(s, actor, goal="read my Q3 OKRs in Drive and mail them to Sam")
        task = await s.get(Task, made.id)
        assert task is not None
        read, send = PlanStep(task_id=task.id, ordinal=0), PlanStep(task_id=task.id, ordinal=1)
        s.add_all([read, send])
        await s.flush()

        await _call(s, task, read, "frame.read_file")
        await _call(s, task, read, "web.fetch", ok=False)  # a fetch that failed read nothing
        await _call(s, task, send, "gdrive_search")  # the step's own read is its own watermark's
        assert await taint_floor(s, task, send) is Taint.USER, "the person's own files are not outside text"

        await _call(s, task, read, "gdrive_read_file")
        assert await taint_floor(s, task, send) is Taint.THIRD_PARTY, (
            "a connected service's text carries over"
        )
        assert await taint_floor(s, task, read) is Taint.THIRD_PARTY, "the send step's read reaches a re-run"
