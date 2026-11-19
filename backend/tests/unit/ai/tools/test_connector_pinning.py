# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connectors door dials only what it vetted.

A connector URL is user-supplied, and its OAuth metadata is server-supplied: either can name an
address inside the network. Outside local mode every request an MCP or OAuth client makes is
resolved once, every answer is vetted, and the connection goes to the vetted address with the
name on Host and SNI - so a second resolution cannot swap it, and a followed redirect is vetted
again. No network: a table plays DNS and MockTransport plays the server.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools import pin
from prismoid.ai.tools.connectors import ProbeFailed, connector_toolsets, probe_server
from prismoid.ai.tools.pin import PinnedTransport
from prismoid.conf import ConnectorsSettings
from prismoid.schemas.connectors import ConnectorSpec, ToolPolicy
from prismoid.schemas.ids import ConnectorId
from tests.support.settings_env import no_developer_settings


@dataclass
class Dns:
    """DNS as a table; each name's answers are served in turn, the last one repeating."""

    table: dict[str, list[list[str]]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def __call__(self, host: str, port: int) -> list[str]:
        self.asked.append(host)
        answers = self.table.get(host, [["93.184.216.34"]])
        return answers[0] if len(answers) == 1 else answers.pop(0)


def _pinned(dns: Dns, handler: Any, *, allow_loopback: bool = False) -> PinnedTransport:
    return PinnedTransport(
        allow_loopback=allow_loopback, resolver=dns, inner=lambda: httpx.MockTransport(handler)
    )


async def test_the_request_goes_to_the_vetted_address_with_the_name_on_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=_pinned(Dns(), handler)) as client:
        await client.get("https://mcp.example.com/mcp")
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "mcp.example.com"
    assert seen[0].extensions.get("sni_hostname") == "mcp.example.com"


async def test_an_unreachable_answer_falls_through_to_the_next_vetted_one() -> None:
    # macOS answers `localhost` with ::1 first; a server bound to 127.0.0.1 alone is still reached.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "::1":
            raise httpx.ConnectError("All connection attempts failed", request=request)
        return httpx.Response(200)

    dns = Dns({"localhost": [["::1", "127.0.0.1"]]})
    async with httpx.AsyncClient(transport=_pinned(dns, handler, allow_loopback=True)) as client:
        response = await client.get("http://localhost:8472/mcp")
    assert response.status_code == 200
    assert seen == ["::1", "127.0.0.1"]
    assert response.request.url.host == "localhost"


