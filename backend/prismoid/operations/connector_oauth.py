# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The OAuth 2.1 legs of a connector (ADR-020): discovery, client registration, the consent flow,
the code exchange and token refresh.

Two ways a consent flow comes home. The web's: the browser that started it returns to the
callback, which exchanges the code there (the binding cookie proves it is that browser). The
phone app's (RFC 8252): the app starts with the hash of a secret only it holds, the callback
keeps the code and hands the app a one-time return code, and the app finishes - owner, return
code and secret all matching - so a shared consent link still lands nothing anywhere.

Every leg here dials a remote authorization server, so the file keeps the two disciplines of
`operations/connectors.py`: commit before HTTP (a session sits committed and idle across every
network call), and tokens first (the code exchange commits ciphertext before anyone probes).
Every discovered endpoint passes the same SSRF door the server URL did (`_checked_legs`), and the
flow is sealed with `operations/cipher`; the single-use hashed state is spent by
`Connector.consume_state` in `connectors.handle_callback` before `complete_flow` runs.

The redirect URI is built HERE (not `api/links.py` - operations sits below `api` and may not
import it): one f-string over `settings.api.public_base_url`, the reserved stub made real.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import logfire
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyUrl, BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.tools.connectors import connector_http_client
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.connector import Connector
from prismoid.errors import Conflict
from prismoid.operations import cipher
from prismoid.operations import inbox as inbox_ops
from prismoid.schemas.connectors import (
    AuthKind,
    ConnectorStatus,
    app_return_url,
    auth_server_metadata_urls,
    check_server_url,
    default_endpoints,
    pkce_pair,
    protected_resource_urls,
    refresh_request,
    s256_challenge,
    sanitize_server_text,
    token_request,
)
from prismoid.schemas.connectors import (
    authorize_url as build_authorize_url,
)
from prismoid.schemas.ids import hash_token, new_connector_return_code, new_connector_state

CLIENT_NAME = "Prismoid"
FLOW_TTL = dt.timedelta(minutes=10)  # authorize → callback; a consent screen, not a coffee break
APP_RETURN_TTL = dt.timedelta(minutes=5)  # callback → the app's finish; the app is already open
REFRESH_HORIZON = dt.timedelta(minutes=5)  # refresh anything expiring inside a step's plausible life


def callback_url() -> str:
    """The OAuth 2.1 redirect_uri, from PUBLIC_BASE_URL."""
    return f"{settings.api.public_base_url}/v1/connectors/oauth/callback"


# An OAuth leg's answer is a small JSON document (metadata, a client registration, a token), from
# a server the connecting user chose and the API process reads, so it is read only this far.
OAUTH_MAX_BYTES = 1024 * 1024


def _default_http() -> httpx.AsyncClient:
    # No redirects on auth legs (OAuth 2.1), and pinned: every leg dials only a vetted public address.
    return connector_http_client(timeout=httpx.Timeout(10.0), max_response_bytes=OAUTH_MAX_BYTES)


# The MockTransport seam: tests point this at a canned authorization server. One module-level
# factory, same shape as `ai/frames.new_frame` - swap the maker, not the call sites.
http_client_factory = _default_http


async def knock_broken(session: AsyncSession, row: Connector, previous: str, why: str) -> None:
    """The needs_reauth inbox knock - TRANSITION edge
    only: a connector that stays broken knocks once, not per probe. Rides the caller's
    transaction (inbox writers flush; the caller commits right after)."""
    if previous == ConnectorStatus.NEEDS_REAUTH:
        return
    await inbox_ops.connector_broken(
        session, actor_id=row.owner_actor_id, name=row.name, slug=row.slug, why=why
    )


# ── discovery ─────────────────────────────────────────────────────────────────────────────────

_OAUTH_LEGS: Final = ("authorization_endpoint", "token_endpoint", "registration_endpoint")


def _checked_endpoint(url: str, what: str) -> str:
    """Discovered OAuth legs pass the same door the server URL did - the egress posture does
    not end at the first hop. A remote server whose metadata points this process (or the
    owner's browser) at an internal address, in any spelling, is refused by name."""
    try:
        return check_server_url(url, allow_insecure_localhost=settings.connectors.allow_insecure_localhost)
    except ValueError as bad:
        raise Conflict(f"the server's {what} is not an address this process will dial - {bad}") from None


