# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the user population's shapes (admin surface). `*In` = request, `*Out` = response.

The boundary vocabulary holds even here: these are USER shapes - the operator manages
people, and the word "actor" stays inside event/audit payloads. What is deliberately absent:
token hashes, credential material, preferences (the operator has no business in them). `is_admin`
appears because the operator is deciding who else holds the keys; `status` because suspension is
the point.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.user import User


class AdminUserOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    name: str
    status: str  # active | suspended
    is_admin: bool
    created_at: dt.datetime
    deleted: bool  # GDPR tombstone - listed honestly, acted on never


class AdminUserDetailOut(AdminUserOut):
    """The one-user view: the list row plus the operational facts worth a second query."""

    live_sessions: int
    live_api_keys: int
    task_counts: dict[str, int]
    spend_total_usd: Decimal
    spend_mtd_usd: Decimal
    # The newest of (a session's birth, an API key's throttled last-use bump) - honest, and
    # labelled "last seen" rather than pretending to per-request telemetry.
    last_seen_at: dt.datetime | None


class UserSessionOut(BaseModel):
    """One login, as metadata. Note what is absent: the token, in any form."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    created_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None
    user_agent: str
    live: bool


class UserApiKeyOut(BaseModel):
    """One key, as metadata - the auth ApiKeyOut's shape, through the operator door."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None


class SuspendIn(BaseModel):
    """Suspension takes a reason - it lands in the audit row, not the void."""

    reason: str = Field(min_length=1, max_length=500)


class RevokeSessionsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions_revoked: int
    api_keys_revoked: int


def admin_user_out(user: User, actor: Actor) -> AdminUserOut:
    return AdminUserOut(
        id=user.actor_id,
        email=user.email,
        name=user.name,
        status=actor.status,
        is_admin=actor.has_scope(Scope.ADMIN),
        created_at=user.created_at,
        deleted=user.deleted_at is not None,
    )


def admin_user_detail_out(user: User, actor: Actor, facts: dict[str, Any]) -> AdminUserDetailOut:
    base = admin_user_out(user, actor)
    return AdminUserDetailOut(
        **base.model_dump(),
        live_sessions=facts["live_sessions"],
        live_api_keys=facts["live_api_keys"],
        task_counts=facts["task_counts"],
        spend_total_usd=Decimal(facts["spend_total_usd"]),
        spend_mtd_usd=Decimal(facts["spend_mtd_usd"]),
        last_seen_at=facts["last_seen_at"],
    )
