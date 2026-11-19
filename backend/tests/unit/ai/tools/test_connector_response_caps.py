# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A connector server is a stranger's server: what it sends is bounded before it is buffered.

Anyone who signs up can point a connector at a server they run, and its MCP and OAuth answers are
read in the shared API and worker processes. So the pinned transport reads at most a response's
worth and a client's worth of bytes, asks for an unencoded body and refuses a coded one (httpx
would inflate it unbounded), the probe refuses an inventory past a tool count, and a binary tool
result past the vision ceiling becomes a note. No network: MockTransport plays the server.
"""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic_ai import BinaryContent

from prismoid.ai.tools.connectors import (
    BINARY_MAX_BYTES,
    PROBE_MAX_TOOLS,
    ProbeFailed,
    fenced_result,
    probe_server,
)
from prismoid.ai.tools.pin import PinnedTransport


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


class Wire(httpx.AsyncByteStream):
    """A body as the wire carries it (a `content=` response is read the moment it is built)."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def _client(handler: Callable[[httpx.Request], httpx.Response], **caps: int) -> httpx.AsyncClient:
    transport = PinnedTransport(
        allow_loopback=False, resolver=_public, inner=lambda: httpx.MockTransport(handler), **caps
    )
    return httpx.AsyncClient(transport=transport)


async def test_a_response_past_the_cap_is_cut_off_as_a_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Wire(b"x" * 600, b"x" * 600))

    async with _client(handler, max_response_bytes=1_000) as client:
        with pytest.raises(httpx.ReadError, match="more than"):
            await client.get("https://mcp.example.com/mcp")


async def test_a_declared_length_past_the_cap_is_refused_unread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "999999999"}, stream=Wire(b"{}"))

    async with _client(handler, max_response_bytes=1_000) as client:
        with pytest.raises(httpx.ReadError, match="999999999"):
            await client.get("https://mcp.example.com/.well-known/oauth-authorization-server")


async def test_a_paginated_answer_is_bounded_over_the_whole_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Wire(b"x" * 800))

    async with _client(handler, max_response_bytes=1_000, max_client_bytes=2_000) as client:
        await client.get("https://mcp.example.com/mcp?page=1")
        await client.get("https://mcp.example.com/mcp?page=2")
        with pytest.raises(httpx.ReadError, match="more than"):
            await client.get("https://mcp.example.com/mcp?page=3")


async def test_the_client_asks_for_an_unencoded_body_and_refuses_a_coded_one() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Wire(gzip.compress(b"{}")))

    async with _client(handler) as client:
        with pytest.raises(httpx.ReadError, match="gzip-encoded"):
            await client.get("https://mcp.example.com/mcp")
    assert seen[0].headers["accept-encoding"] == "identity"


def _crowded_server(tools: int) -> FastMCP:
    mcp = FastMCP(
        "crowded", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    for n in range(tools):
        mcp.add_tool(lambda: "ok", name=f"tool_{n}", description="one of many")
    return mcp


def _asgi_factory(mcp: FastMCP) -> Callable[..., httpx.AsyncClient]:
    app = mcp.streamable_http_app()

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")

    return factory


async def test_the_probe_refuses_an_inventory_past_the_tool_cap() -> None:
    mcp = _crowded_server(PROBE_MAX_TOOLS + 1)
    factory = _asgi_factory(mcp)
    async with mcp.session_manager.run():
        with pytest.raises(ProbeFailed, match=f"more than {PROBE_MAX_TOOLS} tools"):
            await probe_server("http://localhost/mcp", http_client_factory=factory)


def test_a_binary_result_past_the_vision_ceiling_becomes_a_note() -> None:
    huge = BinaryContent(data=b"\x89PNG" + b"\0" * BINARY_MAX_BYTES, media_type="image/png")
    small = BinaryContent(data=b"\x89PNG", media_type="image/png")

    alone = fenced_result(huge, "Drive")
    assert isinstance(alone, str) and "image/png" in alone and "left out" in alone

    mixed = fenced_result(["caption", huge, small], "Drive")
    assert isinstance(mixed, list)
    assert mixed[1:] == [small]
    note = mixed[0]
    assert isinstance(note, str) and "caption" in note and "left out" in note

    assert fenced_result(small, "Drive") is small
