# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An express turn still feeds memory_extract after delivery: "remember I prefer tables" is a quick
turn, and it is kept in the person's own words. A Project task on the same door keeps nothing.

The model is faked (`planning.run_light_agent`), and `run_task`'s own body runs with its plan, wave and
delivery effects stubbed; the express verdict, the post-delivery effects, the fence and the ledger rows are real.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest

from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import Memory, MemoryScope
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import effects
from prismoid.jobs import run_task as run_task_job
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import MemoryWritten
from prismoid.schemas.outputs import MemoryExtract, QuotedMemory
from tests.support.cast import member

pytestmark = pytest.mark.integration

ASK = "remember I prefer tables"


def _model(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """memory_extract, faked to keep the person's quote; the turn's other effects, stubbed. Returns the briefs."""
    briefs: list[str] = []

    async def extract(session: object, task: object, name: str, brief: str) -> object:
        assert name == "memory_extract"
        briefs.append(brief)
        return MemoryExtract(
            items=[QuotedMemory(kind="preference", lasts="standing", quote="I prefer tables")]
        )

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    async def no(*args: object, **kwargs: object) -> bool:
        return False

    async def no_steps(task_id: str) -> list[str]:
        return []

    async def delivered(task_id: str, summary: str, turn: int = 1) -> dict[str, Any]:
        return {"verdict": "delivered", "replaced": []}

    monkeypatch.setattr("prismoid.operations.planning.run_light_agent", extract)
    for name, stub in {
        "mark_dequeued": nothing,
        "mark_planning": nothing,
        "plan_task": nothing,
        "mark_running": nothing,
        "fanout_enabled": no,
        "frontier": no_steps,
        "pause_held": no,
        "is_cancelled": no,
        "deliver_task": delivered,
        "conversation_report": nothing,
        "title_task": nothing,
    }.items():
        monkeypatch.setattr(effects, name, stub)
    return briefs


async def _express_turn(*, in_project: bool) -> tuple[uuid.UUID, uuid.UUID]:
    """Mint the ask (in a Project or not), check the door calls it express, and run the turn through
    `run_task`'s own body. Returns (creator id, task id)."""
    async with session_scope() as session:
        actor, _ = await member(session, "Tabler")
        await session.flush()
        project_id = None
        if in_project:
            project_id = (
                await project_ops.create(session, actor, name=f"m-{uuid.uuid4().hex[:6]}")
            ).project.id
        task = await task_ops.create_task(session, actor, goal=ASK, project_id=project_id, clarify="never")
        await session.commit()
        actor_id, task_id = actor.id, task.id

    express = await effects.turn_is_express(str(task_id))
    assert express, "the quick ask must take the express door"
    body = inspect.unwrap(run_task_job.run_task)  # the workflow's own body, under DBOS's wrappers
    assert await body(str(task_id), 1) == "delivered"
    return actor_id, task_id


async def test_an_express_turn_keeps_the_persons_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    briefs = _model(monkeypatch)
    actor_id, task_id = await _express_turn(in_project=False)

    assert len(briefs) == 1 and ASK in briefs[0]
    async with row_scope(Task, task_id) as session:
        kept = [m.text for m in await Memory.for_scope(session, MemoryScope.USER, user_id=actor_id, limit=10)]
        written = [e for e in await TaskEvent.replay(session, task_id) if isinstance(e, MemoryWritten)]
    assert kept == ["I prefer tables"]
    assert len(written) == 1 and written[0].count == 1


async def test_an_express_project_turn_keeps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    briefs = _model(monkeypatch)
    actor_id, task_id = await _express_turn(in_project=True)

    assert briefs == [], "a Project task never reaches the extractor"
    async with row_scope(Task, task_id) as session:
        kept = [m.text for m in await Memory.for_scope(session, MemoryScope.USER, user_id=actor_id, limit=10)]
        written = [e for e in await TaskEvent.replay(session, task_id) if isinstance(e, MemoryWritten)]
    assert kept == []
    assert len(written) == 1 and written[0].skipped == "project"
