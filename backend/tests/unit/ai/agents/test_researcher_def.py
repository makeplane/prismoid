# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The researcher as a definition: a citing text step agent with no file hands, a prompt in named blocks
under the step budget, and a run that ends in an answer at its request ceiling."""

from __future__ import annotations

import dataclasses
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai import config
from prismoid.ai.agents import grants
from prismoid.ai.agents.base import REGISTRY, build, get
from prismoid.ai.capabilities.citations import CitationCheck
from prismoid.ai.capabilities.forced_answer import ForcedAnswer
from prismoid.ai.deps import TaskDeps
from prismoid.conf import SearchSettings
from prismoid.schemas.guard import FORCED_ANSWER
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.first_request import fake_row, first_request

STEP_PROMPT_WORDS = (250, 500)  # the step-agent prompt's floor and budget
BLOCK_CAP = 1600  # characters in one `##` block
HANDS = frozenset(
    {"read_file", "web_fetch", "find_in_page", "note", "routine_create", "ask_user", "request_connector"}
)


@contextmanager
def _deployment(*, search: bool, ceiling: int = 200) -> Generator[None]:
    """A deployment with a search engine configured or none, and a step request ceiling."""
    current = config.current()
    engines = (
        SearchSettings(engines="searxng", urls="searxng=http://localhost:8888")
        if search
        else SearchSettings(engines="")
    )
    worker = current.worker.model_copy(update={"request_ceiling": ceiling})
    with config.use_settings(current.model_copy(update={"search": engines, "worker": worker})):
        yield


@pytest.mark.parametrize("search", [True, False])
async def test_a_researcher_request_carries_its_row_and_no_more(search: bool) -> None:
    with _deployment(search=search):
        sent = (await first_request("researcher", browser=True, image=True)).tools
    assert sent == (HANDS | {"web_search"} if search else HANDS)


async def test_the_request_follows_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bite: a tool cut from the row leaves the request."""
    monkeypatch.setitem(
        grants.GRANTS, "researcher", tuple(g for g in grants.GRANTS["researcher"] if g.tool != "note")
    )
    monkeypatch.setitem(
        REGISTRY,
        AgentName("researcher"),
        dataclasses.replace(get("researcher"), tools=grants.granted("researcher")),
    )
    with _deployment(search=False):
        sent = (await first_request("researcher", browser=False, image=False)).tools
    assert "note" not in sent and "find_in_page" in sent


def test_the_prompt_is_within_the_step_budget_in_named_blocks() -> None:
    researcher = get("researcher")
    words = len(researcher.prompt.split())
    assert STEP_PROMPT_WORDS[0] <= words <= STEP_PROMPT_WORDS[1], f"{words} words"
    assert [b.id for b in researcher.blocks] == [
        "researcher.role",
        "researcher.two-kinds-of-brief",
        "researcher.how-to-work",
        "researcher.depth",
        "researcher.citing",
        "researcher.never",
    ]
    assert all(len(b.text) <= BLOCK_CAP for b in researcher.blocks)


def test_the_prompt_teaches_the_source_ids_and_leaves_tool_law_to_the_tools() -> None:
    prompt = get("researcher").prompt
    assert "[S3]" in prompt and "find_in_page" in prompt and "note" in prompt
    assert "start_index" not in prompt and "publish(" not in prompt and "/work/" not in prompt


def test_a_built_researcher_checks_its_citations() -> None:
    with _deployment(search=False):
        runner = build("researcher", TaskDeps(frame=FakeFrame("t1")), [fake_row(ModelClass.LONG_CONTEXT)])
    kinds = {type(c) for c in runner.agent.root_capability.capabilities}
    assert {ForcedAnswer, CitationCheck} <= kinds


def _looper(asked: list[AgentInfo]) -> FunctionModel:
    """A model that keeps calling a tool while it may, and answers only when the tools are off."""

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked.append(info)
        if (info.model_settings or {}).get("tool_choice") == "none":
            return ModelResponse(parts=[TextPart("The answer, from what was read.")])
        return ModelResponse(parts=[ToolCallPart("find_in_page", {"url": "S1", "text": "revenue"})])

    return FunctionModel(answer)


def _told(messages: list[ModelMessage]) -> list[str]:
    return [
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]


async def test_the_run_ends_in_an_answer_at_its_request_ceiling() -> None:
    asked: list[AgentInfo] = []
    with _deployment(search=False, ceiling=4):
        runner = build("researcher", TaskDeps(frame=FakeFrame("t1")), [fake_row(ModelClass.LONG_CONTEXT)])
        with runner.agent.override(model=_looper(asked)):
            result = await runner.run("Who won?")
    assert result.output == "The answer, from what was read."
    assert len(asked) == 4, "every request the ceiling allows, and no more"
    assert [(i.model_settings or {}).get("tool_choice") for i in asked] == [None, None, None, "none"]
    assert asked[-1].function_tools, "the tool list stays (the cached prefix holds); only the choice is off"
    assert _told(result.all_messages()).count(FORCED_ANSWER) == 1


async def test_without_the_forced_answer_the_ceiling_kills_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bite: the same run with the capability removed dies at its ceiling with nothing."""
    plain = dataclasses.replace(get("researcher"), capabilities=())
    monkeypatch.setitem(REGISTRY, AgentName("researcher"), plain)
    with _deployment(search=False, ceiling=4):
        runner = build("researcher", TaskDeps(frame=FakeFrame("t1")), [fake_row(ModelClass.LONG_CONTEXT)])
        with runner.agent.override(model=_looper([])), pytest.raises(Exception) as died:
            await runner.run("Who won?")
    assert isinstance(died.value, UsageLimitExceeded) or isinstance(died.value.__cause__, UsageLimitExceeded)
