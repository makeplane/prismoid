# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`bot_lesson` from a run proposes at most three cases, and its brief opens with its kind.

The real agent is built and driven by a scripted model: a lesson carrying a fourth case is refused
by the output schema and the model is asked again, so no run can grow a bot's eval set by more than
three cases. No database, no live model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.operations import bot_lessons
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit


class _Session:
    """Only what `bot_lesson` touches: the commit that releases the snapshot before the model call."""

    commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _lesson(n_cases: int) -> dict[str, Any]:
    cases = [{"goal": f"Review supplier contract {i}", "expect": "contract-reviewer"} for i in range(n_cases)]
    return {"summary": "Ranks findings by exposure.", "instructions": "Read every clause.", "cases": cases}


async def _run_lesson(monkeypatch: pytest.MonkeyPatch, *answers: int) -> tuple[Any, list[list[ModelMessage]]]:
    """Run `bot_lessons.bot_lesson` from a run, the model answering with `answers` cases in turn."""
    seen: list[list[ModelMessage]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        args = _lesson(answers[len(seen) - 1])
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])

    row = ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get("bot_lesson").model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )
    runner = build("bot_lesson", TaskDeps(task_id=None), [row])

    async def machinery(session: object, seat: str) -> Any:
        assert seat == "bot_lesson"
        return runner

    monkeypatch.setattr(bot_lessons, "machinery_agent", machinery)
    session = _Session()
    with runner.agent.override(model=FunctionModel(model)):
        lesson = await bot_lessons.bot_lesson(cast(AsyncSession, session), "from_run", "# The bot\n")
    assert session.commits == 1
    return lesson, seen


async def test_a_lesson_from_a_run_proposes_at_most_three_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    lesson, seen = await _run_lesson(monkeypatch, 4, 3)
    assert len(lesson.cases) == 3
    assert len(seen) == 2, "the four-case lesson is refused and asked again"


async def test_the_brief_opens_with_the_lesson_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    _, seen = await _run_lesson(monkeypatch, 1)
    first = seen[0][-1]
    assert isinstance(first, ModelRequest)
    prompts = [p.content for p in first.parts if isinstance(p, UserPromptPart)]
    assert prompts == ["Source: from_run\n\n# The bot\n"]
