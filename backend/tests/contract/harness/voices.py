# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Scripted models: what each agent says, so a turn is deterministic.

A voice is the list of replies one agent gives, one reply per model request it makes during the
scenario (a retry or a second step of the same agent takes the next reply). A reply is built by
`says`, `writes`, `submits` or `calls` and may be several parts at once (text beside a tool call). A voice
asked for more than it has fails the scenario, and so does one left with a reply nobody asked for
(`Voice.unused`): either way the turn did something the scenario did not plan for.

Every reply is a response the agent could really make: `calls` names only a tool the agent was
given and `says` answers only an agent that may answer in text, so no scenario records an
unknown-tool retry as if it were the contract.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ModelResponsePart, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from prismoid.schemas.outputs import SubmittedDocument
from prismoid.schemas.plan import DeliverableKind, PlanProposal, ProposedStep

type Reply = Callable[[AgentInfo], list[ModelResponsePart] | Awaitable[list[ModelResponsePart]]]


def says(text: str) -> Reply:
    """Plain text: the answer of a reply turn, a researcher's answer (the system publishes it), or a
    step's closing line."""

    def reply(info: AgentInfo) -> list[ModelResponsePart]:
        assert info.allow_text_output, f"this agent cannot answer in text: {text!r} would be retried"
        return [TextPart(text)]

    return reply


def writes(text: str, title: str = "Memo") -> Reply:
    """A writer's document: its `submit_document` call, the title above `text`."""
    return submits(SubmittedDocument(title=title, content=text))


def submits(output: BaseModel) -> Reply:
    """The agent's typed output (a plan, a title): the call of its one output tool."""
    return lambda info: [ToolCallPart(info.output_tools[0].name, output.model_dump(mode="json"))]


def calls(tool: str, /, **args: object) -> Reply:
    """One tool call, by the tool's name as the model sees it; the agent must have been given it."""

    def reply(info: AgentInfo) -> list[ModelResponsePart]:
        given = sorted(t.name for t in (*info.function_tools, *info.output_tools))
        assert tool in given, f"{tool!r} is not a tool this agent was given: {given}"
        return [ToolCallPart(tool, args)]

    return reply


def publishes(filename: str, kind: str = "document") -> tuple[Reply, Reply, Reply]:
    """A step that writes a file, publishes it and says it is done: three requests (not a writer's or a researcher's)."""
    path = f"/work/{filename}"
    return (
        calls("write_file", path=path, content="# A page\n"),
        calls("publish", path=path, type=kind, title=filename),
        says("Done."),
    )


def planned(*steps: ProposedStep) -> Reply:
    """The planner's answer: these steps, in this order."""
    return submits(PlanProposal(must=["it is done"], steps=list(steps)))


def _lead_plan(*steps: ProposedStep) -> dict[str, object]:
    return PlanProposal(steps=list(steps), deliverable=DeliverableKind.FILES).model_dump(mode="json")


def leads(*steps: ProposedStep) -> Reply:
    """The Lead's plan ending: these steps, in this order, and the line the person reads now."""
    return calls("submit_plan", say="Writing it now.", plan=_lead_plan(*steps))


def revises(*steps: ProposedStep) -> Reply:
    """The Lead's replan seat after a steer: these steps replace the ones not yet started."""
    return calls("revise_plan", say="Changing the rest.", plan=_lead_plan(*steps))


def step(label: str, agent: str, *after: str) -> ProposedStep:
    """One proposed step: `after` are the labels it waits for; the wording is filler."""
    return ProposedStep(
        label=label,
        agent=agent,
        title=f"Step {label}",
        goal=f"do step {label}",
        acceptance=["it exists"],
        depends_on=list(after),
        publishes=True,
    )


async def parts_of(reply: Reply, info: AgentInfo) -> list[ModelResponsePart]:
    """A reply's parts: it may act first (`people.meanwhile`) and so be awaitable."""
    parts = reply(info)
    return await parts if inspect.isawaitable(parts) else parts


def both(*replies: Reply) -> Reply:
    """Several parts in one response: text beside a tool call."""

    async def respond(info: AgentInfo) -> list[ModelResponsePart]:
        return [part for reply in replies for part in await parts_of(reply, info)]

    return respond


class Voice:
    """One agent's scripted replies for one run of a scenario."""

    def __init__(self, agent: str, replies: Sequence[Reply], *, repeats: bool = False) -> None:
        self.agent = agent
        self._replies = list(replies)
        self._repeats = repeats  # machinery: the last reply answers every later request
        self.requests = 0
        self.faults: list[str] = []  # a refused reply, kept: the engine may swallow it as a failed step

    @property
    def unused(self) -> int:
        """How many scripted replies no request asked for."""
        return len(self._replies) - self.requests

    def model(self) -> FunctionModel:
        """A fresh model over this voice: every agent built for the scenario shares its replies."""
        return FunctionModel(self._respond, stream_function=self._stream)

    async def _next(self, info: AgentInfo) -> list[ModelResponsePart]:
        try:
            if self._repeats and self.requests >= len(self._replies):
                self.requests += 1
                return await parts_of(self._replies[-1], info)
            if self.requests >= len(self._replies):
                raise AssertionError(
                    f"the scenario scripted {len(self._replies)} replies for {self.agent!r} and the turn asked for more"
                )
            reply = self._replies[self.requests]
            self.requests += 1
            return await parts_of(reply, info)
        except AssertionError as fault:
            self.faults.append(str(fault))
            raise

    async def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=await self._next(info))

    async def _stream(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        for i, part in enumerate(await self._next(info)):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {
                    i: DeltaToolCall(
                        name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id
                    )
                }
