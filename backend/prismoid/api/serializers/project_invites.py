# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project join links (ADR-062) - request and response shapes."""

import datetime as dt
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.base import utcnow
from prismoid.db.models.project import Project
from prismoid.db.models.project_invite import InviteRole, InviteState, ProjectInvite

type ProjectRoleName = Literal["owner", "editor", "viewer"]


# Every link is single-use, so the caller picks no use count; an older
# client's `max_uses` is ignored like any unknown field. Expiry is always set: None means the
# server's default (`project_invite_ttl_days`).
class ProjectInviteIn(BaseModel):
    role: InviteRole = "editor"
    expires_in_days: int | None = Field(default=None, ge=1, le=30)


class ProjectInviteOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    role: InviteRole
    uses: int  # 0 or 1: a link lets one person in, then reads `used_up`
    expires_at: dt.datetime
    created_at: dt.datetime
    state: InviteState


class ProjectInviteCreatedOut(ProjectInviteOut):
    code: str  # shown ONCE - the link is {web}/join/{code}; never stored or logged


class ProjectInviteAcceptIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class ProjectInviteAcceptedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: UUID
    workspace_id: UUID
    role: ProjectRoleName  # "owner" when the owner redeems their own link


def project_invite_out(row: ProjectInvite) -> ProjectInviteOut:
    return ProjectInviteOut(
        id=row.id,
        role=cast(InviteRole, row.role),  # a String column; pydantic re-checks the Literal at runtime
        uses=row.uses,
        expires_at=row.expires_at,
        created_at=row.created_at,
        state=row.state(utcnow()),
    )


def project_invite_accepted_out(project: Project, role: str) -> ProjectInviteAcceptedOut:
    # `role` is a String column's value; the cast is for the checker, pydantic re-checks at runtime
    return ProjectInviteAcceptedOut(
        project_id=project.id, workspace_id=project.workspace_id, role=cast(ProjectRoleName, role)
    )
