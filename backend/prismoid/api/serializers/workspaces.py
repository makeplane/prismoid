# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every workspace shape, one file (ADR-061). `*In` = request, `*Out` = response.

The caller's standing rides every workspace it sees: `role` is THEIR role in that workspace, which is what
the web needs to decide what to draw (an owner's settings, a member's roster)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from prismoid.db.models.workspace_invite import WorkspaceInvite


class WorkspaceBriefOut(BaseModel):
    """A workspace as its member sees it in a list or on `/v1/auth/me`."""

    id: UUID
    name: str
    slug: str
    kind: str
    role: str


def workspace_brief_out(workspace: Workspace, role: str) -> WorkspaceBriefOut:
    return WorkspaceBriefOut(
        id=workspace.id, name=workspace.name, slug=workspace.slug, kind=workspace.kind, role=role
    )


class WorkspaceOut(WorkspaceBriefOut):
    """One workspace's detail."""


def workspace_out(workspace: Workspace, role: WorkspaceRole) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        kind=workspace.kind,
        role=role.value,
    )


class UserBriefOut(BaseModel):
    id: UUID
    email: str
    name: str


class WorkspaceMemberOut(BaseModel):
    user: UserBriefOut
    role: str
    joined_at: dt.datetime


def workspace_member_out(member: WorkspaceMember, user: User) -> WorkspaceMemberOut:
    brief = UserBriefOut(id=user.actor_id, email=user.email, name=user.name)
    return WorkspaceMemberOut(user=brief, role=member.role, joined_at=member.created_at)


class WorkspaceInviteOut(BaseModel):
    id: UUID
    email: str
    role: str
    expires_at: dt.datetime
    invited_by: UUID | None


def workspace_invite_out(row: WorkspaceInvite) -> WorkspaceInviteOut:
    return WorkspaceInviteOut(
        id=row.id,
        email=row.email,
        role=row.role,
        expires_at=row.expires_at,
        invited_by=row.invited_by_actor_id,
    )


class WorkspaceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspaceMemberUpdateIn(BaseModel):
    role: WorkspaceRole


class WorkspaceTransferIn(BaseModel):
    actor_id: UUID


class WorkspaceInviteIn(BaseModel):
    email: EmailStr
    role: WorkspaceRole = WorkspaceRole.MEMBER


class WorkspaceInviteAcceptIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
