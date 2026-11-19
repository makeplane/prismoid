# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the project population, observed (admin surface). Read-only: a project's settings
belong to its owner; the operator sees workspace, size, and money against budget."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_projects import (
    AdminProjectDetailOut,
    AdminProjectOut,
    admin_project_detail_out,
    admin_project_out,
)
from prismoid.api.serializers.common import PageOut
from prismoid.operations import admin_ops


async def list_projects(
    page: PageDep, actor: AdminDep, session: SessionDep, q: str | None = None
) -> PageOut[AdminProjectOut]:
    rows = await admin_ops.list_projects_admin(session, q=q, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1]["project"].cursor() if len(rows) == page.limit else None
    return PageOut(items=[admin_project_out(r) for r in rows], next_cursor=next_cursor)


async def get_project(project_id: UUID, actor: AdminDep, session: SessionDep) -> AdminProjectDetailOut:
    return admin_project_detail_out(await admin_ops.get_project_admin(session, project_id))
