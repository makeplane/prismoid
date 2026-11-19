# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connectors, end to end against a REAL MCP server (ADR-020) - no network, no mocks of ours.

The server is the SDK's own `mcp.server.fastmcp.FastMCP`, served through its real streamable-
HTTP ASGI app via `httpx.ASGITransport` - so the wire protocol, the initialize handshake, the
Authorization header, and the tool round trip are all the genuine article. The model is a
scripted `FunctionModel`: it lists the tools it was shown (pinning the filter + prefix), calls
one, and answers from the result. What this file proves is the whole Phase-1 claim: a
connector's tools reach an agent, calls land on the journal, and the server's instructions
reach the prompt.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from starlette.types import Receive, Scope, Send

import prismoid.ai.tools.connectors as engine
from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.capabilities.tool_record import ToolRecorder
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.connectors import connector_toolsets, probe_server
from prismoid.schemas.connectors import ConnectorSpec, ToolPolicy
from prismoid.schemas.ids import ConnectorId

pytestmark = [pytest.mark.integration]

# localhost, not an invented host: the SDK's streamable transport ships DNS-rebinding
# protection and answers 421 to any Host it was not told about.
BASE = "http://localhost"


@pytest.fixture
def server() -> tuple[FastMCP, dict[str, str]]:
    from mcp.server.transport_security import TransportSecuritySettings

    # The ASGI client speaks Host: localhost (no port); the default rebinding guard expects the
    # server's own host:port and answers 421. The guard is off HERE because the "network" is an
    # in-process ASGI transport - production connectors talk to servers that run their own.
    mcp = FastMCP(
        "example",
        instructions="Use echo wisely.",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    from mcp.types import ToolAnnotations

    @mcp.tool(description="Echo the text back.", annotations=ToolAnnotations(readOnlyHint=True))
    def echo(text: str) -> str:
        return f"echo: {text}"

    @mcp.tool(description="Not in the inventory.")
    def hidden(text: str) -> str:  # pragma: no cover - the filter keeps the model from seeing it
        return "should not be reachable"

    @mcp.tool(
        description="Send mail - the side effect.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    def send(to: str, body: str) -> str:
        return f"sent to {to}"

    _ = (echo, hidden, send)  # the decorator registered them; the tuple is for pyright's eyes
    return mcp, {}


def _factory(mcp: FastMCP, seen: dict[str, str]) -> Callable[..., httpx.AsyncClient]:
    inner_app = mcp.streamable_http_app()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            if "authorization" in headers:
                seen["authorization"] = headers["authorization"]
        await inner_app(scope, receive, send)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE,
            headers=kwargs.get("headers"),
            auth=kwargs.get("auth"),
        )

    return factory


async def test_probe_speaks_the_real_protocol(server: tuple[FastMCP, dict[str, str]]):
    mcp, seen = server
    factory = _factory(mcp, seen)  # builds the ASGI app - the session manager exists only after
    async with mcp.session_manager.run():
        result = await probe_server(
            f"{BASE}/mcp",
            authorization="Bearer probe-token",
            http_client_factory=factory,
        )
    assert [t.name for t in result.tools] == ["echo", "hidden", "send"]
    assert result.tools[0].description == "Echo the text back."
    assert result.instructions == "Use echo wisely."
    assert result.server_info.get("name") == "example"
    assert seen["authorization"] == "Bearer probe-token", "the credential rode the real wire"
    # ToolAnnotations captured at the one MCP seam: the policy dial's raw material.
    by_name = {t.name: t for t in result.tools}
    assert by_name["echo"].read_only is True
    assert by_name["send"].read_only is False and by_name["send"].destructive is True
    assert by_name["hidden"].read_only is None, "no annotations = unknown, honestly"


async def test_agent_calls_a_connector_tool_end_to_end(server: tuple[FastMCP, dict[str, str]]):
    mcp, seen = server
    spec = ConnectorSpec(
        id=ConnectorId(uuid.uuid4()),
        slug="gmail",
        name="Gmail",
        url=f"{BASE}/mcp",
        tool_names=("echo",),  # `hidden` deliberately NOT inventoried
        instructions="Use echo wisely.",
        authorization=SecretStr("Bearer run-token"),
        tool_policies={"echo": ToolPolicy.ALLOW},  # this test pins the RUN path (ask has its own)
    )

    journal: list[tuple[str, bool, str]] = []

    async def record(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        journal.append((tool, ok, summary))

    observed: dict[str, Any] = {}

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        observed["tools"] = sorted(t.name for t in info.function_tools)
        observed.setdefault("instructions", getattr(messages[0], "instructions", None))
        returns = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name="gmail_echo", args={"text": "hello"})])
        observed["tool_return"] = str(returns[0].content)
        return ModelResponse(parts=[TextPart(f"The server said: {returns[0].content}")])

    # The REAL toolset chain, pointed at the ASGI server by injecting the client factory into
    # the one transport construction site.
    real_transport = engine.StreamableHttpTransport
    factory = _factory(mcp, seen)

    def asgi_transport(url: str, headers: dict[str, str] | None = None, **_: Any) -> Any:
        return real_transport(url, headers=headers, httpx_client_factory=factory)

    async with mcp.session_manager.run():
        try:
            # Before connector_toolsets: the transport is constructed at toolset build time.
            engine.StreamableHttpTransport = asgi_transport
            toolsets = connector_toolsets((spec,), record, approve=Approvals(tainted=tainted))
            agent = Agent(
                FunctionModel(scripted), deps_type=TaskDeps, toolsets=toolsets, capabilities=[ToolRecorder()]
            )
            result = await agent.run("say hello through gmail", deps=TaskDeps(journal_tool=record))
        finally:
            engine.StreamableHttpTransport = real_transport

    assert "\necho: hello\n" in result.output and result.output.startswith("The server said: ")
    assert observed["tools"] == ["gmail_echo"], "prefixed, and `hidden` filtered out of sight"
    assert "\necho: hello\n" in observed["tool_return"], "the result reaches the model fenced"
    assert observed["instructions"] and "Use echo wisely." in str(observed["instructions"]), (
        "include_instructions carried the server's guidance into the prompt"
    )
    assert journal == [("gmail_echo", True, "args: text")], (
        "one durable-journal intent, prefixed name, arg keys only"
    )
    assert seen["authorization"] == "Bearer run-token"


