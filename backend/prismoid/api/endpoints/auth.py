# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Auth - signup, login, forgotten passwords, me, API keys.

Thin: auth → validate → one operation → serialize. ≤20 lines each, no business logic,
no transaction. `signup`, `login`, `forgot_password`, and `reset_password` take no Actor -
Rule 14's exceptions here, because a route that demands credentials in order to obtain
credentials is a locked door with the key inside (the reset pair's authentication is the
emailed single-use `cr_` code).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from prismoid.api.deps import (
    ActorDep,
    AuthSessionIdDep,
    OrglessActorDep,
    OrglessSessionActorDep,
    SessionActorDep,
    SessionDep,
    bearer_header,
)
from prismoid.api.serializers.auth import (
    ApiKeyCreatedOut,
    ApiKeyCreateIn,
    ApiKeyOut,
    AuthConfigOut,
    ForgotPasswordIn,
    LoginIn,
    MeOut,
    ResetPasswordIn,
    SessionOut,
    SessionWorkspaceIn,
    SignupIn,
    UserOut,
    UserUpdateIn,
    VerifyEmailIn,
    api_key_created_out,
    api_key_out,
    auth_config_out,
    me_out,
    user_out,
)
from prismoid.api.serializers.common import OkOut
from prismoid.db.models.actor import Scope
from prismoid.operations import auth as ops
from prismoid.operations import workspaces as workspaces_ops


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")


async def auth_config() -> AuthConfigOut:
    """Takes no Actor: the signup page reads it before anyone has credentials (ADR-086). It
    describes the deployment, never an account, so there is nothing to protect."""
    return auth_config_out(ops.public_config())


async def signup(body: SignupIn, request: Request, session: SessionDep) -> SessionOut:
    result = await ops.signup(
        session,
        email=body.email,
        name=body.name,
        password=body.password,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        invite_code=body.invite_code,
    )
    return SessionOut(token=result.token, expires_at=result.expires_at, user=user_out(result.user))


async def login(body: LoginIn, request: Request, session: SessionDep) -> SessionOut:
    result = await ops.login(
        session,
        email=body.email,
        password=body.password,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return SessionOut(token=result.token, expires_at=result.expires_at, user=user_out(result.user))


async def forgot_password(body: ForgotPasswordIn, request: Request, session: SessionDep) -> OkOut:
    """202 whether or not the email exists - an acknowledgement, never an oracle."""
    await ops.request_password_reset(session, email=body.email, ip=_client_ip(request))
    return OkOut(ok=True)


async def reset_password(body: ResetPasswordIn, request: Request, session: SessionDep) -> OkOut:
    await ops.reset_password(session, code=body.code, new_password=body.new_password, ip=_client_ip(request))
    return OkOut(ok=True)


async def logout(request: Request, actor: OrglessSessionActorDep, session: SessionDep) -> OkOut:
    # Resolve the token the way the door did (case-insensitive Bearer), not a blind 7-char slice
    # that mangles a lowercase or absent header - logout must revoke exactly what authenticated us.
    await ops.logout(session, bearer_header(request) or "")
    return OkOut(ok=True)


async def me(actor: OrglessActorDep, session: SessionDep) -> MeOut:
    return me_out(*await ops.whoami(session, actor), is_admin=actor.has_scope(Scope.ADMIN))


async def switch_session_workspace(
    body: SessionWorkspaceIn, actor: ActorDep, session: SessionDep, auth_session_id: AuthSessionIdDep
) -> MeOut:
    """Move this login session to another of the caller's workspaces - answers `me`, as seen
    from there. An API key has no session to switch: a 400."""
    await workspaces_ops.switch_workspace(session, actor, auth_session_id, body.workspace_id)
    return me_out(*await ops.whoami(session, actor), is_admin=actor.has_scope(Scope.ADMIN))


async def verify_email(body: VerifyEmailIn, actor: SessionActorDep, session: SessionDep) -> MeOut:
    await ops.verify_email(session, actor, body.code)
    return me_out(*await ops.whoami(session, actor), is_admin=actor.has_scope(Scope.ADMIN))


async def resend_verification(request: Request, actor: SessionActorDep, session: SessionDep) -> OkOut:
    await ops.resend_verification(session, actor, ip=_client_ip(request))
    return OkOut(ok=True)


async def update_me(body: UserUpdateIn, actor: SessionActorDep, session: SessionDep) -> UserOut:
    return user_out(await ops.update_me(session, actor, name=body.name, timezone=body.timezone))


async def create_api_key(
    body: ApiKeyCreateIn, actor: SessionActorDep, session: SessionDep
) -> ApiKeyCreatedOut:
    key, plaintext = await ops.create_api_key(session, actor, body.name)
    return api_key_created_out(key, plaintext)


async def list_api_keys(actor: SessionActorDep, session: SessionDep) -> list[ApiKeyOut]:
    return [api_key_out(k) for k in await ops.list_api_keys(session, actor)]


async def revoke_api_key(key_id: UUID, actor: SessionActorDep, session: SessionDep) -> OkOut:
    await ops.revoke_api_key(session, actor, key_id)
    return OkOut(ok=True)
