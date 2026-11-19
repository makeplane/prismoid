# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The only three things a view may depend on, as Annotated aliases.

This is the modern FastAPI idiom - the one the official template uses - and it buys class-based
views' deduplication with none of their hidden state. A raw `Depends(...)` in a view signature is
a rule violation and `just audit` greps for it: the point of the aliases is that there is exactly
one way to get an Actor, one way to get a session, and one way to paginate, so those three things
can be changed in one place.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.session import AuthSession
from prismoid.db.session import get_session
from prismoid.errors import BadRequest, Forbidden, PayloadTooLarge, Unauthorized, ValidationFailed
from prismoid.operations import stream_tickets
from prismoid.operations.throttle import FixedWindowLimiter
from prismoid.schemas.ids import (
    API_KEY_PREFIX,
    FIRE_TOKEN_PREFIX,
    OAUTH_BINDING_COOKIE,
    OAUTH_BINDING_PREFIX,
    SESSION_TOKEN_PREFIX,
    new_oauth_binding,
)
from prismoid.schemas.principal import Credential, CredKind, Principal
from prismoid.schemas.stream_tickets import StreamSubject

_BEARER = "bearer "

# The per-actor backstop: the shared fixed-window limiter (operations/throttle - one
# implementation for every burst door), which carries the replica
# division and the amortised stale-key eviction this door pioneered.
_actor_limiter = FixedWindowLimiter("requests")


def throttle_actor(actor_id: uuid.UUID) -> None:
    _actor_limiter.hit(actor_id, per_minute=settings.api.per_actor_rate_limit_per_minute)