def _checked_legs(metadata: dict[str, Any]) -> dict[str, Any]:
    """Every endpoint we might visit out of discovered metadata, through the door above. The
    sealed copy in `flow_enc` is written only after this - callback/refresh legs trust it by
    provenance, so discovery is the ONE validation site."""
    for leg in _OAUTH_LEGS:
        if metadata.get(leg):
            metadata[leg] = _checked_endpoint(str(metadata[leg]), leg.replace("_", " "))
    return metadata


def _resource_hint(row: Connector) -> str | None:
    """The `resource_metadata` URL a 401 handed us, if it passes the SSRF door."""
    hint: str | None = None
    if row.flow_enc:
        stored = json.loads(cipher.decrypt(row.flow_enc))
        hint = stored.get("resource_metadata")
    if not hint:
        return hint
    try:
        return check_server_url(hint, allow_insecure_localhost=settings.connectors.allow_insecure_localhost)
    except ValueError as bad:
        # The hint came from the server's own WWW-Authenticate header - a URL we would
        # dial. One pointing inside the network is dropped; the candidates derived from
        # the already-checked server URL remain.
        logfire.warn("connector {slug} resource_metadata hint refused: {err}", slug=row.slug, err=str(bad))
        return None


async def _fetched[M: BaseModel](client: httpx.AsyncClient, url: str, model: type[M]) -> M | None:
    """One metadata document, or None when it is unreachable, not a 200, or not that shape."""
    try:
        answer = await client.get(url)
    except httpx.HTTPError:
        return None
    if answer.status_code != 200:
        return None
    try:
        return model.model_validate(answer.json())
    except (ValidationError, ValueError):
        return None


async def _protected_resource(
    client: httpx.AsyncClient, row: Connector, hint: str | None
) -> tuple[str | None, str, str | None]:
    """The first protected-resource metadata that answers: (the RFC 8707 resource, its scopes,
    its checked authorization server), or (None, "", None) when none does."""
    for candidate in ([hint] if hint else []) + protected_resource_urls(row.server_url):
        prm = await _fetched(client, candidate, ProtectedResourceMetadata)
        if prm is None:
            continue
        issuer = None
        if prm.authorization_servers:
            issuer = _checked_endpoint(str(prm.authorization_servers[0]), "authorization server")
        return str(prm.resource), " ".join(prm.scopes_supported or []), issuer
    return None, "", None


async def _discover(client: httpx.AsyncClient, row: Connector) -> tuple[dict[str, object], str | None, str]:
    """Resource → authorization-server metadata, per the MCP walk. Returns (AS metadata as a
    plain dict, the RFC 8707 resource, the space-joined scopes to request)."""
    resource, scopes, issuer = await _protected_resource(client, row, _resource_hint(row))
    if issuer is None:
        # The MCP default when no resource metadata exists: the server is its own AS.
        parts = urlsplit(row.server_url)
        issuer = f"{parts.scheme}://{parts.netloc}"

    for candidate in auth_server_metadata_urls(issuer):
        meta = await _fetched(client, candidate, OAuthMetadata)
        if meta is None:
            continue
        if not scopes:
            scopes = " ".join(meta.scopes_supported or [])
        return _checked_legs(meta.model_dump(mode="json", exclude_none=True)), resource, scopes

    return _checked_legs(dict(default_endpoints(issuer))), resource, scopes


async def _ensure_client(
    client: httpx.AsyncClient, row: Connector, metadata: dict[str, object]
) -> OAuthClientInformationFull:
    """The registration we hold, or the one we perform (RFC 7591). A server with neither a
    registration endpoint nor supplied credentials is a dead end worth naming precisely."""
    if row.client_info_enc:
        return OAuthClientInformationFull.model_validate_json(cipher.decrypt(row.client_info_enc))

    registration = metadata.get("registration_endpoint")
    if not registration:
        raise Conflict(
            "this server offers no client registration - add the connector again with "
            "--client-id/--client-secret from its developer console"
        )
    wanted = OAuthClientMetadata(
        redirect_uris=[AnyUrl(callback_url())],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=CLIENT_NAME,
    )
    answer = await client.post(str(registration), json=wanted.model_dump(mode="json", exclude_none=True))
    if answer.status_code not in (200, 201):
        raise Conflict(
            f"client registration was refused ({answer.status_code}) - "
            f"{sanitize_server_text(answer.text, 200)}"
        )
    info = OAuthClientInformationFull.model_validate(answer.json())
    row.client_info_enc = cipher.encrypt(info.model_dump_json(exclude_none=True))
    return info


