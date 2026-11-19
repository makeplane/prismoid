# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Drill D5 on Prismoid's real step (ADR-089): cancel is the only hard stop, and it is terminal. A writer's
step on paper runs the real `execute_step` with only the model scripted; the person cancels the task
while the provider serves request 2. The step stops at its next safe point, buys nothing after the
cancel, leaves no call-log rows, and a re-entry (a recovered workflow's body) never replays it."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import execution, steering
from prismoid.operations.call_log import PgCallLog
from prismoid.operations.execution import harvest
from prismoid.schemas.ids import StepId
from tests.support.rigs.durability_drill import World
from tests.support.rigs.machine_execution_rig import task_with_steps

pytestmark = [pytest.mark.integration, pytest.mark.drill, pytest.mark.xdist_group("call-log-drills")]

CANCELLED_AT = 2  # the request being served when the person cancels


async def _cancel(task_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        person = await session.get(Actor, task.creator_actor_id)
        assert person is not None
        await steering.cancel(session, person, task_id)


async def _attempt(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    """One pass of the step's ladder: what it said."""
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


async def test_a_cancel_mid_step_stops_it_and_nothing_replays(
    monkeypatch: pytest.MonkeyPatch, silent_dispatcher: None
) -> None:
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    world = World([[("read_file", {"path": f"/work/notes-{n}.md"})] for n in range(1, 5)])
    scripted = world.model().stream_function
    assert scripted is not None

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        """The scripted provider, the person cancelling while it serves request 2."""
        if sum(isinstance(m, ModelResponse) for m in messages) + 1 == CANCELLED_AT:
            await _cancel(task_id)
        async for chunk in scripted(messages, info):
            yield chunk

    monkeypatch.setattr(agents_base, "resolve", lambda *_, **__: FunctionModel(stream_function=stream))
    monkeypatch.setattr(harvest, "extract_previews", _no_previews)

    said = await _attempt(task_id, step_id)
    assert "the task ended" in said, said
    assert world.provider_calls == CANCELLED_AT, "nothing was bought after the cancel"
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.state != "done"
        task = await session.get(Task, task_id)
        assert task is not None
        log = PgCallLog(task.workspace_id)
    assert (await log.latest(StepId(step_id))).key is None, "the cancelled attempt left no rows"

    said = await _attempt(task_id, step_id)
    assert "the task ended" in said, said
    assert world.provider_calls == CANCELLED_AT, "a cancelled attempt is never replayed"
