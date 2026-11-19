# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The phone app's OAuth sign-in (RFC 8252), end to end against the canned authorization server.

The app starts with the S256 hash of a secret only it holds; the callback exchanges nothing,
keeps the code sealed with the flow, and sends the browser to the app's scheme with a one-time
return code; the app finishes with that code and its secret. A shared consent link still lands
nothing: whoever approves it hands the code to THEIR app, which is not the connector's owner
and has no secret, and the owner never sees the return code. The web's flow is untouched.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, NotFound
from prismoid.operations import cipher
from prismoid.operations import connectors as ops
from prismoid.schemas.connectors import (
    APP_RETURN_SCHEMES,
    ConnectorStatus,
    ConnectorToolInfo,
    ProbeResult,
    pkce_pair,
)
from tests.integration.connectors.test_connector_oauth import MCP, CannedAS
from tests.support.cast import member

pytestmark = [pytest.mark.integration]

BROWSER = "cb_" + "b" * 43


@pytest.fixture(autouse=True)
def own_cipher_key(monkeypatch: pytest.MonkeyPatch):
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


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> CannedAS:
    canned = CannedAS()
    canned.install(monkeypatch)
    from prismoid.ai.tools.connectors import ProbeAuthRequired

    async def needs_auth(url: str, **kw: Any) -> ProbeResult:
        raise ProbeAuthRequired(
            'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
        )

    monkeypatch.setattr(ops, "probe_server", needs_auth)
    return canned


