# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the project population's shapes (admin surface). `*Out` = response, read-only.

Observation, not administration: a project's settings belong to its owner, so the operator view
carries facts (owner, size, budget vs month-to-date spend) and no verbs. MTD comes from the
rollup, never a ledger scan.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AdminProjectOwnerOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    name: str


class AdminProjectOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    owner: AdminProjectOwnerOut | None  # None = an erased owner profile
    members: int
    tasks: int
    budget_usd: Decimal | None
    mtd_usd: Decimal
    created_at: dt.datetime


class AdminProjectMemberOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    name: str
    role: str  # owner | editor | viewer
    added_at: dt.datetime | None  # None on the owner row (ownership is a column, not a membership)


class AdminProjectDetailOut(AdminProjectOut):
    member_list: list[AdminProjectMemberOut]


def admin_project_out(row: dict[str, Any]) -> AdminProjectOut:
    project, owner = row["project"], row["owner"]
    return AdminProjectOut(
        id=project.id,
        name=project.name,
        owner=None
        if owner is None
        else AdminProjectOwnerOut(id=owner.actor_id, email=owner.email, name=owner.name),
        members=row["members"],
        tasks=row["tasks"],
        budget_usd=project.budget_usd,
        mtd_usd=row["mtd_usd"],
        created_at=project.created_at,
    )


def admin_project_detail_out(row: dict[str, Any]) -> AdminProjectDetailOut:
    base = admin_project_out(row)
    project, owner = row["project"], row["owner"]
    users = row["member_users"]
    members: list[AdminProjectMemberOut] = []
    if owner is not None:  # the owner leads the list - a column, not a membership row
        members.append(
            AdminProjectMemberOut(
                id=owner.actor_id, email=owner.email, name=owner.name, role="owner", added_at=None
            )
        )
    for m in row["member_rows"]:
        u = users.get(m.actor_id)
        if u is None or m.actor_id == project.owner_actor_id:
            continue
        members.append(
            AdminProjectMemberOut(id=u.actor_id, email=u.email, name=u.name, role=m.role, added_at=m.added_at)
        )
    return AdminProjectDetailOut(**base.model_dump(), member_list=members)
