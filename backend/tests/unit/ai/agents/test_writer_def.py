# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The writer as a definition: a text deliverable step agent whose request carries only its grants row, under a
prompt budget, in named blocks. Replaces the tool-set pin `test_text_deliverable` held."""

from __future__ import annotations

import dataclasses
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai import config
from prismoid.ai.agents import grants
from prismoid.ai.agents.base import REGISTRY, build, get
from prismoid.ai.deps import TaskDeps
from prismoid.conf import SearchSettings
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import SUBMIT_DOCUMENT
from prismoid.schemas.plan import LaneRole
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.first_request import fake_row, first_request

STEP_PROMPT_WORDS = 500  # the step-agent prompt budget
BLOCK_CAP = 1600  # characters in one `##` block
HANDS = frozenset(
    {"read_file", "web_fetch", "find_in_page", "routine_create", "ask_user", "request_connector"}
)


@contextmanager
def _search(on: bool) -> Generator[None]:
    """A deployment with a search engine configured, or with none."""
    engines = (
        SearchSettings(engines="searxng", urls="searxng=http://localhost:8888")
        if on
        else SearchSettings(engines="")
    )
    with config.use_settings(config.current().model_copy(update={"search": engines})):
        yield


def over_budget(prompt: str) -> bool:
    """Whether a role prompt is past the step-agent budget."""
    return len(prompt.split()) > STEP_PROMPT_WORDS


@pytest.mark.parametrize("search", [True, False])
async def test_a_writer_request_carries_no_tool_it_is_not_granted(search: bool) -> None:
    """Offered every closure, browser hands and an image model, the request still carries only the row."""
    with _search(search):
        sent = (await first_request("writer", browser=True, image=True)).tools
    assert sent == (HANDS | {"web_search"} if search else HANDS)


async def test_the_request_follows_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bite: a tool cut from the row leaves the request."""
    row = tuple(g for g in grants.GRANTS["writer"] if g.tool != "find_in_page")
    monkeypatch.setitem(grants.GRANTS, "writer", row)
    monkeypatch.setitem(
        REGISTRY, AgentName("writer"), dataclasses.replace(get("writer"), tools=grants.granted("writer"))
    )
    with _search(True):
        sent = (await first_request("writer", browser=True, image=True)).tools
    assert "find_in_page" not in sent and "web_fetch" in sent


@pytest.mark.parametrize("lane_role", [None, "fleet_table"])
async def test_a_document_citing_an_unread_source_is_sent_back(lane_role: LaneRole | None) -> None:
    """The writer and its fleet table: a submitted document that cites an id no read was given goes back."""
    drafts = ["Revenue grew [S9].", "Revenue grew [S1]."]
    asked: list[int] = []

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked.append(1)
        return ModelResponse(parts=[ToolCallPart(SUBMIT_DOCUMENT, {"title": "Q3", "content": drafts.pop(0)})])

    deps = TaskDeps(frame=FakeFrame("t1"), sources={"S1": "https://example.com/q3"}, lane_role=lane_role)
    runner = build("writer", deps, [fake_row(ModelClass.FAST_EXECUTOR)])
    with runner.agent.override(model=FunctionModel(model)):
        result = await runner.run("Write the Q3 note.")
    assert result.output.content == "Revenue grew [S1]." and len(asked) == 2


def test_the_prompt_is_within_the_step_budget() -> None:
    prompt = get("writer").prompt
    assert not over_budget(prompt), f"{len(prompt.split())} words"
    assert over_budget(prompt + " word" * STEP_PROMPT_WORDS)


def test_the_prompt_is_named_blocks_said_once() -> None:
    blocks = get("writer").blocks
    assert [b.id for b in blocks] == [
        "writer.role",
        "writer.how-you-work",
        "writer.how-to-write",
        "writer.figures",
        "writer.what-you-never-do",
    ]
    assert all(len(b.text) <= BLOCK_CAP for b in blocks)


def test_the_prompt_leaves_tool_law_to_the_tools() -> None:
    """How to read the web and when to save a routine live on those tools' descriptions, never here."""
    prompt = get("writer").prompt
    assert "start_index" not in prompt and "routine_create" not in prompt
    assert "web_fetch" in prompt and "find_in_page" in prompt
