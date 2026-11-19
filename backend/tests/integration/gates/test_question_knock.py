# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's question knocks the inbox (ADR-100): the INPUT gate a running step opens is written to
whoever can answer it - the approval gate's audience - as a `question` item that points at the gate,
so the person who was not watching (on their phone, say) opens the task with the question in hand.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import gates
from prismoid.operations import tasks as task_ops
from prismoid.schemas.ids import StepId
from tests.support.asks import question_set

pytestmark = pytest.mark.integration


async def _task_and_step(db: AsyncSession, actor: Actor, goal: str) -> tuple[Task, PlanStep]:
    task = await task_ops.create_task(db, actor, goal=goal)
    row = await db.get(Task, task.id)
    assert row is not None
    step = PlanStep(task_id=row.id, ordinal=0, title="draft it")
    db.add(step)
    await db.flush()
    return row, step


async def test_a_question_knocks_its_creator_with_the_gate_in_hand(
    db: AsyncSession, actor: Actor, silent_dispatcher: None
) -> None:
    task, step = await _task_and_step(db, actor, "write the quarterly report")
    asked = question_set(("Which quarter should it cover?", ["Q3", "Q4"]), ("Who reads it?", []))

    gate = await gates.open_question(db, task, StepId(step.id), asked)
    await db.commit()

    knocks = list(await db.scalars(select(InboxItem).where(InboxItem.task_id == task.id)))
    assert len(knocks) == 1
    knock = knocks[0]
    assert knock.kind == InboxKind.QUESTION.value
    assert knock.actor_id == actor.id
    assert knock.gate_id == gate.id
    assert knock.title == "write the quarterly report asks: Which quarter should it cover? (+1 more)"
    assert "1. Which quarter should it cover?" in knock.body
    assert "2. Who reads it?" in knock.body


async def test_a_long_question_is_trimmed_to_a_line(
    db: AsyncSession, actor: Actor, silent_dispatcher: None
) -> None:
    task, step = await _task_and_step(db, actor, "plan the offsite " * 20)

    await gates.open_question(db, task, StepId(step.id), question_set(("Which city? " * 30, [])))
    await db.commit()

    knock = await db.scalar(select(InboxItem).where(InboxItem.task_id == task.id))
    assert knock is not None
    assert len(knock.title) <= 160
    assert knock.title.endswith("…")