def bearer_header(request: HTTPConnection) -> str | None:
    """The `Authorization: Bearer <token>` credential (case-insensitive), or None.

    One extraction, shared by `require_principal` and `auth.logout` - so logout revokes exactly the
    token that authenticated the request instead of blind-slicing seven characters off a header that
    might be absent, lowercase, or a different scheme entirely."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith(_BEARER):
        return header[len(_BEARER) :].strip() or None
    return None


async def authenticate(session: AsyncSession, raw: str) -> Principal[Actor]:
    """The credential → a bound unit of work: resolve, stand, bind. The ONE
    body shared by the door and the live streams' re-proof, so a stream re-proves exactly what
    the door proved - the credential, the account's standing, AND (through `require_workspace`) the
    workspace membership - and a removed member's open stream severs within one interval.

    A person with no active membership left resolves with no workspace and nothing bound: the door
    lets them back in only through `OrglessActorDep`, and everything else asks `require_workspace`.

    Dispatch is by **prefix**, not by trying both: `ck_` is an API key, `cs_` is a session,
    anything else is a 401 without touching the database."""
    if raw.startswith(API_KEY_PREFIX):
        principal = await ApiKey.resolve(session, raw)
    elif raw.startswith(SESSION_TOKEN_PREFIX):
        principal = await AuthSession.resolve(session, raw)
    else:
        raise Unauthorized("unrecognised credential")

    if principal is None:
        raise Unauthorized("invalid or expired credentials")
    # The kill switch, enforced at the ONE door every credential passes (admin surface, D4):
    # a suspended actor's sessions and keys are revoked at suspend time, but a credential that
    # raced the revoke - or a stream re-proving itself (the 10 s stream reauth) - lands here,
    # and here is where it dies. One check, every route, including the SSE re-proof.
    if not principal.actor.is_active():
        raise Forbidden("account suspended")
    if principal.workspace_id is not None:
        tenant.bind(session, principal.workspace_id, principal.role)
        if principal.api_key_id is not None:
            tenant.pin(session, principal.workspace_id)  # a key never acts for another workspace
    return principal


def require_workspace(principal: Principal[Actor]) -> Principal[Actor]:
    """Acting in a workspace: 403 once every membership is gone or suspended.
    Every door asks it but the way back in (`OrglessActorDep`), and so does every stream."""
    if principal.workspace_id is None:
        raise Forbidden("no active workspace")
    return principal


async def require_principal(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Principal[Actor]:
    """Every route requires an Actor except `/healthz`, `/v1/auth/login`, `/v1/auth/signup`,
    `/v1/auth/config` (the deployment's public face, read before anyone signs in: ADR-086), the
    password-reset pair (`forgot-password`/`reset-password`, whose authentication is the
    emailed single-use `cr_` code), and the connectors OAuth callback.

    **It binds the workspace** when there is one: the request's session acts for the
    credential's workspace - a login session's active workspace, an API key's own workspace - with the caller's role
    there, so every tenant read downstream asks `tenant.workspace_of` and gets it. Views never see the
    principal: they take `ActorDep` (a workspace required) or, on the way back in, `OrglessActorDep`.

    The credential rides the `Authorization` header and nowhere else: a token in a URL lands in
    access logs, proxies and browser history. A browser's EventSource, which cannot set a header,
    opens its stream with a single-use ticket instead (`task_stream_principal`).
    """
    raw = bearer_header(request)
    if not raw:
        raise Unauthorized("no credentials")

    principal = await authenticate(session, raw)
    throttle_actor(principal.actor.id)  # the per-actor backstop, after the credential resolves
    return principal


async def require_actor(principal: Annotated[Principal[Actor], Depends(require_principal)]) -> Actor:
    """`ActorDep`: the caller, acting in a workspace."""
    return require_workspace(principal).actor


async def require_orgless_actor(principal: Annotated[Principal[Actor], Depends(require_principal)]) -> Actor:
    """`OrglessActorDep`: the caller, with or without a workspace - `me` answers `workspace: null`
    for a person whose every membership is gone. The way back in (list, create and join workspaces,
    log out) takes its session-only twin, `OrglessSessionActorDep`. Nothing behind either reads a
    tenant row."""
    return principal.actor


def _signed_in(principal: Principal[Actor]) -> Principal[Actor]:
    """A login session, never an API key. A key acts in its workspace on a person's behalf; minting,
    listing or revoking keys, editing the account, logging out and the workspace doors are the
    person's own acts, and a key that could do them could make a leak permanent or reach past the
    workspace it is pinned to."""
    if principal.api_key_id is not None:
        raise Forbidden("this needs a login session, not an API key")
    return principal


async def require_session_actor(
    actor: Annotated[Actor, Depends(require_actor)],
    principal: Annotated[Principal[Actor], Depends(require_principal)],
) -> Actor:
    """`SessionActorDep`: `ActorDep` (the workspace door included), signed in with a login session
    (`_signed_in`)."""
    _signed_in(principal)
    return actor


async def require_orgless_session_actor(
    principal: Annotated[Principal[Actor], Depends(require_principal)],
) -> Actor:
    """`OrglessSessionActorDep`: `OrglessActorDep`, signed in with a login session (`_signed_in`)."""
    return _signed_in(principal).actor


async def require_admin(
    actor: Annotated[Actor, Depends(require_actor)],
    principal: Annotated[Principal[Actor], Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Actor:
    """`/admin/*` - the operator's cross-workspace console, so the unit of work is PLATFORM:
    tenant reads raise, admin queries enumerate across workspaces by design. An API key never opens
    it: a key acts only in
    the workspace it was minted in, an admin's too, so the console needs a login session."""
    if not actor.has_scope(Scope.ADMIN):
        raise Forbidden("admin scope required")
    if principal.api_key_id is not None:
        raise Forbidden("the admin console needs a login session, not an API key")
    tenant.bind_platform(session)
    return actor


async def auth_session_id(principal: Annotated[Principal[Actor], Depends(require_principal)]) -> uuid.UUID:
    """The login session that authenticated this request - the workspace switch's row,
    so it acts in a workspace like `ActorDep`. An API key belongs to one workspace and has no session to
    switch: a 400."""
    sid = require_workspace(principal).auth_session_id
    if sid is None:
        raise BadRequest("switching needs a login session")
    return sid


async def login_session_id(principal: Annotated[Principal[Actor], Depends(require_principal)]) -> uuid.UUID:
    """The login session that authenticated this request - a push device's owner (ADR-100): a phone
    is the person's, not a workspace's, and ends with its session. An API key has no session for it
    to end with: the 403 `SessionActorDep` answers."""
    sid = _signed_in(principal).auth_session_id
    if sid is None:
        raise Forbidden("this needs a login session, not an API key")
    return sid


async def auth_session_id_or_none(
    principal: Annotated[Principal[Actor], Depends(require_principal)],
) -> uuid.UUID | None:
    """The login session behind this request, or None for an API key - the invite accept, which
    points a session at the new workspace when there is one and otherwise just joins."""
    return principal.auth_session_id


async def credential(principal: Annotated[Principal[Actor], Depends(require_principal)]) -> Credential:
    """The credential row this request authenticated with - its id and its table, never the token,
    so it acts in a workspace like `ActorDep`. A screen ticket names it, and the screen
    socket's proof re-reads that row by id (`AuthSession.live_principal`, `ApiKey.live_principal`)
    on every re-proof."""
    return credential_of(require_workspace(principal))


def credential_of(principal: Principal[Actor]) -> Credential:
    """The credential row a resolved principal came from: what a ticket names and a stream re-proves."""
    if principal.auth_session_id is not None:
        return Credential(id=principal.auth_session_id, kind=CredKind.SESSION)
    if principal.api_key_id is not None:
        return Credential(id=principal.api_key_id, kind=CredKind.API_KEY)
    raise Unauthorized("no credentials")  # unreachable: authenticate resolved one of the two or raised


async def _stream_principal(
    request: Request, session: AsyncSession, subject: StreamSubject, subject_id: uuid.UUID
) -> Principal[Actor]:
    """A stream's door: the `Authorization` header when the client can set one (the CLI, curl, an
    API key), else the `?ticket=` a browser minted for exactly this stream. The ticket is read from
    the query here, never declared as a parameter, so it is no endpoint argument a span could
    record; the access log and the span redact `ticket=` (prismoid/observability.py)."""
    ticket = request.query_params.get("ticket")
    if bearer_header(request) is None and ticket:
        principal = await stream_tickets.admit(session, ticket, subject, subject_id)
        throttle_actor(principal.actor.id)
        return principal
    return require_workspace(await require_principal(request, session))


async def task_stream_principal(
    task_id: uuid.UUID, request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Principal[Actor]:
    """`TaskStreamDep`: who opened `GET /v1/tasks/{id}/events`, acting in a workspace."""
    return await _stream_principal(request, session, StreamSubject.TASK, task_id)


async def conversation_stream_principal(
    conversation_id: uuid.UUID, request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Principal[Actor]:
    """`ConversationStreamDep`: who opened `GET /v1/conversations/{id}/events`, acting in a workspace."""
    return await _stream_principal(request, session, StreamSubject.CONVERSATION, conversation_id)


# The binding cookie lives as long as one consent screen may take, and is sent only on the
# connector routes: the starts read it back, the callback checks it.
_OAUTH_BINDING_MAX_AGE_S = 600
_OAUTH_BINDING_PATH = "/v1/connectors"


async def oauth_binding(request: Request, response: Response) -> str:
    """`OAuthBindingDep`: the browser that starts a connector OAuth flow. The `cn_` state
    alone is shareable - a victim who opens someone else's consent link would land THEIR tokens on
    that person's connector - so a start also sets an HttpOnly, SameSite=Lax cookie, the flow
    keeps its hash, and the callback refuses any browser without it. This browser's live binding is
    reused, so two connects started side by side both finish. Secure whenever the callback is https.
    The web's start calls send `credentials: "include"` so a cross-origin answer may set it."""
    held = request.cookies.get(OAUTH_BINDING_COOKIE, "")
    binding = held if held.startswith(OAUTH_BINDING_PREFIX) and len(held) > 40 else new_oauth_binding()
    response.set_cookie(
        OAUTH_BINDING_COOKIE,
        binding,
        max_age=_OAUTH_BINDING_MAX_AGE_S,
        path=_OAUTH_BINDING_PATH,
        httponly=True,
        secure=urlsplit(settings.api.public_base_url).scheme == "https",
        samesite="lax",
    )
    return binding


class FireAuth(BaseModel):
    """Who knocked on a routine's fire door - exactly one of the two is set.

    `POST /v1/routines/{id}/fire` is the fourth route-level auth exception (the cr_/cn_
    family): its Bearer may be the routine's own single-purpose `rt_` token instead of an
    Actor credential. The dep DISPATCHES; the operation VERIFIES the token against the one
    routine the path names - a fire token never resolves to an Actor, so it can start runs
    and nothing else. Every other prefix walks the normal `require_actor` door, kill switch
    and throttle included."""

    model_config = {"arbitrary_types_allowed": True}

    actor: Actor | None = None
    fire_token: str | None = None


async def fire_auth(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> FireAuth:
    raw = bearer_header(request)
    if raw and raw.startswith(FIRE_TOKEN_PREFIX):
        return FireAuth(fire_token=raw)
    return FireAuth(actor=require_workspace(await require_principal(request, session)).actor)


# The {"data": ...} envelope's allowance over the payload cap itself.
_FIRE_ENVELOPE_SLACK = 4096


async def fire_payload(request: Request) -> bytes | None:
    """The fire door's body, read STREAMING under the cap. This is the one
    endpoint built for anonymous machine traffic, so buffering is bounded BEFORE any parse -
    a declared FireIn model had FastAPI read and json-decode an arbitrarily large body just to
    refuse it after. Returns the `data` field re-serialized (exactly what
    the gate stores as trigger.json), None for an empty or data-less body; 413 past the cap,
    422 for non-JSON."""
    import orjson

    cap = settings.routines.fire_payload_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + _FIRE_ENVELOPE_SLACK:
        raise PayloadTooLarge(f"fire payload is capped at {cap} bytes - got {declared}")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap + _FIRE_ENVELOPE_SLACK:
            raise PayloadTooLarge(f"fire payload is capped at {cap} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return None
    try:
        body = orjson.loads(raw)
    except orjson.JSONDecodeError:
        raise ValidationFailed("the fire body must be JSON", {"body": 'send {} or {"data": ...}'}) from None
    if not isinstance(body, dict) or body.get("data") is None:
        return None
    payload = orjson.dumps(body["data"])
    if len(payload) > cap:
        raise PayloadTooLarge(f"fire payload is capped at {cap} bytes - got {len(payload)}")
    return payload


class Page(BaseModel):
    """Keyset pagination, and only keyset. An OFFSET of 10,000 reads 10,000 rows to throw
    them away; a cursor reads the page. Every list endpoint returns `next_cursor`."""

    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _cursor_parses(cls, v: str | None) -> str | None:
        """A cursor is opaque to clients but must still PARSE: `keyset_before` (db/base.py) splits it
        into a `(created_at, id)` pair, and a hand-edited `?cursor=` reached that split as a raw
        `ValueError` deep in a fat model → HTTP 500. Validate the shape here, at the door, so a bad
        cursor is the 422 it always should have been - mirrored on the one producer's format."""
        if v is None:
            return v
        at_str, sep, id_str = v.partition("|")
        try:
            if not sep:
                raise ValueError("missing separator")
            dt.datetime.fromisoformat(at_str)
            uuid.UUID(id_str)
        except ValueError:
            raise ValidationFailed("malformed cursor", {"cursor": "not a valid page cursor"}) from None
        return v


ActorDep = Annotated[Actor, Depends(require_actor)]
OAuthBindingDep = Annotated[str, Depends(oauth_binding)]
OAuthReturnBinding = Annotated[str, Cookie(alias=OAUTH_BINDING_COOKIE, include_in_schema=False)]
OrglessActorDep = Annotated[Actor, Depends(require_orgless_actor)]  # the way back in ONLY
SessionActorDep = Annotated[Actor, Depends(require_session_actor)]  # keys and the account: no API key
OrglessSessionActorDep = Annotated[Actor, Depends(require_orgless_session_actor)]  # workspace doors, logout
AdminDep = Annotated[Actor, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
PageDep = Annotated[Page, Depends()]
AuthSessionIdDep = Annotated[uuid.UUID, Depends(auth_session_id)]  # the workspace switch ONLY
LoginSessionIdDep = Annotated[uuid.UUID, Depends(login_session_id)]  # device registration ONLY
MaybeAuthSessionIdDep = Annotated[uuid.UUID | None, Depends(auth_session_id_or_none)]  # invite accept ONLY
CredentialDep = Annotated[Credential, Depends(credential)]  # the screen and stream tickets' mints ONLY
TaskStreamDep = Annotated[Principal[Actor], Depends(task_stream_principal)]  # the task SSE stream ONLY
ConversationStreamDep = Annotated[Principal[Actor], Depends(conversation_stream_principal)]  # its twin
FireAuthDep = Annotated[FireAuth, Depends(fire_auth)]  # the routine fire door ONLY
FirePayloadDep = Annotated[bytes | None, Depends(fire_payload)]  # its capped, streamed body
