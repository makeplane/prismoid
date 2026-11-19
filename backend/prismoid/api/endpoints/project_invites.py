# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project join links (ADR-062). Thin: auth → validate → one operation → serialize,
no transaction. Minting, listing and revoking are the owner's (`Project.get_for(OWNER)`); accepting
is anyone's in the Project's workspace who holds a live code, and moves their login session into that workspace.
A link is single-use: its first join spends it."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, MaybeAuthSessionIdDep, SessionDep
from prismoid.api.serializers.project_invites import (
    ProjectInviteAcceptedOut,
    ProjectInviteAcceptIn,
    ProjectInviteCreatedOut,
    ProjectInviteIn,
    ProjectInviteOut,
    project_invite_accepted_out,
    project_invite_out,
)
from prismoid.operations import project_invites as ops


async def create_project_invite(
    project_id: UUID, body: ProjectInviteIn, actor: ActorDep, session: SessionDep
) -> ProjectInviteCreatedOut:
    row, code = await ops.create_invite(
        session, actor, project_id, role=body.role, expires_in_days=body.expires_in_days
    )
    return ProjectInviteCreatedOut(**project_invite_out(row).model_dump(), code=code)


async def list_project_invites(
    project_id: UUID, actor: ActorDep, session: SessionDep
) -> list[ProjectInviteOut]:
    return [project_invite_out(row) for row in await ops.list_invites(session, actor, project_id)]


async def revoke_project_invite(
    project_id: UUID, project_invite_id: UUID, actor: ActorDep, session: SessionDep
) -> None:
    await ops.revoke_invite(session, actor, project_id, project_invite_id)


async def accept_project_invite(
    body: ProjectInviteAcceptIn, actor: ActorDep, session: SessionDep, auth_session_id: MaybeAuthSessionIdDep
) -> ProjectInviteAcceptedOut:
    project, role = await ops.accept(session, actor, body.code, auth_session_id)
    return project_invite_accepted_out(project, role)