async def test_every_answer_is_still_vetted_before_any_is_dialed() -> None:
    dialed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dialed.append(request.url.host)
        raise httpx.ConnectError("refused", request=request)

    dns = Dns({"mcp.example.com": [["93.184.216.34", "10.0.0.5"]]})
    async with httpx.AsyncClient(transport=_pinned(dns, handler)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://mcp.example.com/mcp")
    assert dialed == [], "one internal answer refuses the name before anything is dialed"


@pytest.mark.parametrize(
    "answer",
    ["10.0.0.5", "127.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fd00::1"],
)
async def test_a_name_that_resolves_inside_the_network_is_never_dialed(answer: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an internal address must never be dialed")

    dns = Dns(table={"internal.example": [[answer]]})
    async with httpx.AsyncClient(transport=_pinned(dns, handler)) as client:
        with pytest.raises(httpx.ConnectError, match="not an address this server will dial") as caught:
            await client.get("https://internal.example/mcp")
    assert answer not in str(caught.value), "the resolved address is reconnaissance, not a message"


async def test_a_redirect_is_vetted_again_and_a_rebinding_answer_refused() -> None:
    """The first answer is public, the second private: the hop is resolved again and refused."""
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://rebind.example/mcp/"})

    dns = Dns(table={"rebind.example": [["93.184.216.34"], ["10.0.0.9"]]})
    async with httpx.AsyncClient(transport=_pinned(dns, handler), follow_redirects=True) as client:
        with pytest.raises(httpx.ConnectError, match="inside the network"):
            await client.get("https://rebind.example/mcp")
    assert len(hits) == 1 and dns.asked == ["rebind.example", "rebind.example"]


@pytest.mark.parametrize("location", ["/mcp/", "https://mcp.example.com/mcp/"])
async def test_a_same_origin_redirect_is_followed_by_name_through_the_mcp_client(location: str) -> None:
    """A 307 /mcp -> /mcp/ is the common trailing-slash normalisation. The MCP SDK follows it
    only within the origin of `response.request`, so the transport must leave the caller's
    request naming the host: the hop is resolved again by name, never dialed at the address."""
    from mcp.shared._httpx_utils import request_within_origin

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/mcp":
            return httpx.Response(307, headers={"location": location})
        return httpx.Response(200)

    dns = Dns()
    async with httpx.AsyncClient(transport=_pinned(dns, handler)) as client:
        response = await request_within_origin(client, "POST", "https://mcp.example.com/mcp")
    assert response.status_code == 200
    assert response.request.url.host == "mcp.example.com"
    assert dns.asked == ["mcp.example.com", "mcp.example.com"]
    assert [r.url.host for r in seen] == ["93.184.216.34", "93.184.216.34"]
    assert all(r.extensions.get("sni_hostname") == "mcp.example.com" for r in seen)
    assert all(r.headers["host"] == "mcp.example.com" for r in seen)


async def test_the_callers_request_is_left_naming_the_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    request = httpx.Request("GET", "https://mcp.example.com/mcp")
    response = await _pinned(Dns(), handler).handle_async_request(request)
    assert response.status_code == 200
    assert request.url.host == "mcp.example.com" and "sni_hostname" not in request.extensions


async def test_plain_http_only_reaches_a_loopback_name_where_loopback_is_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    dns = Dns(table={"localhost": [["127.0.0.1"]]})
    async with httpx.AsyncClient(transport=_pinned(dns, handler)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://mcp.example.com/mcp")
        with pytest.raises(httpx.ConnectError):
            await client.get("http://localhost:9004/mcp")
    async with httpx.AsyncClient(transport=_pinned(dns, handler, allow_loopback=True)) as client:
        assert (await client.get("http://localhost:9004/mcp")).status_code == 200


async def test_the_probe_refuses_a_server_url_that_resolves_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    dns = Dns(table={"split-horizon.example": [["10.1.2.3"]]})
    monkeypatch.setattr(pin, "default_resolver", dns)
    with pytest.raises(ProbeFailed, match="not an address this server will dial"):
        await probe_server("https://split-horizon.example/mcp", authorization="Bearer x", timeout_s=5)
    assert dns.asked == ["split-horizon.example"]


async def test_a_mounted_toolset_refuses_a_server_that_resolves_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = Dns(table={"split-horizon.example": [["10.1.2.3"]]})
    monkeypatch.setattr(pin, "default_resolver", dns)
    spec = ConnectorSpec(
        id=ConnectorId(uuid.uuid4()),
        slug="inside",
        name="Inside",
        url="https://split-horizon.example/mcp",
        tool_names=("echo",),
        tool_policies={"echo": ToolPolicy.ALLOW},
        authorization=SecretStr("Bearer sekrit"),
    )
    rows: list[tuple[str, bool, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, ok, denied_reason))

    (toolset,) = connector_toolsets((spec,), journal, approve=Approvals(tainted=tainted))
    ctx = RunContext(deps=TaskDeps(), model=TestModel(), usage=RunUsage())
    async with toolset:
        assert await toolset.get_tools(ctx) == {}, "the refused server's tools are absent"
    assert [(tool, ok) for tool, ok, _ in rows] == [("connector.inside", False)]
    assert "not an address this server will dial" in rows[0][2]
    assert set(dns.asked) == {"split-horizon.example"}


def test_local_mode_reaches_the_persons_own_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prismoid up` (PRISMOID_ENV=local) allows a loopback connector; everywhere else it is refused
    unless a developer opts in."""
    no_developer_settings(monkeypatch, tmp_path)  # a developer's opt-in is not the default
    from prismoid.local.postgres import PgUrls
    from prismoid.runner.local_cli import child_env

    urls = PgUrls(async_url="postgresql+asyncpg://a@/prismoid", sync_url="postgresql://a@/prismoid")
    env = child_env(tmp_path, urls, tmp_path / "web", 9123)
    assert env["PRISMOID_ENV"] == "local" and env["PRISMOID_CONNECTORS_ALLOW_INSECURE_LOCALHOST"] == "true"
    assert ConnectorsSettings().allow_insecure_localhost is False
