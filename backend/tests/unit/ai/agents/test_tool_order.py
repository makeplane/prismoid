# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The model sees each connector's tools in name order and the connectors in slug order, never in the
order a server answered: a reshuffled inventory must not move a byte of the cached prefix. The
built-in tools keep their mount order."""

from __future__ import annotations

import json
import random
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import AbstractToolset

from prismoid.ai.agents import mounts
from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.connectors import ConnectorSpec
from prismoid.schemas.ids import ConnectorId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit
from tests.support.fakes.fake_frames import FakeFrame

AGENT = "researcher"
INVENTORY = {
    "gmail": ("send_email", "search_threads", "list_labels", "draft_reply"),
    "linear": ("update_issue", "create_issue", "list_teams"),
    "drive": ("read_file", "search", "copy"),
}


class _Seen(Exception):
    """The first model request was captured; the run stops there."""


def _connectors(rng: random.Random) -> tuple[ConnectorSpec, ...]:
    """The inventory in an arrival order a server might give: specs and tools shuffled."""
    specs = [
        ConnectorSpec(
            id=ConnectorId(uuid.uuid5(uuid.NAMESPACE_DNS, slug)),
            slug=slug,
            name=slug.title(),
            url=f"https://{slug}.example.com/mcp",
            tool_names=tuple(rng.sample(tools, len(tools))),
            instructions=f"How to use {slug}.",
        )
        for slug, tools in INVENTORY.items()
    ]
    return tuple(rng.sample(specs, len(specs)))


def _served(specs: tuple[ConnectorSpec, ...], *_: Any, **__: Any) -> list[AbstractToolset[Any]]:
    """A stand-in for the MCP servers: each lists its tools in the order its spec holds them."""

    async def call() -> str:
        return "ok"

    served: list[AbstractToolset[Any]] = []
    for spec in specs:
        toolset: FunctionToolset[Any] = FunctionToolset(instructions=spec.instructions, id=spec.slug)
        for tool in spec.tool_names:
            toolset.add_function(call, name=f"{spec.slug}_{tool}", description=f"{spec.name}: {tool}")
        served.append(toolset)
    return served


async def _first_request(connectors: tuple[ConnectorSpec, ...]) -> str:
    """What the model is sent on its first request: tool definitions and instructions, as bytes."""
    seen: list[str] = []

    def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [[t.name, t.description, t.parameters_json_schema] for t in info.function_tools]
        seen.append(json.dumps([tools, info.instructions]))
        raise _Seen

    deps = TaskDeps(frame=FakeFrame("t1"), connectors=connectors)
    catalog = [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=(get(AGENT).model_class,),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal(0)},
        )
    ]
    runner = build(AGENT, deps, catalog)
    with runner.agent.override(model=FunctionModel(capture)), pytest.raises(_Seen):
        await runner.run("go")
    return seen[0]


async def test_a_shuffled_inventory_sends_identical_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "connector_toolsets", _served)
    first = await _first_request(_connectors(random.Random(1)))
    for seed in range(2, 6):
        assert await _first_request(_connectors(random.Random(seed))) == first

    tools, instructions = json.loads(first)
    names = [name for name, *_ in tools]
    served = [name for name in names if name.partition("_")[0] in INVENTORY]
    assert served == sorted(served) and len(served) == sum(len(t) for t in INVENTORY.values())

    alone = [name for name, *_ in json.loads(await _first_request(()))[0]]
    assert [name for name in names if name not in served] == alone, "built-in order is untouched"
    assert alone[:3] == ["read_file", "ask_user", "request_connector"], "built-ins keep their mount order"
    assert instructions.index("How to use drive") < instructions.index("How to use gmail")
    assert instructions.index("How to use gmail") < instructions.index("How to use linear")
