# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - workspaces (ADR-061). Every route `AdminDep`."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_workspaces import AdminWorkspaceOut, admin_workspace_out
from prismoid.api.serializers.common import PageOut
from prismoid.db.base import keyset_cursor
from prismoid.db.models.workspace import Workspace
from prismoid.errors import NotFound


async def list_workspaces(page: PageDep, actor: AdminDep, session: SessionDep) -> PageOut[AdminWorkspaceOut]:
    rows = await Workspace.admin_page(session, limit=page.limit, cursor=page.cursor)
    next_cursor = keyset_cursor(rows[-1][0]) if len(rows) == page.limit else None
    return PageOut(items=[admin_workspace_out(o, n) for o, n in rows], next_cursor=next_cursor)


async def get_workspace(workspace_id: UUID, actor: AdminDep, session: SessionDep) -> AdminWorkspaceOut:
    workspace = await Workspace.get(session, workspace_id)
    if workspace is None:
        raise NotFound("no such workspace")
    return admin_workspace_out(workspace, await Workspace.member_count(session, workspace_id))
