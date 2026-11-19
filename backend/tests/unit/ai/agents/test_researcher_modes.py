# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The researcher plays the scatter lane's parts by its step's lane role: the map and each gather seat.

A lane step runs the researcher in its part, a step with no lane role is never a seat, and a seat's records
name their pages by source id: the system fills each URL from the read, sends an unread id back, and drops it
once no request is left for the fix.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import UsageLimits

from prismoid.ai.agents.base import definition_for, instructions_for
from prismoid.ai.capabilities.record_sources import RecordSources
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.research import WebRead
from prismoid.schemas import outputs
from prismoid.schemas.plan import LaneRole
from prismoid.schemas.swarm import RecordSet, ResearchRecord, ScatterResult

PAGE = "https://example.com/startups"


@pytest.mark.parametrize(
    ("stored", "lane_role", "typed"),
    [
        ("researcher", "scatter_map", ScatterResult),
        ("researcher", "gather", RecordSet),
    ],
)
def test_a_lane_step_runs_the_researcher_in_its_part(stored: str, lane_role: LaneRole, typed: type) -> None:
    seat = definition_for(stored, TaskDeps(lane_role=lane_role))
    assert str(seat.name) == "researcher" and outputs.get(seat.output_ref) is typed
    craft = {"scatter_map": "## Mapping the territory", "gather": "## Harvesting one territory"}
    read = {heading for heading in craft.values() if heading in instructions_for(seat)}
    assert read == {craft[lane_role]}, "a part reads its own craft and no other"


@pytest.mark.parametrize("lane_role", [None, "section", "synthesis"])
def test_a_step_outside_the_lane_parts_is_the_researcher_itself(lane_role: LaneRole | None) -> None:
    researcher = definition_for("researcher", TaskDeps(lane_role=lane_role))
    assert researcher.output_ref is None and researcher.can_ask_user and not researcher.quiet_stream
    assert "## Mapping the territory" not in instructions_for(researcher)


def _record(entity: str, source: str) -> ResearchRecord:
    return ResearchRecord(entity=entity, source=source, excerpts=["raised $12M in 2025"])


def _seat(answers: list[RecordSet]) -> Agent[TaskDeps, RecordSet]:
    """A seat that reads one page (given S1), then answers with each of `answers` in turn."""

    async def web_fetch(url: str) -> ToolReturn[str]:
        return ToolReturn(f"Source [S1]\n{url}", metadata=WebRead(page=url, source="S1", spent=1))

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("web_fetch", {"url": PAGE})])
        answer = answers.pop(0).model_dump(mode="json", exclude={"records": {"__all__": {"url"}}})
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])

    agent: Agent[TaskDeps, RecordSet] = Agent(
        FunctionModel(model), deps_type=TaskDeps, output_type=RecordSet, capabilities=[RecordSources()]
    )
    agent.tool_plain(web_fetch)
    return agent


async def test_a_record_takes_its_url_from_the_page_its_source_names() -> None:
    result = await _seat([RecordSet(records=[_record("Acme", "S1")])]).run("go", deps=TaskDeps())
    assert [(r.entity, r.url) for r in result.output.records] == [("Acme", PAGE)]


async def test_a_record_naming_an_unread_page_is_sent_back_and_fixed() -> None:
    wrong = RecordSet(records=[_record("Acme", "S1"), _record("Bolt", "S9")])
    fixed = RecordSet(records=[_record("Acme", "S1"), _record("Bolt", "S1")])
    result = await _seat([wrong, fixed]).run("go", deps=TaskDeps())
    assert [r.entity for r in result.output.records] == ["Acme", "Bolt"]
    told = [p for m in result.all_messages() if isinstance(m, ModelRequest) for p in m.parts]
    assert any("[S9]" in str(getattr(p, "content", "")) for p in told)


async def test_with_no_request_left_a_record_on_an_unread_page_is_dropped() -> None:
    wrong = RecordSet(records=[_record("Acme", "S1"), _record("Bolt", "S9")], exhausted=True)
    result = await _seat([wrong]).run("go", deps=TaskDeps(), usage_limits=UsageLimits(request_limit=2))
    assert [r.entity for r in result.output.records] == ["Acme"] and result.output.exhausted


async def test_a_page_an_earlier_step_read_may_back_a_record() -> None:
    earlier = "https://example.org/registry"
    answer = RecordSet(records=[_record("Acme", "S4")])
    result = await _seat([answer]).run("go", deps=TaskDeps(sources={"S4": earlier}))
    assert [r.url for r in result.output.records] == [earlier]