# ── the consent flow ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AppSignIn:
    """A phone app starting the flow: the S256 hash of its secret, and which app build it is."""

    code_challenge: str
    return_scheme: str


async def begin_authorization(
    session: AsyncSession,
    actor: Actor,
    connector_id: uuid.UUID,
    *,
    binding: str,
    app: AppSignIn | None = None,
) -> str:
    """Discovery → registration → PKCE + state → the URL the owner's browser must visit.
    Legal on a live OAuth connector too (re-consent is how `needs_reauth` heals). `binding` is the
    starting browser's cookie value (`api.deps.oauth_binding`); the sealed flow keeps its hash and
    the callback finishes only in that browser. With `app`, the flow keeps the app's challenge
    instead and the callback hands the code to the app (`finish_app_flow`)."""
    row = await Connector.get_for(session, connector_id, actor)
    if row.auth_kind == AuthKind.BEARER:
        raise Conflict("this connector authenticates with a pasted token - there is nothing to authorize")
    if row.status == ConnectorStatus.LIVE and row.auth_kind == AuthKind.NONE:
        raise Conflict("this server needs no authorization - it is already live")

    await session.commit()  # the read is done; the network happens on an idle session
    async with http_client_factory() as client:
        metadata, resource, scopes = await _discover(client, row)
        info = await _ensure_client(client, row, metadata)

    state = new_connector_state()
    verifier, challenge = pkce_pair()
    auth_method = info.token_endpoint_auth_method or "client_secret_post"
    if info.client_secret is None:
        auth_method = "none"
    row.oauth_state_hash = hash_token(state)
    row.flow_expires_at = utcnow() + FLOW_TTL
    row.flow_enc = cipher.encrypt(
        json.dumps(
            {
                "verifier": verifier,
                "token_endpoint": str(metadata["token_endpoint"]),
                "auth_method": auth_method,
                "resource": resource,
                "scopes": scopes,
                **(
                    {"app": {"challenge": app.code_challenge, "scheme": app.return_scheme}}
                    if app is not None
                    else {"binding": hash_token(binding)}
                ),
            }
        )
    )
    row.auth_kind = AuthKind.OAUTH
    row.status = ConnectorStatus.AUTHORIZING
    row.key_id = cipher.key_id()
    await session.commit()

    return build_authorize_url(
        str(metadata["authorization_endpoint"]),
        client_id=info.client_id or "",
        redirect_uri=callback_url(),
        state=state,
        code_challenge=challenge,
        resource=resource,
        scopes=scopes or None,
    )


@dataclass(slots=True)
class CallbackOutcome:
    ok: bool
    message: str
    connector_name: str = ""
    # For the web landing: the only row-derived value that may ride the redirect URL.
    # Server-derived, never provider text - a Location header is reflected content.
    connector_slug: str = ""
    # An app sign-in: where the browser goes instead - the app, never the web.
    app_return: str = ""


async def complete_flow(
    session: AsyncSession,
    row: Connector,
    *,
    binding: str,
    code: str,
    error: str,
    error_description: str,
) -> CallbackOutcome | None:
    """The callback after its state token claimed `row`: the browser-binding proof, then the code
    exchange. Returns the refusal, or None once the tokens are durable. Commits on every path,
    and commits the claim before the exchange dials out. An app sign-in exchanges nothing here:
    it is handed to the app (`_hand_to_app`)."""
    flow = json.loads(cipher.decrypt(row.flow_enc or ""))
    if "app" in flow:
        return await _hand_to_app(session, row, flow, code=code, error=error, detail=error_description)
    refusal = await _refusal(
        session, row, flow, binding=binding, code=code, error=error, detail=error_description
    )
    if refusal is not None:
        return refusal
    return await _exchange(session, row, flow, code)


