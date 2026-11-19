# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`AgentRunner`, the one run loop: a response that streams text and then calls an output tool ends on
the tool's output in one request (a `run_stream` driver would end on the text), and the run's span
carries the run's ids, never content."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from pydantic import BaseModel
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from prismoid.ai.agents.base import AgentDef
from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.ids import AgentName, StepId, TaskId
from prismoid.schemas.model_class import ModelClass


class Plan(BaseModel):
    steps: list[str]


PLAN_ARGS = '{"steps": ["read", "write"]}'


def _text_then_plan(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("Here is my plan."), ToolCallPart("submit_plan", PLAN_ARGS)])


async def _streamed_text_then_plan(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | DeltaToolCalls]:
    yield "Here is my plan."
    yield {0: DeltaToolCall(name="submit_plan", json_args=PLAN_ARGS)}


def _runner(deps: TaskDeps) -> AgentRunner:
    model = FunctionModel(_text_then_plan, stream_function=_streamed_text_then_plan)
    agent = Agent(
        model, deps_type=TaskDeps, output_type=[str, ToolOutput(Plan, name="submit_plan")], name="lead"
    )
    definition = AgentDef(name=AgentName("lead"), model_class=ModelClass.FAST_EXECUTOR, description="plans")
    return AgentRunner(agent, deps, definition)


async def test_text_then_an_output_tool_ends_on_the_tool_in_one_request() -> None:
    result = await _runner(TaskDeps()).run("plan it")
    assert result.output == Plan(steps=["read", "write"]), "the output tool wins over the text before it"
    assert result.usage.requests == 1


async def test_the_run_carries_its_ids_and_no_content() -> None:
    task, step = TaskId(uuid.uuid4()), StepId(uuid.uuid4())
    result = await _runner(TaskDeps(task_id=task, step_id=step)).run("plan it")
    assert result.metadata == {"task_id": str(task), "step_id": str(step)}
