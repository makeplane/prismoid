# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connectors - external MCP servers a user's tasks can reach (ADR-020).

Thin: auth → validate → one operation call → serialize. ≤20 lines each, no business
logic, no `ai/` import, no transaction. `oauth_callback` is the fourth unauthenticated route
(amended by ADR-020): a browser returning from an identity provider carries no bearer
token, so the single-use hashed `state` parameter IS the authentication - in the browser that
started the flow only (the HttpOnly binding cookie the starts set) - and the answer is a human
page, not JSON - the watch-page precedent.
"""

from __future__ import annotations

import html
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from prismoid.api.deps import ActorDep, OAuthBindingDep, OAuthReturnBinding, PageDep, SessionDep
from prismoid.api.serializers.common import OkOut, PageOut
from prismoid.api.serializers.connectors import (
    AppFinishIn,
    AppSignInIn,
    AuthorizeIn,
    AuthorizeOut,
    CatalogOut,
    ConnectorConnectIn,
    ConnectorConnectOut,
    ConnectorCreateIn,
    ConnectorOut,
    ConnectorUpdateIn,
    OAuthReturnIn,
    PoliciesIn,
    catalog_entry_out,
    connector_out,
)
from prismoid.conf import settings
from prismoid.operations import connectors as ops
from prismoid.schemas.connector_catalog import CATALOG


async def get_catalog(actor: ActorDep) -> CatalogOut:
    """The data catalog, verbatim. Auth'd like every route; no rows touched."""
    return CatalogOut(items=[catalog_entry_out(e) for e in CATALOG])


async def create_connector(body: ConnectorCreateIn, actor: ActorDep, session: SessionDep) -> ConnectorOut:
    row = await ops.create_connector(
        session,
        actor,
        server_url=body.server_url,
        name=body.name,
        bearer_token=body.bearer_token,
        client_id=body.client_id,
        client_secret=body.client_secret,
    )
    return connector_out(row)


async def list_connectors(actor: ActorDep, session: SessionDep, page: PageDep) -> PageOut[ConnectorOut]:
    rows = await ops.list_connectors(session, actor, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1].cursor() if len(rows) == page.limit else None
    return PageOut(items=[connector_out(c) for c in rows], next_cursor=next_cursor)


async def get_connector(connector_id: UUID, actor: ActorDep, session: SessionDep) -> ConnectorOut:
    return connector_out(await ops.get_connector(session, actor, connector_id))


async def connect_connector(
    body: ConnectorConnectIn, actor: ActorDep, session: SessionDep, binding: OAuthBindingDep
) -> ConnectorConnectOut:
    """One-click connect: create + begin authorization in one round trip."""
    row, authorize_url = await ops.connect(
        session,
        actor,
        catalog_slug=body.catalog_slug,
        server_url=body.server_url,
        name=body.name,
        bearer_token=body.bearer_token,
        client_id=body.client_id,
        client_secret=body.client_secret,
        binding=binding,
        app=_app(body.app),
    )
    return ConnectorConnectOut(
        connector=connector_out(row), authorize_url=authorize_url, app_sign_in=body.app is not None
    )


async def rename_connector(
    connector_id: UUID, body: ConnectorUpdateIn, actor: ActorDep, session: SessionDep
) -> ConnectorOut:
    row = await ops.update_connector(
        session, actor, connector_id, name=body.name, auto_attach=body.auto_attach
    )
    return connector_out(row)


async def set_policies(
    connector_id: UUID, body: PoliciesIn, actor: ActorDep, session: SessionDep
) -> ConnectorOut:
    row = await ops.set_tool_policies(
        session, actor, connector_id, policies={k: v.value for k, v in body.policies.items()}
    )
    return connector_out(row)


async def disable_connector(connector_id: UUID, actor: ActorDep, session: SessionDep) -> ConnectorOut:
    return connector_out(await ops.disable_connector(session, actor, connector_id))


async def enable_connector(connector_id: UUID, actor: ActorDep, session: SessionDep) -> ConnectorOut:
    return connector_out(await ops.enable_connector(session, actor, connector_id))


async def delete_connector(connector_id: UUID, actor: ActorDep, session: SessionDep) -> OkOut:
    await ops.delete_connector(session, actor, connector_id)
    return OkOut(ok=True)


async def authorize_connector(
    connector_id: UUID,
    actor: ActorDep,
    session: SessionDep,
    binding: OAuthBindingDep,
    body: AuthorizeIn | None = None,
) -> AuthorizeOut:
    app = _app(body.app if body else None)
    url = await ops.begin_authorization(session, actor, connector_id, binding=binding, app=app)
    return AuthorizeOut(authorize_url=url, app_sign_in=app is not None)


async def finish_app_sign_in(
    connector_id: UUID, body: AppFinishIn, actor: ActorDep, session: SessionDep
) -> ConnectorOut:
    """The phone app finishes an OAuth sign-in the callback handed back to it."""
    row = await ops.finish_app_sign_in(
        session, actor, connector_id, return_code=body.return_code, code_verifier=body.code_verifier
    )
    return connector_out(row)


def _app(body: AppSignInIn | None) -> ops.AppSignIn | None:
    return None if body is None else ops.AppSignIn(body.code_challenge, body.return_scheme)


async def probe_connector(connector_id: UUID, actor: ActorDep, session: SessionDep) -> ConnectorOut:
    return connector_out(await ops.reprobe_connector(session, actor, connector_id))


async def oauth_callback(
    q: Annotated[OAuthReturnIn, Query()], session: SessionDep, binding: OAuthReturnBinding = ""
) -> Response:
    outcome = await ops.handle_callback(session, binding=binding, **q.model_dump())  # the four query keys
    if outcome.app_return:  # an app sign-in goes home to the app (a fixed scheme), never the web
        return RedirectResponse(outcome.app_return, status_code=302)
    web = settings.api.web_base_url.rstrip("/")
    if web and outcome.connector_slug:
        # Land the browser back IN the product. Only server-derived values ride the
        # Location header - the slug and the ok bit; a provider's error_description is
        # reflected content and stays off the wire (the web reads the row for detail).
        target = f"{web}/connectors?connected={quote(outcome.connector_slug)}&ok={1 if outcome.ok else 0}"
        return RedirectResponse(target, status_code=302)
    return HTMLResponse(
        _page(outcome.ok, outcome.message),
        status_code=200 if outcome.ok else 400,
        # Defence in depth behind the escaping below: this page needs no scripts, images, or remote
        # assets, so forbid them all. An injected `<script>` cannot run even if escaping ever slipped.
        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
    )


def _page(ok: bool, message: str) -> str:
    """A tiny, self-contained human page - no assets, no scripts, no redirect target to configure."""
    mark, tone = ("✓", "#15803d") if ok else ("✕", "#b91c1c")  # server-fixed literals, not user input
    # `message` can carry the provider's `error_description` - attacker-controllable via the callback
    # URL. F-stringing it raw was reflected HTML injection; escape it into inert text.
    safe = html.escape(message)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Prismoid</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
        "<body style='font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;"
        "min-height:100vh;margin:0;background:#fafaf9;color:#1c1917'>"
        f"<div style='text-align:center;padding:2rem'><div style='font-size:2.5rem;color:{tone}'>{mark}</div>"
        f"<p style='max-width:28rem'>{safe}</p>"
        "<p style='color:#78716c;font-size:13px'>Prismoid · connectors</p></div></body></html>"
    )
