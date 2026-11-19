# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A conversation reply on the real build, its model a script (`model_build`): the bot's tools mount
as they do in production, and `decide` plays the model's one choice for the reply."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from prismoid.ai.agents.base import build, definition_for
from prismoid.ai.agents.run import AgentRunner, Start
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.message import Message
from prismoid.db.session import row_scope
from prismoid.operations import chat as chat_ops
from prismoid.operations import chat_reply
from tests.support.first_request import fake_row

# The model's choice for one reply: a tool and its arguments, or None to answer in text.
type Choice = tuple[str, dict[str, Any]] | None
# What the script sees: the reply's brief and the tools the reply was sent.
type Decide = Callable[[str, frozenset[str]], Choice]

REPLY = "Here is my answer."


@dataclass(frozen=True)
class _Scripted:
    """The real runner, its model swapped for `FunctionModel` for each run."""

    runner: AgentRunner
    model: FunctionModel

    async def run(self, start: Start, **kwargs: Any) -> Any:
        with self.runner.agent.override(model=self.model):
            return await self.runner.run(start, **kwargs)


def _script(decide: Decide, heard: list[str]) -> FunctionModel:
    def choose(messages: list[ModelMessage], info: AgentInfo) -> Choice:
        parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
        returns = [str(p.content) for p in parts if isinstance(p, ToolReturnPart)]
        if returns:
            heard[:] = returns
            return None
        brief = "\n".join(str(getattr(p, "content", "")) for p in parts)
        return decide(brief, frozenset(t.name for t in info.function_tools))

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        choice = choose(messages, info)
        if choice is None:
            return ModelResponse(parts=[TextPart(REPLY)])
        return ModelResponse(parts=[ToolCallPart(choice[0], choice[1], "call-1")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        choice = choose(messages, info)
        if choice is None:
            yield REPLY
            return
        yield {0: DeltaToolCall(name=choice[0], json_args=json.dumps(choice[1]), tool_call_id="call-1")}

    return FunctionModel(respond, stream_function=stream)


def model_build(decide: Decide, heard: list[str] | None = None) -> Callable[..., _Scripted]:
    """A `build()` stand-in for the reply lane: the real agent for the bot, scripted by `decide`;
    `heard` collects what the tools said back to the model."""

    def build_(name: str, deps: TaskDeps, *_args: Any, **_kwargs: Any) -> _Scripted:
        row = fake_row(definition_for(name, deps).model_class)
        return _Scripted(build(name, deps, [row]), _script(decide, heard if heard is not None else []))

    return build_


async def reply_choosing(
    monkeypatch: pytest.MonkeyPatch,
    cid: uuid.UUID,
    seq: int,
    tool: str | None,
    args: dict[str, Any] | None = None,
    heard: list[str] | None = None,
) -> frozenset[str]:
    """Answer line `seq` with a model that calls `tool` when it is sent (else replies); returns the
    tools the reply was sent, and fills `heard` with what the tool said back to the model."""
    seen: list[frozenset[str]] = []

    def decide(_brief: str, tools: frozenset[str]) -> Choice:
        seen.append(tools)
        return (tool, args or {}) if tool in tools else None

    monkeypatch.setattr(chat_reply, "build", model_build(decide, heard))
    async with row_scope(Conversation, cid) as session:
        assert await chat_ops.reply(session, cid, seq) == "done"
        failed = [m.detail for m in await Message.since(session, cid) if m.kind == "failed"]
    assert seen, f"the reply never reached the model: {failed}"
    return seen[0]