async def _exchange(
    session: AsyncSession, row: Connector, flow: dict[str, Any], code: str
) -> CallbackOutcome | None:
    """The code exchange, either way home: the refusal, or None once the tokens are durable."""
    info = OAuthClientInformationFull.model_validate_json(cipher.decrypt(row.client_info_enc or ""))
    data, headers = token_request(
        code=code,
        code_verifier=str(flow["verifier"]),
        redirect_uri=callback_url(),
        client_id=info.client_id or "",
        client_secret=info.client_secret,
        auth_method=str(flow["auth_method"]),
        resource=flow.get("resource"),
    )
    await session.commit()  # the claim is durable; the exchange happens on an idle session

    async with http_client_factory() as client:
        try:
            answer = await client.post(str(flow["token_endpoint"]), data=data, headers=headers)
        except httpx.HTTPError as bad:
            row.status = ConnectorStatus.PENDING_AUTH
            row.last_error = sanitize_server_text(f"token exchange failed: {bad}", 500)
            await session.commit()
            return CallbackOutcome(
                False, "The token exchange could not reach the provider.", row.name, row.slug
            )

    if answer.status_code != 200:
        row.status = ConnectorStatus.PENDING_AUTH
        row.last_error = sanitize_server_text(
            f"token exchange refused ({answer.status_code}): {answer.text[:200]}", 500
        )
        await session.commit()
        return CallbackOutcome(False, "The provider refused the token exchange.", row.name, row.slug)

    tokens = OAuthToken.model_validate(answer.json())
    _store_tokens(row, tokens)
    # The verifier is single-use; what refresh needs - endpoint, method, resource - stays.
    row.flow_enc = cipher.encrypt(
        json.dumps({k: flow[k] for k in ("token_endpoint", "auth_method", "resource", "scopes") if k in flow})
    )
    row.flow_expires_at = None
    row.scopes = tokens.scope or str(flow.get("scopes") or "")
    await session.commit()
    return None


async def _hand_to_app(
    session: AsyncSession, row: Connector, flow: dict[str, Any], *, code: str, error: str, detail: str
) -> CallbackOutcome:
    """An app sign-in's callback: keep the code sealed with the flow for `APP_RETURN_TTL` and send
    the browser to the app with a one-time return code. The browser proves nothing here - the
    finish does, with the owner's session and the app's secret. A refusal goes to the app too."""
    app = dict(flow["app"])
    if error or not code:
        row.status = ConnectorStatus.NEEDS_REAUTH if row.tokens_enc else ConnectorStatus.PENDING_AUTH
        row.last_error = sanitize_server_text(f"{error or 'authorization refused'}: {detail}", 500).rstrip(
            ": "
        )
        await session.commit()
        return CallbackOutcome(
            False,
            "The provider did not authorize the connection.",
            row.name,
            row.slug,
            app_return=app_return_url(str(app["scheme"]), str(row.id), return_code=None),
        )

    return_code = new_connector_return_code()
    app["return"] = hash_token(return_code)
    row.flow_enc = cipher.encrypt(json.dumps({**flow, "app": app, "code": code}))
    row.flow_expires_at = utcnow() + APP_RETURN_TTL
    await session.commit()
    return CallbackOutcome(
        True,
        "Back to the Prismoid app to finish connecting.",
        row.name,
        row.slug,
        app_return=app_return_url(str(app["scheme"]), str(row.id), return_code=return_code),
    )


async def finish_app_flow(
    session: AsyncSession, row: Connector, *, return_code: str, code_verifier: str
) -> CallbackOutcome | None:
    """The app comes back with the return code and its secret. `row` is already the caller's own
    (`Connector.get_for`). Both must match the flow, inside its TTL, once: any miss spends the
    handed code, so a second try starts a new sign-in. Returns the refusal, or None once the
    tokens are durable."""
    flow = json.loads(cipher.decrypt(row.flow_enc)) if row.flow_enc else {}
    app = flow.get("app") or {}
    held = (
        row.status == ConnectorStatus.AUTHORIZING
        and row.flow_expires_at is not None
        and row.flow_expires_at > utcnow()
        and "return" in app
        and "code" in flow
    )
    if not held:
        raise Conflict("no sign-in is waiting to be finished on this connector")
    if not (
        hmac.compare_digest(str(app["return"]), hash_token(return_code))
        and hmac.compare_digest(str(app["challenge"]), s256_challenge(code_verifier))
    ):
        row.status = ConnectorStatus.NEEDS_REAUTH if row.tokens_enc else ConnectorStatus.PENDING_AUTH
        row.last_error = "the app sign-in could not be finished"
        row.flow_expires_at = None
        row.flow_enc = cipher.encrypt(json.dumps({k: v for k, v in flow.items() if k not in ("app", "code")}))
        await session.commit()
        return CallbackOutcome(
            False, "This sign-in could not be finished. Connect again.", row.name, row.slug
        )

    code = str(flow.pop("code"))
    flow.pop("app")
    return await _exchange(session, row, flow, code)


