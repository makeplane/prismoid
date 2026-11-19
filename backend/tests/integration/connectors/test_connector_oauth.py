# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The OAuth choreography (ADR-020), end to end against a canned authorization server.

`operations/connector_oauth.http_client_factory` is the seam: every auth leg (discovery, DCR, the
code exchange, refresh) hits an `httpx.MockTransport` that records what we sent - so the tests
assert the *grammar* (PKCE round trip, client auth methods, resource indicator, rotation), not
just the outcome. The MCP probe is faked at the `probe_server` symbol here; the real protocol
round trip has its own in-process-server test beside the engine mount.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.connector import Connector
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, ValidationFailed
from prismoid.operations import cipher, connector_oauth
from prismoid.operations import connectors as ops
from prismoid.schemas.connectors import ConnectorStatus, ConnectorToolInfo, ProbeResult, s256_challenge
from tests.support.cast import member

pytestmark = [pytest.mark.integration]

AS = "https://auth.example.test"
MCP = "https://mcp.example.test/mcp"
BROWSER = "cb_" + "b" * 43  # the starting browser's binding cookie


@pytest.fixture(autouse=True)
def own_cipher_key(monkeypatch: pytest.MonkeyPatch):
    """A per-run cipher key so no test ever writes a key file into the repo."""
    monkeypatch.setattr(settings.connectors, "encryption_key", SecretStr(Fernet.generate_key().decode()))
    cipher.reset()
    yield
    cipher.reset()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


class CannedAS:
    """The authorization server, as a MockTransport handler that records every request."""

    def __init__(self, *, with_registration: bool = True, with_metadata: bool = True) -> None:
        self.with_registration = with_registration
        self.with_metadata = with_metadata
        self.requests: list[httpx.Request] = []
        self.token_answer: dict[str, Any] = {
            "access_token": "tok_access_1",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": "tok_refresh_1",
            "scope": "read write",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200,
                json={"resource": MCP, "authorization_servers": [AS], "scopes_supported": ["read", "write"]},
            )
        if path.startswith("/.well-known/oauth-authorization-server"):
            if not self.with_metadata:
                return httpx.Response(404)
            meta: dict[str, Any] = {
                "issuer": AS,
                "authorization_endpoint": f"{AS}/authorize",
                "token_endpoint": f"{AS}/token",
                "code_challenge_methods_supported": ["S256"],
            }
            if self.with_registration:
                meta["registration_endpoint"] = f"{AS}/register"
            return httpx.Response(200, json=meta)
        if path.startswith("/.well-known/openid-configuration"):
            return httpx.Response(404)
        if path == "/register":
            body = json.loads(request.content)
            return httpx.Response(
                201, json={**body, "client_id": "dcr-client", "client_secret": "dcr-secret"}
            )
        if path == "/token":
            return httpx.Response(200, json=self.token_answer)
        return httpx.Response(404)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(
            connector_oauth,
            "http_client_factory",
            lambda: httpx.AsyncClient(transport=transport, follow_redirects=False),
        )

    def token_form(self) -> dict[str, str]:
        request = next(r for r in reversed(self.requests) if r.url.path == "/token")
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def token_headers(self) -> httpx.Headers:
        return next(r for r in reversed(self.requests) if r.url.path == "/token").headers


def _needs_auth_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.tools.connectors import ProbeAuthRequired

    async def probe(url: str, **kw: Any) -> ProbeResult:
        raise ProbeAuthRequired(
            f'Bearer resource_metadata="{urlsplit(MCP).scheme}://{urlsplit(MCP).netloc}'
            '/.well-known/oauth-protected-resource/mcp"'
        )

    monkeypatch.setattr(ops, "probe_server", probe)


def _live_probe(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any] | None = None) -> None:
    async def probe(url: str, *, authorization: str | None = None, **kw: Any) -> ProbeResult:
        if seen is not None:
            seen["authorization"] = authorization
        return ProbeResult(
            tools=(
                ConnectorToolInfo(name="search", description="find things"),
                ConnectorToolInfo(name="send", description="send a thing"),
            ),
            instructions="Use search before send.",
            server_info={"name": "example", "version": "1.0"},
        )

    monkeypatch.setattr(ops, "probe_server", probe)