def _go_live(monkeypatch: pytest.MonkeyPatch) -> None:
    async def live(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(tools=(ConnectorToolInfo(name="search", description="find"),), server_info={})

    monkeypatch.setattr(ops, "probe_server", live)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _started(db: AsyncSession, actor: Actor, scheme: str = "prismoid-dev") -> tuple[Any, str, str]:
    """A connector mid app sign-in: (row, the authorize URL's state, the app's secret)."""
    row = await ops.create_connector(db, actor, server_url=MCP, name="Example")
    verifier, challenge = pkce_pair()
    url = await ops.begin_authorization(
        db, actor, row.id, binding=BROWSER, app=ops.AppSignIn(challenge, scheme)
    )
    return row, _query(url)["state"], verifier


def _exchanged(server: CannedAS) -> bool:
    return any(r.url.path == "/token" for r in server.requests)


async def test_the_app_sign_in_comes_home_to_the_app_and_finishes_there(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    row, state, verifier = await _started(db, actor)
    flow = json.loads(cipher.decrypt(row.flow_enc or ""))
    assert "binding" not in flow and flow["app"]["scheme"] == "prismoid-dev"

    # The callback needs no cookie, exchanges nothing, and sends the browser to the app.
    outcome = await ops.handle_callback(db, binding="", state=state, code="the-code")
    assert outcome.ok and outcome.app_return.startswith("prismoid-dev://connectors/return?")
    back = _query(outcome.app_return)
    assert back["connector_id"] == str(row.id) and back["ok"] == "1"
    assert back["return_code"].startswith("ca_")
    assert "the-code" not in outcome.app_return, "the provider's code never rides the redirect"
    assert not _exchanged(server)
    await db.refresh(row)
    assert row.status == ConnectorStatus.AUTHORIZING
    assert row.flow_enc is not None and "the-code" not in row.flow_enc, "sealed at rest"

    _go_live(monkeypatch)
    done = await ops.finish_app_sign_in(
        db, actor, row.id, return_code=back["return_code"], code_verifier=verifier
    )
    assert done.status == ConnectorStatus.LIVE
    assert server.token_form()["code"] == "the-code"
    assert server.token_form()["redirect_uri"].endswith("/v1/connectors/oauth/callback")

    with pytest.raises(Conflict):  # single use
        await ops.finish_app_sign_in(
            db, actor, row.id, return_code=back["return_code"], code_verifier=verifier
        )


async def test_a_wrong_secret_spends_the_handed_code(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    row, state, verifier = await _started(db, actor)
    back = _query((await ops.handle_callback(db, binding="", state=state, code="c")).app_return)

    other, _ = pkce_pair()
    refused = await ops.finish_app_sign_in(
        db, actor, row.id, return_code=back["return_code"], code_verifier=other
    )
    assert refused.status == ConnectorStatus.PENDING_AUTH and refused.last_error
    assert not _exchanged(server)
    with pytest.raises(Conflict):  # spent: the right secret is too late now
        await ops.finish_app_sign_in(
            db, actor, row.id, return_code=back["return_code"], code_verifier=verifier
        )
    flow = json.loads(cipher.decrypt(refused.flow_enc or ""))
    assert "code" not in flow and "app" not in flow


async def test_a_wrong_return_code_spends_the_handed_code(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    row, state, verifier = await _started(db, actor)
    back = _query((await ops.handle_callback(db, binding="", state=state, code="c")).app_return)
    refused = await ops.finish_app_sign_in(
        db, actor, row.id, return_code="ca_" + "x" * 43, code_verifier=verifier
    )
    assert refused.status != ConnectorStatus.LIVE and not _exchanged(server)
    with pytest.raises(Conflict):
        await ops.finish_app_sign_in(
            db, actor, row.id, return_code=back["return_code"], code_verifier=verifier
        )


async def test_someone_elses_app_cannot_finish_it(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    """The shared-link case: the person who approved gets the return code in THEIR app, but the
    connector is not theirs - and they never had the owner's secret."""
    row, state, verifier = await _started(db, actor)
    back = _query((await ops.handle_callback(db, binding="", state=state, code="c")).app_return)
    stranger, _ = await member(db, "Bo")
    await db.commit()
    with pytest.raises(NotFound):
        await ops.finish_app_sign_in(
            db, stranger, row.id, return_code=back["return_code"], code_verifier=verifier
        )
    assert not _exchanged(server)


async def test_a_late_finish_is_refused(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    row, state, verifier = await _started(db, actor)
    back = _query((await ops.handle_callback(db, binding="", state=state, code="c")).app_return)
    await db.refresh(row)
    row.flow_expires_at = utcnow() - dt.timedelta(seconds=1)
    await db.commit()
    with pytest.raises(Conflict):
        await ops.finish_app_sign_in(
            db, actor, row.id, return_code=back["return_code"], code_verifier=verifier
        )
    assert not _exchanged(server)


async def test_a_provider_refusal_goes_back_to_the_app(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    row, state, _ = await _started(db, actor, scheme="prismoid")
    outcome = await ops.handle_callback(
        db, binding="", state=state, error="access_denied", error_description="<b>nope</b>"
    )
    assert not outcome.ok
    back = _query(outcome.app_return)
    assert outcome.app_return.startswith("prismoid://") and back["ok"] == "0"
    assert "return_code" not in back and "nope" not in outcome.app_return, "provider text stays off"
    await db.refresh(row)
    assert row.status == ConnectorStatus.PENDING_AUTH and "access_denied" in (row.last_error or "")


async def test_over_http_the_app_flow_needs_no_cookie_and_lands_in_the_app(
    db: AsyncSession, actor: Actor, server: CannedAS, monkeypatch: pytest.MonkeyPatch
):
    from httpx import ASGITransport, AsyncClient

    from prismoid.api.app import create_app
    from prismoid.operations import auth as auth_ops

    monkeypatch.setattr(settings.api, "web_base_url", "http://localhost:5173")
    _, key = await auth_ops.create_api_key(db, actor, "phone")
    h = {"Authorization": f"Bearer {key}"}
    verifier, challenge = pkce_pair()
    app = {"code_challenge": challenge, "return_scheme": "prismoid-staging"}

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as phone:
        started = await phone.post("/v1/connectors/connect", headers=h, json={"server_url": MCP, "app": app})
        assert started.status_code == 201, started.text
        assert started.json()["app_sign_in"] is True
        connector_id = started.json()["connector"]["id"]
        state = _query(started.json()["authorize_url"])["state"]

        bad = await phone.post(
            f"/v1/connectors/{connector_id}/authorize",
            headers=h,
            json={"app": {**app, "return_scheme": "https"}},
        )
        assert bad.status_code == 422, "only the fixed app schemes"

        # The browser is a different cookie jar entirely - the app flow does not care.
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as browser:
            landed = await browser.get("/v1/connectors/oauth/callback", params={"state": state, "code": "c"})
        assert landed.status_code == 302
        location = landed.headers["location"]
        assert location.startswith("prismoid-staging://connectors/return?"), "the app, not the web"

        _go_live(monkeypatch)
        finished = await phone.post(
            f"/v1/connectors/{connector_id}/oauth/finish",
            headers=h,
            json={"return_code": _query(location)["return_code"], "code_verifier": verifier},
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "live"

        # The web's start is unchanged: no body, no app sign-in, the binding cookie as before.
        web = await phone.post(f"/v1/connectors/{connector_id}/authorize", headers=h)
        assert web.status_code == 200 and web.json()["app_sign_in"] is False
        assert "httponly" in web.headers.get("set-cookie", "").lower()


def test_the_api_offers_exactly_the_app_schemes():
    from typing import get_args

    from prismoid.api.serializers.connectors import AppSignInIn

    offered = get_args(AppSignInIn.model_fields["return_scheme"].annotation)
    assert tuple(offered) == APP_RETURN_SCHEMES
