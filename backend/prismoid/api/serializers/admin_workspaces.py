# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the workspace population's shapes (ADR-061). `*In` = request, `*Out` = response."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from prismoid.db.models.workspace import Workspace


class AdminWorkspaceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    slug: str
    kind: str
    status: str
    members: int
    created_at: dt.datetime


def admin_workspace_out(workspace: Workspace, members: int) -> AdminWorkspaceOut:
    return AdminWorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        kind=workspace.kind,
        status=workspace.status,
        members=members,
        created_at=workspace.created_at,
    )
