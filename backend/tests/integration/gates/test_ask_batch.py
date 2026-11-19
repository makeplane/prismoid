# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One BATCH per mid-run gate, answers paired back by prompt, and the ask budget's ledger.

The live failure this pins against: a step needing five facts opened five serial
"One question before I continue" gates - ask, park, answer, resume, ask again, fifteen rounds
on one task. The law (ONE numbered batch per gate) now holds mid-run: `ask_user` carries a
batch, `open_question` builds one carousel gate from it, `store_answer` hands every answer back
paired with its prompt, and `Gate.count_for_step` is the ledger the engine's ask budget reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import execution, gates
from prismoid.operations import tasks as task_ops
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateSpec, QuestionType
from tests.support.asks import question_set
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Batch Asker")
    await db.commit()
    return row


async def _task_and_step(db: AsyncSession, actor: Actor) -> tuple[Task, PlanStep]:
    task = await task_ops.create_task(db, actor, goal="build the plan collection")
    row = await db.get(Task, task.id)
    assert row is not None
    step = PlanStep(task_id=row.id, ordinal=0, title="organize the plan")
    db.add(step)
    await db.flush()
    return row, step


async def test_open_question_builds_ONE_gate_from_the_whole_batch(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    task, step = await _task_and_step(db, actor)

    gate = await gates.open_question(
        db,
        task,
        StepId(step.id),
        question_set(
            ("Which currency?", ["eur", "usd"]),
            ("Anything else I should know?", []),
            ("18, 24, or 30+ months?", ["18", "24", "30+"]),
        ),
    )

    spec = GateSpec.model_validate(gate.spec)
    assert spec.title == "3 questions before I continue"
    assert spec.questions is not None
    asked = spec.questions.questions
    assert [q.id for q in asked] == ["q1", "q2", "q3"]
    assert asked[0].type is QuestionType.SINGLE_SELECT  # options ⇒ a choice
    assert asked[1].type is QuestionType.TEXT  # no options ⇒ free text
    assert [o.value for o in asked[2].options] == ["18", "24", "30+"]


async def test_store_answer_pairs_every_answer_with_its_prompt(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    task, step = await _task_and_step(db, actor)
    step.resume_state = {"history": "…", "tool_call_id": "tc1"}
    await gates.open_question(
        db,
        task,
        StepId(step.id),
        question_set(("Which currency?", ["eur", "usd"]), ("How formal?", [])),
    )
    await db.commit()

    await execution.store_answer(
        db, step.id, {"action": "accept", "content": {"q1": "eur", "q2": "keep it casual"}}
    )

    state = step.resume_state
    assert state is not None
    assert state["answer"] == (
        "The user answered your questions:\n1. Which currency?\n   → eur\n2. How formal?\n   → keep it casual"
    )
    assert state["cancelled"] is False


async def test_a_single_answer_stays_bare(db: AsyncSession, actor: Actor, silent_dispatcher: object) -> None:
    """The one-question batch reads exactly like the old shape did - no ceremony around it."""
    task, step = await _task_and_step(db, actor)
    step.resume_state = {"history": "…", "tool_call_id": "tc1"}
    await gates.open_question(db, task, StepId(step.id), question_set(("Which currency?", ["eur", "usd"])))
    await db.commit()

    await execution.store_answer(db, step.id, {"action": "accept", "content": {"q1": "eur"}})

    state = step.resume_state
    assert state is not None
    assert state["answer"] == "eur"


async def test_count_for_step_is_the_ask_budgets_ledger(
    db: AsyncSession, actor: Actor, silent_dispatcher: object
) -> None:
    task, step = await _task_and_step(db, actor)
    assert await Gate.count_for_step(db, step.id) == 0

    await gates.open_question(db, task, StepId(step.id), question_set(("First round?", [])))
    assert await Gate.count_for_step(db, step.id) == 1

    await gates.open_question(db, task, StepId(step.id), question_set(("Second round?", [])))
    assert await Gate.count_for_step(db, step.id) == 2
    # Past MAX_STEP_QUESTION_GATES the engine auto-answers with OUT_OF_QUESTIONS instead of
    # parking (execute_step's Deferral branch) - the constant is the contract:
    assert execution.MAX_STEP_QUESTION_GATES == 2
    assert "best judgment" in execution.OUT_OF_QUESTIONS
