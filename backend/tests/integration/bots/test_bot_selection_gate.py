# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bot publish gate on the Lead: each selection case is the real Lead, built through `build()`,
deciding one turn on the author's roster with the draft swapped in, its model scripted.

A red verdict is a 409 at publish, a green one publishes, a near-miss the bot grabs turns the
verdict red, and the gate builds the Lead alone: no router, no second classifier.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents import base as agents_base
from prismoid.db.session import session_scope
from prismoid.errors import Conflict
from prismoid.operations import bot_selection
from prismoid.operations import bots as ops
from prismoid.schemas.bots import NO_BOT, BotSource
from tests.support.cast import member

pytestmark = pytest.mark.integration

OWN = "Review this MSA against our standard terms"
NEAR_MISS = "Draft a new NDA for a contractor"
BOT_MD = """---
name: {name}
description: >
  Reviews vendor contracts against our standard terms.
  Load when: an MSA or SOW needs a clause-level read. Not for: drafting contracts.
capability: frontier-reasoner
produces: document
tools: [frame.read_file]
---
Read every clause. Compare it to the standard terms. Flag each deviation.
"""


def _source(name: str) -> BotSource:
    cases = f"selection:\n  - goal: {OWN}\n    expect: {name}\n  - goal: {NEAR_MISS}\n    expect: {NO_BOT}\n"
    return BotSource(bot_md=BOT_MD.format(name=name), cases_yaml=cases)


def _goal(messages: list[ModelMessage]) -> str:
    """The case's goal: the request line of the run's first user prompt."""
    first = messages[0]
    assert isinstance(first, ModelRequest)
    prompt = next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
    assert "# The roster" in prompt and "This workspace's own agents" in prompt, "the Lead reads the roster"
    return prompt.split("\n")[2]


def _lead(seats: dict[str, str]) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    """A scripted Lead: a goal in `seats` ends in a one-step plan on that agent; any other goal is a reply."""

    def decide(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        goal = _goal(messages)
        if goal not in seats:
            return ModelResponse(parts=[TextPart("Here is the answer.")])
        step = {"label": "s1", "agent": seats[goal], "title": "Do it", "goal": goal, "publishes": True}
        plan = {"say": "On it.", "plan": {"steps": [step], "deliverable": "files"}}
        return ModelResponse(parts=[ToolCallPart("submit_plan", plan, "call-1")])

    return decide


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str]], list[str]]:
    """Script the Lead's model; returns the names of every agent the gate builds."""
    names: list[str] = []
    real = agents_base.build

    def recording(name: str, *args: Any, **kwargs: Any) -> Any:
        names.append(name)
        return real(name, *args, **kwargs)

    def use(seats: dict[str, str]) -> list[str]:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(_lead(seats)))
        monkeypatch.setattr(agents_base, "build", recording)
        return names

    return use


async def _gate(name: str) -> tuple[bool, list[tuple[str, str, bool]]]:
    """Push `name`, score it on the gate, record the verdict; whether it is green and its cases."""
    async with session_scope() as session:
        author, _ = await member(session, "Author")
        draft = await ops.push(session, actor=author, name=name, source=_source(name))
        report = await bot_selection.test_selection(session, author, name)
        await ops.apply_eval(session, actor=author, name=name, version=draft.version, report=report)
        try:
            await ops.publish(session, actor=author, name=name)
        except Conflict:
            return False, [(c.goal, c.chosen, c.ok) for c in report.cases]
        return True, [(c.goal, c.chosen, c.ok) for c in report.cases]


async def test_a_bot_the_lead_seats_on_its_own_goals_publishes(built: Callable[..., list[str]]) -> None:
    name = f"msa-{uuid.uuid4().hex[:8]}"
    names = built({OWN: name, NEAR_MISS: "writer"})
    published, cases = await _gate(name)
    assert published, cases
    assert cases == [(OWN, name, True), (NEAR_MISS, "writer", True)]
    assert set(names) == {"lead"}, "the gate builds the Lead alone, never a router or a selector"


async def test_a_bot_the_lead_does_not_seat_is_refused_with_a_409(built: Callable[..., list[str]]) -> None:
    name = f"msa-{uuid.uuid4().hex[:8]}"
    built({OWN: "writer"})
    published, cases = await _gate(name)
    assert not published
    assert cases == [(OWN, "writer", False), (NEAR_MISS, NO_BOT, True)]


async def test_a_near_miss_the_bot_grabs_turns_the_verdict_red(built: Callable[..., list[str]]) -> None:
    name = f"msa-{uuid.uuid4().hex[:8]}"
    built({OWN: name, NEAR_MISS: name})
    published, cases = await _gate(name)
    assert not published, "an over-claiming description is red even when it wins its own goals"
    assert cases == [(OWN, name, True), (NEAR_MISS, name, False)]


async def test_a_read_before_any_plan_is_not_a_delegation(
    built: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is the first act: a Lead that reads first has kept the turn, and nothing runs.
    The read is `web_fetch`, mounted on every Lead; `web_search` exists only with an engine configured."""
    name = f"msa-{uuid.uuid4().hex[:8]}"
    built({})

    def reads(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        _goal(messages)
        return ModelResponse(parts=[ToolCallPart("web_fetch", {"url": "https://example.com/msa"}, "call-1")])

    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(reads))
    published, cases = await _gate(name)
    assert not published
    assert cases == [(OWN, NO_BOT, False), (NEAR_MISS, NO_BOT, True)]
