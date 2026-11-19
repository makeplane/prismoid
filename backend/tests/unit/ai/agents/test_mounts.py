# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The mounts follow the grants: a family mounts only on a definition that holds one of its tools, a
built-in's hands are its `grants.GRANTS` row, and a task's connectors reach only the agents
`grants.CONNECTORS` names. What a whole first request carries, per agent and deployment, is the
rendered-reference law's (`tests/laws/test_law_rendered_tool_references.py`)."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from prismoid.ai.agents import grants, mounts
from prismoid.ai.agents.base import AgentDef, definition_for, get, register, roster
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.meter import MeterContext
from prismoid.ai.skills.deliverables import document_formats, kind_agent, kinds
from prismoid.ai.tools import catalog
from prismoid.schemas.connectors import ConnectorSpec
from prismoid.schemas.ids import AgentName, ConnectorId
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.plan import LaneRole

BY_JOURNAL = {tool.journal: tool.name for tool in catalog.TOOLS}
GMAIL = ConnectorSpec(
    id=ConnectorId(uuid.uuid4()), slug="gmail", name="Gmail", url="https://mcp.example.test"
)


def _mount(definition: AgentDef, deps: TaskDeps | None = None) -> mounts.Mount:
    return mounts.Mount(
        definition=definition,
        deps=deps or TaskDeps(),
        catalog=[],
        meter=MeterContext(),
        served=None,
        order=None,
        quality=None,
    )


def _family(adds: object) -> mounts.Family:
    return next(f for f in mounts.MOUNTS if f.adds is adds)


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_a_built_ins_hands_are_its_grants_row(agent: AgentDef) -> None:
    held = grants.held(str(agent.name), grants.DEPLOYED)
    assert {BY_JOURNAL[t] for t in agent.tools} == held
    assert agent.connectors == grants.CONNECTORS.get(str(agent.name), "none")


def test_a_built_in_that_declares_its_own_tools_is_refused() -> None:
    rogue = AgentDef(
        name=AgentName("rogue"), model_class=ModelClass.FAST_EXECUTOR, description="x", tools=("frame.shell",)
    )
    with pytest.raises(ValueError, match=r"grants\.py"):
        register(rogue)


def test_every_family_serves_catalog_tools() -> None:
    names = {tool.name for tool in catalog.TOOLS}
    assert all(set(f.serves) <= names for f in mounts.MOUNTS)


def test_a_family_mounts_only_where_its_tool_is_held() -> None:
    fetch = _family(mounts.web_fetch)
    holder = get("writer")
    assert fetch(_mount(holder)).toolsets, "the writer holds web_fetch"
    assert fetch(_mount(dataclasses.replace(holder, tools=("frame.read_file",)))) is mounts.NOTHING
    assert _family(mounts.steering).serves == (), "a run capability serves no tool: every build asks it"


@pytest.mark.parametrize("agent", ["maker", "operator"])
def test_the_maker_and_the_operator_read_the_web(agent: str) -> None:
    assert {"web_fetch", "web_search"} <= grants.held(agent)


def test_the_judge_can_open_a_source_and_nothing_else() -> None:
    assert grants.held("judge") == {"web_fetch"}


def test_every_agent_a_dial_seats_reads_the_web_itself() -> None:
    plannable = roster(plannable=True)
    for kind in kinds():
        for fmt in (None, *document_formats()):
            agent = kind_agent(kind, plannable, fmt)
            assert {"web_fetch", "web_search"} <= grants.held(agent), agent


@pytest.mark.parametrize(
    ("agent", "lane_role"),
    [
        ("researcher", "scatter_map"),
        ("researcher", "gather"),
        ("writer", "fleet_table"),
        ("writer", "outline"),
        ("writer", "section"),
    ],
)
def test_a_lane_seat_holds_no_connector_and_no_ask(agent: str, lane_role: LaneRole) -> None:
    deps = TaskDeps(connectors=(GMAIL,), lane_role=lane_role)
    seat = definition_for(agent, deps)
    assert seat.connectors == "none" and not seat.can_ask_user
    assert {BY_JOURNAL[t] for t in seat.tools} <= grants.SEAT_TOOLS
    assert mounts.connectors(_mount(seat, deps)) is mounts.NOTHING


def test_a_granted_agent_holds_the_tasks_connectors() -> None:
    deps = TaskDeps(connectors=(GMAIL,))
    assert mounts.connectors(_mount(get("operator"), deps)).toolsets
    assert get("lead").connectors == "reads"