async def _refusal(
    session: AsyncSession,
    row: Connector,
    flow: dict[str, Any],
    *,
    binding: str,
    code: str,
    error: str,
    detail: str,
) -> CallbackOutcome | None:
    """Why the callback stops before any exchange, committed: another browser finished the flow,
    or the provider sent an error or no code. None when the exchange may go ahead."""
    if not hmac.compare_digest(str(flow.get("binding", "")), hash_token(binding) if binding else "-"):
        row.status = ConnectorStatus.NEEDS_REAUTH if row.tokens_enc else ConnectorStatus.PENDING_AUTH
        row.last_error = "the sign-in finished in a different browser than the one that started it"
        await session.commit()
        return CallbackOutcome(
            False,
            "This sign-in was started in another browser. Connect again from Prismoid's Connectors page, "
            "in the browser where you are signed in.",
            row.name,
            row.slug,
        )

    if error or not code:
        row.status = ConnectorStatus.NEEDS_REAUTH if row.tokens_enc else ConnectorStatus.PENDING_AUTH
        said = f"{error or 'authorization refused'}: {detail}"
        row.last_error = sanitize_server_text(said, 500).rstrip(": ")
        await session.commit()
        return CallbackOutcome(False, "The provider did not authorize the connection.", row.name, row.slug)

    return None


def _store_tokens(row: Connector, tokens: OAuthToken) -> None:
    row.tokens_enc = cipher.encrypt(tokens.model_dump_json(exclude_none=True))
    row.key_id = cipher.key_id()
    row.token_expires_at = (
        utcnow() + dt.timedelta(seconds=int(tokens.expires_in)) if tokens.expires_in else None
    )


# ── refresh ───────────────────────────────────────────────────────────────────────────────────


async def refresh_if_stale(session: AsyncSession, row: Connector) -> None:
    """Renew tokens expiring inside `REFRESH_HORIZON`. Commit-before-HTTP, and rotation-safe:
    OAuth 2.1 servers rotate refresh tokens, so after the network we re-read the row - if a
    parallel step already wrote a fresher token, theirs wins and ours is discarded (last-write
    -wins would clobber the only refresh token that still works)."""
    if row.auth_kind != AuthKind.OAUTH or not row.tokens_enc or not row.flow_enc:
        return
    if row.token_expires_at is None or row.token_expires_at > utcnow() + REFRESH_HORIZON:
        return

    tokens = json.loads(cipher.decrypt(row.tokens_enc))
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        previous = row.status
        row.status = ConnectorStatus.NEEDS_REAUTH
        row.last_error = "access token expired and the server issued no refresh token"
        await knock_broken(session, row, previous, "Its access token expired and cannot be renewed.")
        await session.commit()
        return

    flow = json.loads(cipher.decrypt(row.flow_enc))
    info = OAuthClientInformationFull.model_validate_json(cipher.decrypt(row.client_info_enc or ""))
    data, headers = refresh_request(
        refresh_token=str(refresh_token),
        client_id=info.client_id or "",
        client_secret=info.client_secret,
        auth_method=str(flow.get("auth_method", "client_secret_post")),
        resource=flow.get("resource"),
    )
    before = row.token_expires_at
    await session.commit()

    async with http_client_factory() as client:
        try:
            answer = await client.post(str(flow["token_endpoint"]), data=data, headers=headers)
        except httpx.HTTPError as bad:
            logfire.warn("connector {slug} refresh unreachable: {err}", slug=row.slug, err=str(bad))
            return  # transient - the mount probe decides what it means

    await session.refresh(row)
    if row.token_expires_at != before:
        return  # a parallel step refreshed first; theirs is the live rotation

    if answer.status_code != 200:
        previous = row.status
        row.status = ConnectorStatus.NEEDS_REAUTH
        row.last_error = f"token refresh refused ({answer.status_code})"
        await knock_broken(session, row, previous, "The provider refused to renew its authorization.")
        await session.commit()
        return

    fresh = OAuthToken.model_validate(answer.json())
    if fresh.refresh_token is None and refresh_token:
        # A server that rotates nothing keeps the old refresh token alive - carry it forward.
        fresh = fresh.model_copy(update={"refresh_token": str(refresh_token)})
    _store_tokens(row, fresh)
    await session.commit()
