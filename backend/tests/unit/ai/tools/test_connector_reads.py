# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A connector read is an answer, not a plan: the Lead holds the tools a server labels read-only
and never one that acts, and a large inventory loads on demand through tool search, in a stable order.

The server is the MCP SDK's own `FastMCP` behind its streamable-HTTP ASGI app (no network); the agent
is the real `build()`, its model a scripted `FunctionModel` that records what each request carries.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import prismoid.ai.tools.connectors as engine
from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.connectors import ConnectorSpec, ToolPolicy
from prismoid.schemas.ids import ConnectorId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit

BASE = "http://localhost"
READ = ToolAnnotations(readOnlyHint=True)
LABELS: dict[str, ToolAnnotations | None] = {
    "inbox": READ,
    "send": ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    "archive": None,  # no label: it may act
    "wipe": ToolAnnotations(readOnlyHint=True, destructiveHint=True),  # a contradiction fails closed
}
SHELVES = [f"shelf_{n:02d}" for n in range(40)]


def _server(tools: dict[str, ToolAnnotations | None]) -> FastMCP:
    mcp = FastMCP(
        "mail",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def answer(text: str = "") -> str:
        return f"ok {text}"

    for name, labels in tools.items():
        mcp.tool(name=name, description=f"The {name.replace('_', ' ')} of the mailbox.", annotations=labels)(
            answer
        )
    return mcp


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[FastMCP], None]:
    """Point every connector transport at `mcp`, in process."""
    real: Any = engine.StreamableHttpTransport

    def point(mcp: FastMCP) -> None:
        app = mcp.streamable_http_app()

        def factory(**kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE, headers=kwargs.get("headers")
            )

        def transport(url: str, headers: dict[str, str] | None = None, **_: Any) -> Any:
            return real(url, headers=headers, httpx_client_factory=factory)

        monkeypatch.setattr(engine, "StreamableHttpTransport", transport)

    return point


def _spec(names: list[str]) -> ConnectorSpec:
    return ConnectorSpec(
        id=ConnectorId(uuid.uuid5(uuid.NAMESPACE_DNS, "mail")),
        slug="mail",
        name="Mail",
        url=f"{BASE}/mcp",
        tool_names=tuple(names),
        tool_policies=dict.fromkeys(names, ToolPolicy.ALLOW),
    )


def _row(agent: str) -> ModelSpec:
    model_class = get(agent).model_class
    return ModelSpec(
        provider="fake",
        model_id=f"m-{model_class.value}",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )


async def _quiet(*_: Any, **__: Any) -> None:
    return None


async def _requests(
    mcp: FastMCP, agent: str, spec: ConnectorSpec, script: Iterator[ModelResponse]
) -> list[list[str]]:
    """The connector-visible tool names of each request `agent` makes while `script` answers it."""
    seen: list[list[str]] = []

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append([t.name for t in info.function_tools if t.name.startswith("mail_")])
        return next(script)

    runner = build(agent, TaskDeps(connectors=(spec,), journal_tool=_quiet), [_row(agent)])
    async with mcp.session_manager.run():
        with runner.agent.override(model=FunctionModel(model)):
            await runner.run("what did my last emails say?")
    return seen


def _done() -> Iterator[ModelResponse]:
    yield ModelResponse(parts=[TextPart("done")])


async def test_the_answer_holds_reads_and_never_a_write(serve: Callable[[FastMCP], None]) -> None:
    mcp = _server(LABELS)
    serve(mcp)
    (answer,) = await _requests(mcp, "lead", _spec(list(LABELS)), _done())
    assert answer == ["mail_inbox"]


async def test_a_step_holds_the_whole_inventory(serve: Callable[[FastMCP], None]) -> None:
    mcp = _server(LABELS)
    serve(mcp)
    (step,) = await _requests(mcp, "researcher", _spec(list(LABELS)), _done())
    assert sorted(step) == ["mail_archive", "mail_inbox", "mail_send", "mail_wipe"]


def _search_then_answer() -> Iterator[ModelResponse]:
    yield ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["shelf"]})])
    yield ModelResponse(parts=[TextPart("done")])


async def _forty(serve: Callable[[FastMCP], None], seed: int) -> list[list[str]]:
    """What each request of the Lead shows, over 40 read tools a server listed in a shuffled order."""
    names = random.Random(seed).sample(SHELVES, len(SHELVES))
    mcp = _server(dict.fromkeys(names, READ))
    serve(mcp)
    return await _requests(mcp, "lead", _spec(names), _search_then_answer())


async def test_forty_tools_load_on_demand_in_a_stable_order(serve: Callable[[FastMCP], None]) -> None:
    first = await _forty(serve, seed=1)
    assert first == await _forty(serve, seed=2), "a server's listing order never reaches the model"
    before, after = first
    assert before == [], "the inventory waits behind tool search"
    assert after == [f"mail_{name}" for name in SHELVES[:10]], "a search loads its matches, in name order"
