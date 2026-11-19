# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A reply the token cap cut off never ends as if it were complete.

A reply cut at `finish_reason == "length"` keeps what was written and ends with one plain line, in the
output and on the live stream alike; a cut-off deliverable, structured output or tool call never
delivers at all (the run dies as a `ModelIncapable` the ladder reads).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import StreamedResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.run import CONTINUE_LINE, CUT_OFF_LINE
from prismoid.ai.deps import TaskDeps
from prismoid.errors import ModelIncapable
from prismoid.schemas.ids import StepId, TaskId
from tests.support.runners import runner_for


class _CappedStream(FunctionModel):
    """A streaming provider whose every response stops at the output limit."""

    @asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[StreamedResponse]:
        async with super().request_stream(*args, **kwargs) as response:
            response.finish_reason = "length"
            yield response


def _text_agent(model: FunctionModel) -> Agent[TaskDeps, str]:
    return Agent(model, deps_type=TaskDeps, output_type=str)


async def test_a_cut_off_text_reply_says_so() -> None:
    model = FunctionModel(
        lambda m, i: ModelResponse(parts=[TextPart("The plan has three par")], finish_reason="length")
    )

    result = await runner_for(_text_agent(model), TaskDeps()).run("go")

    assert result.output == f"The plan has three par\n\n{CUT_OFF_LINE}"


async def test_a_complete_text_reply_is_untouched() -> None:
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("Done.")], finish_reason="stop"))

    result = await runner_for(_text_agent(model), TaskDeps()).run("go")

    assert result.output == "Done."


async def test_the_stream_ends_with_the_same_honest_line() -> None:
    sink: list[Any] = []

    async def emit(event: Any) -> None:
        sink.append(event)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "The plan has "
        yield "three par"

    deps = TaskDeps(task_id=TaskId(uuid.uuid4()), step_id=StepId(uuid.uuid4()), emit_event=emit)

    result = await runner_for(_text_agent(_CappedStream(stream_function=stream)), deps).run("go")

    said = "".join(e.text for e in sink if e.type == "message.delta")
    assert said == result.output == f"The plan has three par\n\n{CUT_OFF_LINE}"
    assert sink[-1].type == "message.end"


class _Findings(BaseModel):
    summary: str


@pytest.mark.parametrize(
    "parts",
    [
        [ToolCallPart("final_result", '{"summary": "half a sent')],
        [TextPart("")],
    ],
    ids=["structured-output-cut-mid-args", "cut-before-any-output"],
)
async def test_a_cut_off_structured_output_never_delivers(parts: list[Any]) -> None:
    model = FunctionModel(lambda m, i: ModelResponse(parts=parts, finish_reason="length"))
    agent = Agent(model, deps_type=TaskDeps, output_type=ToolOutput(_Findings, name="final_result"))

    with pytest.raises(ModelIncapable):
        await runner_for(agent, TaskDeps()).run("go")


async def test_a_cut_off_deliverable_is_never_published_half() -> None:
    sink: list[Any] = []

    async def emit(event: Any) -> None:
        sink.append(event)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "The report has three par"

    deps = TaskDeps(task_id=TaskId(uuid.uuid4()), step_id=StepId(uuid.uuid4()), emit_event=emit)
    writer = Agent(_CappedStream(stream_function=stream), name="writer", deps_type=TaskDeps, output_type=str)

    with pytest.raises(ModelIncapable, match="output limit"):
        await runner_for(writer, deps).run("go")
    assert CUT_OFF_LINE not in "".join(getattr(e, "text", "") or "" for e in sink)


async def test_a_cut_off_deliverable_is_written_on_from_the_cut() -> None:
    sent: list[list[ModelMessage]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(messages)
        if len(sent) == 1:
            return ModelResponse(parts=[TextPart("The report has three par")], finish_reason="length")
        return ModelResponse(parts=[TextPart("ts, and it ends here.")], finish_reason="stop")

    writer = Agent(FunctionModel(model), name="writer", deps_type=TaskDeps, output_type=str)

    result = await runner_for(writer, TaskDeps()).run("go")

    assert result.output == "The report has three parts, and it ends here."
    assert CONTINUE_LINE in str(sent[1][-1]), "the next request writes on; it never asks again"
