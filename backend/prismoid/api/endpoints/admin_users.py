# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the user population: observe, suspend, grant, revoke (admin surface).

Every route is `AdminDep`; every mutation writes an `admin_actions` row in the same transaction
(operations/admin_ops - law 2). Suspension is enforced at `require_actor`, so a flipped switch
reaches every credential and every open stream within one re-proof interval (D4).
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_users import (
    AdminUserDetailOut,
    AdminUserOut,
    RevokeSessionsOut,
    SuspendIn,
    UserApiKeyOut,
    UserSessionOut,
    admin_user_detail_out,
    admin_user_out,
)
from prismoid.api.serializers.common import OkOut, PageOut
from prismoid.errors import NotFound
from prismoid.operations import admin_ops


async def list_users(
    page: PageDep,
    actor: AdminDep,
    session: SessionDep,
    q: str | None = None,
    status: str | None = None,
) -> PageOut[AdminUserOut]:
    rows = await admin_ops.list_users(session, q=q, status=status, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1][0].admin_cursor() if len(rows) == page.limit else None
    return PageOut(items=[admin_user_out(u, a) for u, a in rows], next_cursor=next_cursor)


async def get_user(id: UUID, actor: AdminDep, session: SessionDep) -> AdminUserDetailOut:
    user, target, facts = await admin_ops.get_user_admin(session, id)
    return admin_user_detail_out(user, target, facts)


async def suspend_user(id: UUID, body: SuspendIn, actor: AdminDep, session: SessionDep) -> AdminUserOut:
    return admin_user_out(*await admin_ops.suspend_user(session, actor, id, reason=body.reason))


async def unsuspend_user(id: UUID, actor: AdminDep, session: SessionDep) -> AdminUserOut:
    return admin_user_out(*await admin_ops.unsuspend_user(session, actor, id))


async def grant_admin(id: UUID, actor: AdminDep, session: SessionDep) -> AdminUserOut:
    return admin_user_out(*await admin_ops.grant_admin(session, actor, id))


async def revoke_admin(id: UUID, actor: AdminDep, session: SessionDep) -> AdminUserOut:
    return admin_user_out(*await admin_ops.revoke_admin(session, actor, id))


async def revoke_user_sessions(id: UUID, actor: AdminDep, session: SessionDep) -> RevokeSessionsOut:
    result = await admin_ops.revoke_sessions(session, actor, id)
    return RevokeSessionsOut(**result)


async def list_user_sessions(id: UUID, actor: AdminDep, session: SessionDep) -> list[UserSessionOut]:
    rows = await admin_ops.list_user_sessions(session, id)
    return [
        UserSessionOut(
            id=r.id,
            created_at=r.created_at,
            expires_at=r.expires_at,
            revoked_at=r.revoked_at,
            user_agent=r.user_agent,
            live=r.is_live(),
        )
        for r in rows
    ]


async def list_user_keys(id: UUID, actor: AdminDep, session: SessionDep) -> list[UserApiKeyOut]:
    rows = await admin_ops.list_user_keys(session, id)
    return [
        UserApiKeyOut(
            id=r.id,
            name=r.name,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


async def revoke_user_session(id: UUID, session_id: UUID, actor: AdminDep, session: SessionDep) -> OkOut:
    if not await admin_ops.revoke_user_session(session, actor, id, session_id):
        raise NotFound("no such live session")
    return OkOut(ok=True)


async def revoke_user_key(id: UUID, key_id: UUID, actor: AdminDep, session: SessionDep) -> OkOut:
    if not await admin_ops.revoke_user_key(session, actor, id, key_id):
        raise NotFound("no such live key")
    return OkOut(ok=True)
