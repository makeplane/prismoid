# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the audit trail's shapes (admin surface). `*Out` = response.

The row answers WHO / DID WHAT / TO WHAT. The `admin` block is a user shape;
`detail` is the small facts the operation chose to keep - reasons and counts, never secrets.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from prismoid.db.models.admin_action import AdminAction
from prismoid.db.models.user import User


class AuditAdminOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    name: str


class AdminActionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    admin: AuditAdminOut | None  # None only if the admin's User row was erased
    action: str
    subject_type: str
    subject_id: str
    detail: dict[str, Any]
    at: dt.datetime


def admin_action_out(row: AdminAction, users: dict[UUID, User]) -> AdminActionOut:
    user = users.get(row.admin_actor_id)
    return AdminActionOut(
        id=row.id,
        admin=None if user is None else AuditAdminOut(id=user.actor_id, email=user.email, name=user.name),
        action=row.action,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        detail=row.detail,
        at=row.created_at,
    )
