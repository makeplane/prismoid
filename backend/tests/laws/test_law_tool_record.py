# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every tool call is recorded once: each toolset of every built agent passes `ToolRecorder`, which
writes exactly one `tool.called` per call, a connector's (MCP) call included.

The population is every registered agent, built by the real `build()` with one planted family
mounted beside the real ones (a function tool and an in-process MCP connector), and its first request
scripted to call the planted tools and, when the agent is sent it, a frame tool.
The law reads the journal: one row per call id, never zero and never two.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

import prismoid.ai.agents.base as agents_base
from prismoid.ai.agents.base import build, roster
from prismoid.ai.agents.mounts import MOUNTS, Mount, Mounted
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit
from tests.support.fakes.fake_frames import FakeFrame


class _Done(Exception):
    """The calls ran; the run stops at its next request."""


def _planted(_: Mount) -> Mounted:
    """One function tool and one connector, mounted the way every family is."""
    looks: FunctionToolset[TaskDeps] = FunctionToolset(id="planted")

    def planted_look(query: str) -> str:
        """Look one thing up."""
        return f"saw {query}"

    looks.add_function(planted_look)
    server = FastMCP("crm")

    def crm_note(text: str) -> str:
        """Write a note on the record (a write)."""
        return f"noted {text}"

    server.tool(name="crm_note")(crm_note)
    return Mounted(toolsets=(looks, MCPToolset(server, id="crm")))


def _row(model_class: ModelClass) -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model_id=f"m-{model_class.value}",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )


class _Journal:
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.ids.append(tool_call_id)


async def _miscounted(name: str) -> dict[str, int]:
    """The calls of one built agent whose `tool.called` count is not exactly one."""
    calls: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if calls:
            raise _Done
        sent = {tool.name for tool in info.function_tools}
        wanted = {
            "planted_look": {"query": "q"},
            "crm_note": {"text": "t"},
            "read_file": {"path": "/work/a.md"},
        }
        parts = [ToolCallPart(tool, args, f"call-{tool}") for tool, args in wanted.items() if tool in sent]
        calls.extend(part.tool_call_id for part in parts)
        return ModelResponse(parts=parts)

    journal = _Journal()
    frame = FakeFrame("t1")
    await frame.write_file("/work/a.md", "notes")
    deps = TaskDeps(frame=frame, journal_tool=journal)
    runner = build(name, deps, [_row(agents_base.get(name).model_class)])
    with runner.agent.override(model=FunctionModel(script)), pytest.raises(_Done):
        await runner.run("go")
    assert {"call-planted_look", "call-crm_note"} <= set(calls), f"{name} was not sent the planted tools"
    counted = Counter(journal.ids)
    return {call: counted[call] for call in calls if counted[call] != 1}


async def _faults() -> dict[str, dict[str, int]]:
    faults = {str(d.name): await _miscounted(str(d.name)) for d in roster()}
    return {name: miscounted for name, miscounted in faults.items() if miscounted}


async def test_every_call_of_every_built_agent_is_recorded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_base, "MOUNTS", (*MOUNTS, _planted))
    assert len(roster()) > 10, "the population lost the roster"
    assert not await _faults(), "every tool call passes ToolRecorder (ai/agents/base.build)"


class _Unrecorded(AbstractCapability[TaskDeps]):
    """The record's place with nothing in it."""


async def test_the_law_bites_on_an_unwrapped_toolset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_base, "MOUNTS", (*MOUNTS, _planted))
    monkeypatch.setattr(agents_base, "ToolRecorder", _Unrecorded)
    unrecorded = {"call-planted_look": 0, "call-crm_note": 0, "call-read_file": 0}
    assert await _miscounted("writer") == unrecorded, "no tool journals a call the record did not see"