async def test_the_full_dance(db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch):
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)

    row = await ops.create_connector(db, actor, server_url=MCP, name="Example")
    assert row.status == ConnectorStatus.PENDING_AUTH and row.auth_kind == "oauth"
    assert row.last_error == "the server requires authorization"

    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"{AS}/authorize?")
    assert query["code_challenge_method"] == "S256" and query["client_id"] == "dcr-client"
    assert query["resource"] == MCP and query["scope"] == "read write"
    assert row.status == ConnectorStatus.AUTHORIZING and row.oauth_state_hash is not None

    # The row's sealed half is sealed: ciphertext only, none of the secrets legible.
    for column in (row.client_info_enc, row.flow_enc):
        assert column is not None and column.startswith("gAAAAA")
        assert "dcr-secret" not in column and "verifier" not in column

    _live_probe(monkeypatch, seen := {})
    outcome = await ops.handle_callback(db, binding=BROWSER, state=query["state"], code="the-code")
    assert outcome.ok, outcome.message

    form = server.token_form()
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["redirect_uri"].endswith("/v1/connectors/oauth/callback")
    assert s256_challenge(form["code_verifier"]) == query["code_challenge"], "the PKCE round trip"
    assert form["resource"] == MCP
    assert form["client_secret"] == "dcr-secret", "DCR minted a secret ⇒ client_secret_post"

    await db.refresh(row)
    assert row.status == ConnectorStatus.LIVE and row.token_expires_at is not None
    assert row.tool_names() == ("search", "send") and row.instructions == "Use search before send."
    assert seen["authorization"] == "Bearer tok_access_1", "the probe carried the fresh token"
    assert row.tokens_enc is not None and row.tokens_enc.startswith("gAAAAA")
    assert "tok_access_1" not in row.tokens_enc and "tok_refresh_1" not in row.tokens_enc
    assert row.oauth_state_hash is None, "the state was consumed"


async def test_state_replay_and_ttl_refused(db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch):
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Replay")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    state = parse_qs(urlsplit(url).query)["state"][0]

    _live_probe(monkeypatch)
    assert (await ops.handle_callback(db, binding=BROWSER, state=state, code="c")).ok
    second = await ops.handle_callback(db, binding=BROWSER, state=state, code="c")
    assert not second.ok and "already used" in second.message

    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    state = parse_qs(urlsplit(url).query)["state"][0]
    row.flow_expires_at = utcnow() - dt.timedelta(seconds=1)
    await db.commit()
    expired = await ops.handle_callback(db, binding=BROWSER, state=state, code="c")
    assert not expired.ok, "an expired state must not exchange"


async def test_idp_error_lands_honestly(db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch):
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Refused")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    state = parse_qs(urlsplit(url).query)["state"][0]

    outcome = await ops.handle_callback(
        db, binding=BROWSER, state=state, error="access_denied", error_description="nope"
    )
    assert not outcome.ok
    await db.refresh(row)
    assert row.status == ConnectorStatus.PENDING_AUTH
    assert row.last_error is not None and "access_denied" in row.last_error
    assert row.oauth_state_hash is None, "a refusal still consumes the state"
    assert not any(r.url.path == "/token" for r in server.requests), "no exchange was attempted"


