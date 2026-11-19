# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A connector on a step's call log (ADR-020, ADR-089), against a real MCP server over an in-process ASGI
transport: a server that cannot be reached leaves its tools absent and the step runs on, a fully replayed
step opens no session, and a tool whose schema no inlining model can take is the only one left out."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr
from pydantic_ai import Agent, InlineDefsJsonSchemaTransformer
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_core import to_json
from starlette.types import Receive, Scope, Send

import prismoid.ai.tools.connectors as engine
from prismoid.ai.agents.run import AgentRunner, TapSlot
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.capabilities.tool_record import ToolRecorder
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.connectors import connector_toolsets
from prismoid.schemas.connectors import ConnectorSpec, ToolPolicy
from prismoid.schemas.ids import ConnectorId, StepId
from tests.support.rigs.durability_drill import MemoryLog
from tests.support.runners import PLAIN

pytestmark = [pytest.mark.integration]

BASE = (
    "http://localhost"  # the SDK's DNS-rebinding guard knows this Host (off here: the network is in process)
)


def _bomb(depth: int = 20) -> dict[str, Any]:
    """An input schema whose `$ref`s expand to 2**depth copies once inlined."""
    defs: dict[str, Any] = {f"d{depth}": {"type": "string"}}
    for level in range(depth):
        child = {"$ref": f"#/$defs/d{level + 1}"}
        defs[f"d{level}"] = {"type": "object", "properties": {"a": child, "b": child}}
    return {"type": "object", "properties": {"x": {"$ref": "#/$defs/d0"}}, "$defs": defs}


def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


def bomb(x: str) -> str:  # pragma: no cover - never mounted
    """A schema no inlining model can take."""
    return x


def _server() -> FastMCP:
    huge = Tool.from_function(bomb)
    huge.parameters = _bomb()
    return FastMCP(
        "example",
        instructions="Use echo wisely.",
        tools=[Tool.from_function(echo), huge],
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


@contextmanager
def _dialing(transport: Callable[[], httpx.AsyncBaseTransport]) -> Generator[None]:
    """Point the connector's one transport construction site at `transport`."""
    real = engine.StreamableHttpTransport

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **_: Any,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport(), base_url=BASE, headers=headers)

    def swapped(url: str, headers: dict[str, str] | None = None, **_: Any) -> Any:
        return real(url, headers=headers, httpx_client_factory=factory)

    engine.StreamableHttpTransport = swapped
    try:
        yield
    finally:
        engine.StreamableHttpTransport = real


def _counting(mcp: FastMCP, sessions: list[str]) -> Callable[[], httpx.AsyncBaseTransport]:
    """The live server; a POST without a session id is a session opening."""
    inner = mcp.streamable_http_app()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        opening = not any(key == b"mcp-session-id" for key, _ in scope.get("headers", []))
        if scope["type"] == "http" and scope["method"] == "POST" and opening:
            sessions.append("opened")
        await inner(scope, receive, send)

    return lambda: httpx.ASGITransport(app=app)


def _dead() -> httpx.AsyncBaseTransport:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(refuse)


SPEC = ConnectorSpec(
    id=ConnectorId(uuid.uuid4()),
    slug="gmail",
    name="Gmail",
    url=f"{BASE}/mcp",
    tool_names=("echo", "bomb"),
    authorization=SecretStr("Bearer run-token"),
    tool_policies={"echo": ToolPolicy.ALLOW, "bomb": ToolPolicy.ALLOW},
)


class Script:
    """The scripted provider: calls `gmail_echo` when shown it, then answers; counts what it serves."""

    def __init__(self) -> None:
        self.served = 0
        self.shown: list[str] = []

    def model(self, **kw: Any) -> FunctionModel:
        return FunctionModel(self._respond, stream_function=self._stream, **kw)

    def _parts(self, messages: list[ModelMessage], info: AgentInfo) -> list[TextPart | ToolCallPart]:
        self.served += 1
        self.shown = sorted(t.name for t in info.function_tools)
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if "gmail_echo" in self.shown and not returns:
            return [ToolCallPart("gmail_echo", {"text": "hello"})]
        return [TextPart(f"done after {self.served}: {'with' if returns else 'without'} gmail")]

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=self._parts(messages, info))

    async def _stream(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        for index, part in enumerate(self._parts(messages, info)):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {index: DeltaToolCall(name=part.tool_name, json_args=to_json(part.args).decode())}


class Journal:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

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
        self.rows.append((tool, ok, denied_reason))


def _runner(model: FunctionModel, journal: Journal, step: StepId) -> AgentRunner:
    """A step's runner the way `build()` makes one: durability and the recorder mounted."""
    live = TapSlot()
    agent = Agent(
        model,
        name="probe",
        deps_type=TaskDeps,
        toolsets=connector_toolsets((SPEC,), journal, approve=Approvals(tainted=tainted)),
        capabilities=[PrismoidDurability(event_stream_handler=live), ToolRecorder()],
    )
    return AgentRunner(agent, TaskDeps(step_id=step, journal_tool=journal), PLAIN, live=live)


@pytest.mark.parametrize("logged", [True, False], ids=["step", "unlogged"])
async def test_a_dead_server_leaves_its_tools_absent_and_the_step_runs_on(logged: bool) -> None:
    step, log, script, journal = StepId(uuid.uuid4()), MemoryLog(), Script(), Journal()
    scope = await CallLogScope.open(log, step, fresh=False) if logged else None
    with _dialing(_dead):
        result = await _runner(script.model(), journal, step).run("mail hello", log=scope)
    assert result.output == "done after 1: without gmail"
    assert script.shown == []
    assert [(tool, ok) for tool, ok, _ in journal.rows] == [("connector.gmail", False)]
    assert "connection refused" in journal.rows[0][2]


async def test_a_replayed_connector_step_opens_no_session() -> None:
    mcp, sessions = _server(), []
    step, log, script, journal = StepId(uuid.uuid4()), MemoryLog(), Script(), Journal()
    live = _counting(mcp, sessions)  # builds the app, and with it the session manager
    async with mcp.session_manager.run():
        with _dialing(live):
            first = await _runner(script.model(), journal, step).run(
                "mail hello", log=await CallLogScope.open(log, step, fresh=False)
            )
            assert first.output == "done after 2: with gmail" and sessions
            assert [(tool, ok) for tool, ok, _ in journal.rows] == [("gmail_echo", True)]
            sessions.clear()
            again = await _runner(script.model(), journal, step).run(
                "mail hello", log=await CallLogScope.open(log, step, fresh=False)
            )
    assert again.output == first.output
    assert sessions == [], "a full replay connects to nobody"
    assert script.served == 2, "and buys nothing"
    assert len(journal.rows) == 1, "nor journals a call twice"


async def test_a_schema_too_large_to_inline_drops_only_that_tool() -> None:
    mcp, sessions, script, journal = _server(), [], Script(), Journal()
    inlining = ModelProfile(json_schema_transformer=InlineDefsJsonSchemaTransformer)
    live = _counting(mcp, sessions)  # builds the app, and with it the session manager
    async with mcp.session_manager.run():
        with _dialing(live):
            result = await _runner(script.model(profile=inlining), journal, StepId(uuid.uuid4())).run(
                "mail hello"
            )
    assert result.output == "done after 2: with gmail"
    assert script.shown == ["gmail_echo"]
