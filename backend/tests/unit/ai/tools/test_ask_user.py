# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""M3.5a - `ask_user` defers, and the run resumes with the answers.

This is the *mechanism* of a mid-run pause, unit-tested with no database and no worker: the tool
raises `CallDeferred`, the run ends with a `DeferredToolRequests` carrying the question BATCH
(the one-batch law, held mid-run - the single-question shape produced
fifteen serial gates on one live task), and a resume with the answers finishes the run. A real gate and a real park are wired on top of this.
"""

from __future__ import annotations

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools.ask import ASK_USER, with_ask_user

BATCH = {
    "questions": [
        {"question": "Which currency?", "options": ["eur", "usd"]},
        {"question": "How formal should it read?", "options": []},
    ]
}


def _model_that_asks_then_answers() -> FunctionModel:
    """First turn: call `ask_user` with the whole batch. After the answers: the final text."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        already_asked = any(
            isinstance(part, ToolCallPart) and part.tool_name == ASK_USER
            for m in messages
            if isinstance(m, ModelResponse)
            for part in m.parts
        )
        if already_asked:
            return ModelResponse(parts=[TextPart("Done, in euros, kept casual.")])
        return ModelResponse(parts=[ToolCallPart(ASK_USER, BATCH)])

    return FunctionModel(respond)


def _agent() -> Agent[None, object]:
    toolset: FunctionToolset[object] = FunctionToolset()
    with_ask_user(toolset)
    return Agent(_model_that_asks_then_answers(), output_type=[str, DeferredToolRequests], toolsets=[toolset])


async def test_ask_user_defers_with_the_batch_and_resumes_with_the_answers() -> None:
    agent = _agent()

    paused = await agent.run("Summarise the brief.")

    # 1. The run stopped, and it stopped on ONE call carrying EVERY question - not a drip.
    assert isinstance(paused.output, DeferredToolRequests)
    (call,) = paused.output.calls
    assert call.tool_name == ASK_USER
    asked = call.args_as_dict()["questions"]
    assert [q["question"] for q in asked] == ["Which currency?", "How formal should it read?"]
    assert asked[0]["options"] == ["eur", "usd"]
    assert asked[1]["options"] == []

    # 2. Resuming with the answers - ONE string for the one call - finishes the run.
    combined = "The user answered your questions:\n1. Which currency?\n   → eur\n2. How formal should it read?\n   → casual"
    resumed = await agent.run(
        message_history=paused.all_messages(),
        deferred_tool_results=DeferredToolResults(calls={call.tool_call_id: combined}),
    )
    assert resumed.output == "Done, in euros, kept casual."


async def test_an_agent_without_the_tool_cannot_ask() -> None:
    """`ask_user` exists only in a toolset it was explicitly added to (`can_ask_user`)."""
    bare: FunctionToolset[object] = FunctionToolset()
    assert ASK_USER not in [t.name for t in bare.tools.values()]
    with_ask_user(bare)
    assert ASK_USER in [t.name for t in bare.tools.values()]


async def test_a_blank_question_is_retried_never_deferred() -> None:
    """A batch whose question is blank must not defer: the park would find no readable question,
    fall through every rule, and end the run - its narration the requests' repr, and an approval
    sharing the turn silently dropped. The tool's own schema refuses it, so the model is asked
    again in the same run."""
    turns: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turns.append(1)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart(ASK_USER, {"questions": [{"question": "   "}]})])
        return ModelResponse(parts=[TextPart("Proceeding on stated assumptions.")])

    toolset = with_ask_user(FunctionToolset())
    agent = Agent(FunctionModel(respond), toolsets=[toolset], output_type=[str, DeferredToolRequests])

    result = await agent.run("Summarise the brief.")

    assert result.output == "Proceeding on stated assumptions."
    assert len(turns) == 2, "the blank batch went back to the model as a retry"