async def test_an_ask_tool_pauses_the_run_and_resumes_on_the_verdict(
    server: tuple[FastMCP, dict[str, str]],
):
    """The whole library walk, against the real protocol: an `ask` tool ends the run
    in `DeferredToolRequests.approvals` with the hook's metadata (nothing executed, nothing
    journaled); resuming with `ToolApproved` executes and journals it; the denied twin ends
    with the tool never reaching the server."""
    from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied

    mcp, seen = server
    spec = ConnectorSpec(
        id=ConnectorId(uuid.uuid4()),
        slug="gmail",
        name="Gmail",
        url=f"{BASE}/mcp",
        tool_names=("echo", "send"),
        authorization=SecretStr("Bearer run-token"),
        tool_policies={"echo": ToolPolicy.ALLOW, "send": ToolPolicy.ASK},
    )

    journal: list[tuple[str, bool]] = []

    async def record(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        journal.append((tool, ok))

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="gmail_send", args={"to": "a@b.c", "body": "hi"})]
            )
        return ModelResponse(parts=[TextPart(f"done: {returns[0].content}")])

    real_transport = engine.StreamableHttpTransport
    factory = _factory(mcp, seen)

    def asgi_transport(url: str, headers: dict[str, str] | None = None, **_: Any) -> Any:
        return real_transport(url, headers=headers, httpx_client_factory=factory)

    async with mcp.session_manager.run():
        try:
            engine.StreamableHttpTransport = asgi_transport
            toolsets = connector_toolsets((spec,), record, approve=Approvals(tainted=tainted))
            agent = Agent(
                FunctionModel(scripted),
                deps_type=TaskDeps,
                output_type=[str, DeferredToolRequests],
                toolsets=toolsets,
                capabilities=[ToolRecorder()],
            )
            deps = TaskDeps(journal_tool=record)
            first = await agent.run("send the mail", deps=deps)
            assert isinstance(first.output, DeferredToolRequests)
            assert [p.tool_name for p in first.output.approvals] == ["gmail_send"]
            call_id = first.output.approvals[0].tool_call_id
            meta = first.output.metadata[call_id]
            assert meta["tool"] == "gmail_send" and "to: a@b.c" in meta["preview"]
            assert journal == [], "the pause journaled nothing - the gate pair is the record"

            approved = await agent.run(
                deps=deps,
                message_history=first.all_messages(),
                deferred_tool_results=DeferredToolResults(approvals={call_id: ToolApproved()}),
            )
            assert isinstance(approved.output, str) and "\nsent to a@b.c\n" in approved.output
            assert journal == [("gmail_send", True)], "the approved call ran and journaled"

            # The denied twin: fresh run, same pause, ToolDenied - the server is never reached.
            journal.clear()
            second = await agent.run("send the mail", deps=deps)
            assert isinstance(second.output, DeferredToolRequests)
            denied_id = second.output.approvals[0].tool_call_id
            denied = await agent.run(
                deps=deps,
                message_history=second.all_messages(),
                deferred_tool_results=DeferredToolResults(
                    approvals={denied_id: ToolDenied("the user declined this action")}
                ),
            )
            assert isinstance(denied.output, str) and "sent to" not in denied.output
            assert journal == [], "a denied call never reached the server, never journaled"
        finally:
            engine.StreamableHttpTransport = real_transport