async def test_preregistered_client_skips_dcr_and_uses_basic(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    server = CannedAS(with_registration=False)
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(
        db, actor, server_url=MCP, name="Prereg", client_id="my-app", client_secret="my-secret"
    )
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["client_id"] == "my-app"
    assert not any(r.url.path == "/register" for r in server.requests), "no DCR attempted"

    _live_probe(monkeypatch)
    assert (await ops.handle_callback(db, binding=BROWSER, state=query["state"], code="c")).ok
    auth = server.token_headers().get("authorization", "")
    assert auth.startswith("Basic "), "a pre-registered secret authenticates client_secret_basic"
    assert "client_secret" not in server.token_form()


async def test_dcr_less_server_without_credentials_names_the_fix(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    server = CannedAS(with_registration=False)
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="NoDcr")
    with pytest.raises(Conflict, match="client-id"):
        await ops.begin_authorization(db, actor, row.id, binding=BROWSER)


async def test_discovery_all_dead_falls_back_to_default_endpoints(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    dead = CannedAS(with_metadata=False)

    def handler(request: httpx.Request) -> httpx.Response:
        dead.requests.append(request)
        if request.url.path == "/register":
            return httpx.Response(
                201,
                json={
                    "redirect_uris": [ops.callback_url()],
                    "client_id": "fallback-client",
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        connector_oauth,
        "http_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Bare")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    assert url.startswith("https://mcp.example.test/authorize?"), "the MCP-spec default endpoints"


async def test_refresh_rotates_and_persists(db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch):
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Rotate")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    _live_probe(monkeypatch)
    assert (
        await ops.handle_callback(
            db, binding=BROWSER, state=parse_qs(urlsplit(url).query)["state"][0], code="c"
        )
    ).ok

    row.token_expires_at = utcnow() + dt.timedelta(seconds=30)  # inside the horizon
    await db.commit()
    server.token_answer = {
        "access_token": "tok_access_2",
        "token_type": "bearer",
        "expires_in": 3600,
        "refresh_token": "tok_refresh_2",
    }
    await ops.refresh_if_stale(db, row)
    form = server.token_form()
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "tok_refresh_1"
    tokens = json.loads(cipher.decrypt(row.tokens_enc or ""))
    assert tokens["access_token"] == "tok_access_2" and tokens["refresh_token"] == "tok_refresh_2"

    # A server that rotates nothing: the old refresh token is carried forward, not dropped.
    row.token_expires_at = utcnow() + dt.timedelta(seconds=30)
    await db.commit()
    server.token_answer = {"access_token": "tok_access_3", "token_type": "bearer", "expires_in": 3600}
    await ops.refresh_if_stale(db, row)
    tokens = json.loads(cipher.decrypt(row.tokens_enc or ""))
    assert tokens["access_token"] == "tok_access_3" and tokens["refresh_token"] == "tok_refresh_2"


async def test_refresh_refused_marks_needs_reauth(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Stale")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    _live_probe(monkeypatch)
    assert (
        await ops.handle_callback(
            db, binding=BROWSER, state=parse_qs(urlsplit(url).query)["state"][0], code="c"
        )
    ).ok

    row.token_expires_at = utcnow() + dt.timedelta(seconds=30)
    await db.commit()

    original = server.handler

    def refusing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            server.requests.append(request)
            return httpx.Response(400, json={"error": "invalid_grant"})
        return original(request)

    monkeypatch.setattr(
        connector_oauth,
        "http_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refusing), follow_redirects=False),
    )
    await ops.refresh_if_stale(db, row)
    assert row.status == ConnectorStatus.NEEDS_REAUTH


async def test_door_rejections(db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValidationFailed):
        await ops.create_connector(db, actor, server_url="https://10.0.0.8/mcp")
    _live_probe(monkeypatch)
    await ops.create_connector(db, actor, server_url=MCP, name="Twice")
    with pytest.raises(Conflict):
        await ops.create_connector(db, actor, server_url=MCP, name="Twice")


async def test_anonymous_server_goes_straight_to_live(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    _live_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Open")
    assert row.status == ConnectorStatus.LIVE and row.auth_kind == "none"
    assert row.tool_names() == ("search", "send")


async def test_scope_walls(db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch):
    _live_probe(monkeypatch)
    mine = await ops.create_connector(db, actor, server_url=MCP, name="Mine")
    other, _ = await member(db, "Rex")
    await db.commit()
    from prismoid.errors import NotFound

    with pytest.raises(NotFound):
        await ops.get_connector(db, other, mine.id)
    assert await ops.list_connectors(db, other) == []
    resolved = await Connector.resolve_refs(db, other.id, ["mine"])
    assert resolved == {}, "another owner's slug does not resolve - existence is not leaked"


async def test_hostile_discovery_endpoints_are_refused(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """The egress door does not end at the first hop: metadata naming an
    internal authorization server - or an internal token leg, in any IP spelling - is a
    Conflict at begin_authorization, before anything dials it. Asserted under the PROD
    posture: with the dev flag on, loopback (in any spelling) is deliberately admitted."""
    monkeypatch.setattr(settings.connectors, "allow_insecure_localhost", False)

    def internal_as(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200, json={"resource": MCP, "authorization_servers": ["https://2130706433"]}
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        connector_oauth,
        "http_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(internal_as), follow_redirects=False),
    )
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Hostile AS")
    with pytest.raises(Conflict, match="authorization server"):
        await ops.begin_authorization(db, actor, row.id, binding=BROWSER)

    def internal_token(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [AS]})
        if request.url.path.startswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": AS,
                    "authorization_endpoint": f"{AS}/authorize",
                    "token_endpoint": "https://0x7f000001/token",
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        connector_oauth,
        "http_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(internal_token), follow_redirects=False),
    )
    row2 = await ops.create_connector(db, actor, server_url=MCP + "2", name="Hostile token")
    with pytest.raises(Conflict, match="token endpoint"):
        await ops.begin_authorization(db, actor, row2.id, binding=BROWSER)


async def test_abandoned_authorization_reverts_at_the_next_read(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """An abandoned consent tab must not park `authorizing` forever: past the flow TTL the
    next read reverts the row honestly and retires the dead state hash - the web's poll
    settles, and Connect comes back."""
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Ghost")
    await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    assert row.status == ConnectorStatus.AUTHORIZING

    listed = await ops.list_connectors(db, actor)
    assert listed[0].status == ConnectorStatus.AUTHORIZING, "a flow inside its TTL is untouched"

    row.flow_expires_at = utcnow() - dt.timedelta(seconds=1)
    await db.commit()
    listed = await ops.list_connectors(db, actor)
    assert listed[0].status == ConnectorStatus.PENDING_AUTH, "no tokens yet → back to pending"
    assert listed[0].last_error == "authorization was not completed"
    assert listed[0].oauth_state_hash is None, "the dead flow's claim ticket is retired"

    # With tokens at rest, the honest revert is needs_reauth - the grant existed, the flow died.
    row.status = ConnectorStatus.AUTHORIZING
    row.flow_expires_at = utcnow() - dt.timedelta(seconds=1)
    row.tokens_enc = cipher.encrypt('{"access_token": "t", "token_type": "Bearer"}')
    await db.commit()
    fetched = await ops.get_connector(db, actor, row.id)
    assert fetched.status == ConnectorStatus.NEEDS_REAUTH


# ── one-click connect + the web landing ───────────────────────────────────────────────────────


async def test_one_click_connect_is_one_round_trip_to_the_consent_url(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """`connect(catalog_slug=...)` creates the row AND begins authorization: the caller holds
    the consent URL with one call, named after the catalog entry, provenance stamped."""
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)

    row, url = await ops.connect(db, actor, binding=BROWSER, catalog_slug="notion")
    assert row.name == "Notion" and row.slug == "notion" and row.catalog_slug == "notion"
    assert row.status == ConnectorStatus.AUTHORIZING
    assert url is not None and url.startswith(f"{AS}/authorize?")


async def test_one_click_connect_on_an_open_server_is_just_live(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    server = CannedAS()
    server.install(monkeypatch)
    _live_probe(monkeypatch)
    row, url = await ops.connect(db, actor, binding=BROWSER, server_url=MCP)
    assert row.status == ConnectorStatus.LIVE and url is None


async def test_one_click_connect_lands_a_begin_failure_on_the_row_never_a_409(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """A DCR-less server without credentials: the old two-step path raised Conflict; the
    one-button path keeps the row and wears the fix - a card showing what to do beats a 409
    hiding the connector it just made."""
    server = CannedAS(with_registration=False)
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row, url = await ops.connect(db, actor, binding=BROWSER, server_url=MCP, name="NoDcr")
    assert url is None
    assert row.last_error is not None and "client registration" in row.last_error


async def test_connect_refuses_zero_or_two_targets(db: AsyncSession, actor: Actor):
    with pytest.raises(ValidationFailed):
        await ops.connect(db, actor, binding=BROWSER)
    with pytest.raises(ValidationFailed):
        await ops.connect(db, actor, binding=BROWSER, catalog_slug="notion", server_url=MCP)
    with pytest.raises(ValidationFailed, match="catalog"):
        await ops.connect(db, actor, binding=BROWSER, catalog_slug="not_a_real_entry")


async def test_the_callback_redirects_home_when_a_web_exists(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """With `web_base_url` set the browser lands back IN the product - and ONLY server-derived
    values ride the Location (the slug and the ok bit; provider error text never does).
    Unset keeps the static page: CLI-only boxes still get a human answer."""
    from fastapi.responses import HTMLResponse

    from prismoid.api.endpoints import connectors as api
    from prismoid.api.serializers.connectors import OAuthReturnIn

    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Example")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    state = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}["state"]

    _live_probe(monkeypatch)
    monkeypatch.setattr(settings.api, "web_base_url", "http://localhost:5173")
    answer = await api.oauth_callback(OAuthReturnIn(state=state, code="the-code"), db, binding=BROWSER)
    assert answer.status_code == 302
    assert answer.headers["location"] == "http://localhost:5173/connectors?connected=example&ok=1"

    # A dead state with the web configured: no row was claimed, so no slug - the static page
    # answers (a stranger's stale link learns nothing and goes nowhere).
    monkeypatch.setattr(settings.api, "web_base_url", "http://localhost:5173")
    answer = await api.oauth_callback(OAuthReturnIn(state="cn_bogus", code="x"), db, binding=BROWSER)
    assert isinstance(answer, HTMLResponse) and answer.status_code == 400

    # No web configured: the static page, as always. (The probe swaps back to 401-first -
    # a live-probed row would refuse the dance outright, correctly.)
    monkeypatch.setattr(settings.api, "web_base_url", "")
    _needs_auth_probe(monkeypatch)
    row2 = await ops.create_connector(db, actor, server_url=MCP + "2", name="Second")
    url2 = await ops.begin_authorization(db, actor, row2.id, binding=BROWSER)
    _live_probe(monkeypatch)
    state2 = {k: v[0] for k, v in parse_qs(urlsplit(url2).query).items()}["state"]
    answer2 = await api.oauth_callback(OAuthReturnIn(state=state2, code="the-code"), db, binding=BROWSER)
    assert isinstance(answer2, HTMLResponse) and answer2.status_code == 200


async def test_the_callback_finishes_only_in_the_browser_that_started_it(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """The `cn_` state alone is shareable: sent to a victim, their consent would land THEIR tokens on
    the sender's connector. Starting a flow sets an HttpOnly, SameSite=Lax binding cookie; the
    callback refuses a browser without it (or with another one) and exchanges nothing."""
    from httpx import ASGITransport, AsyncClient

    from prismoid.api.app import create_app
    from prismoid.operations import auth as auth_ops

    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    monkeypatch.setattr(settings.api, "web_base_url", "")
    row = await ops.create_connector(db, actor, server_url=MCP, name="Bound")
    _, key = await auth_ops.create_api_key(db, actor, "browser")
    h = {"Authorization": f"Bearer {key}"}

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as starter:
        started = await starter.post(f"/v1/connectors/{row.id}/authorize", headers=h)
        assert started.status_code == 200, started.text
        cookie = started.headers.get("set-cookie", "")
        assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower(), cookie
        state = parse_qs(urlsplit(started.json()["authorize_url"]).query)["state"][0]

        _live_probe(monkeypatch)
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as victim:
            stolen = await victim.get("/v1/connectors/oauth/callback", params={"state": state, "code": "c"})
        assert stolen.status_code == 400, stolen.text
        assert not any(r.url.path == "/token" for r in server.requests), (
            "a foreign browser's code is never exchanged"
        )
        await db.refresh(row)
        assert row.status != ConnectorStatus.LIVE and row.tokens_enc is None

        # The starter's own browser (its cookie jar carries the binding) finishes a fresh flow.
        _needs_auth_probe(monkeypatch)
        again = await starter.post(f"/v1/connectors/{row.id}/authorize", headers=h)
        state = parse_qs(urlsplit(again.json()["authorize_url"]).query)["state"][0]
        _live_probe(monkeypatch)
        done = await starter.get("/v1/connectors/oauth/callback", params={"state": state, "code": "c"})
        assert done.status_code == 200, done.text
    await db.refresh(row)
    assert row.status == ConnectorStatus.LIVE


async def test_another_browsers_binding_spends_the_state_and_exchanges_nothing(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    server = CannedAS()
    server.install(monkeypatch)
    _needs_auth_probe(monkeypatch)
    row = await ops.create_connector(db, actor, server_url=MCP, name="Elsewhere")
    url = await ops.begin_authorization(db, actor, row.id, binding=BROWSER)
    state = parse_qs(urlsplit(url).query)["state"][0]
    _live_probe(monkeypatch)
    outcome = await ops.handle_callback(db, binding="cb_" + "x" * 43, state=state, code="c")
    assert not outcome.ok and "another browser" in outcome.message
    assert not any(r.url.path == "/token" for r in server.requests)
    await db.refresh(row)
    assert row.status == ConnectorStatus.PENDING_AUTH and row.tokens_enc is None
    assert row.oauth_state_hash is None, "spent: the rightful browser starts again"
    again = await ops.handle_callback(db, binding=BROWSER, state=state, code="c")
    assert not again.ok
