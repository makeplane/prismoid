# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Workspaces (ADR-061). Thin: auth → validate → one operation →
serialize, ≤20 lines each, no transaction. Who may call what is the operation's `require_role`.
Listing, creating and joining workspaces take `OrglessSessionActorDep`: they are how a person whose
every membership is gone gets back in, and an API key, pinned to one workspace, never reaches them."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, MaybeAuthSessionIdDep, OrglessSessionActorDep, SessionDep
from prismoid.api.serializers.workspaces import (
    WorkspaceBriefOut,
    WorkspaceCreateIn,
    WorkspaceInviteAcceptIn,
    WorkspaceInviteIn,
    WorkspaceInviteOut,
    WorkspaceMemberOut,
    WorkspaceMemberUpdateIn,
    WorkspaceOut,
    WorkspaceTransferIn,
    WorkspaceUpdateIn,
    workspace_brief_out,
    workspace_invite_out,
    workspace_member_out,
    workspace_out,
)
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.operations import departures
from prismoid.operations import workspaces as ops


async def list_my_workspaces(actor: OrglessSessionActorDep, session: SessionDep) -> list[WorkspaceBriefOut]:
    return [
        workspace_brief_out(workspace, member.role)
        for workspace, member in await ops.my_workspaces(session, actor)
    ]


async def create_workspace(
    body: WorkspaceCreateIn, actor: OrglessSessionActorDep, session: SessionDep
) -> WorkspaceOut:
    workspace = await ops.create_team_workspace(session, actor, name=body.name)
    return workspace_out(workspace, WorkspaceRole.OWNER)


async def get_workspace(workspace_id: UUID, actor: ActorDep, session: SessionDep) -> WorkspaceOut:
    workspace, role = await ops.get_workspace(session, actor, workspace_id)
    return workspace_out(workspace, role)


async def update_workspace(
    workspace_id: UUID, body: WorkspaceUpdateIn, actor: ActorDep, session: SessionDep
) -> WorkspaceOut:
    workspace, role = await ops.rename_workspace(session, actor, workspace_id, name=body.name, slug=body.slug)
    return workspace_out(workspace, role)


async def list_workspace_members(
    workspace_id: UUID, actor: ActorDep, session: SessionDep
) -> list[WorkspaceMemberOut]:
    return [workspace_member_out(m, u) for m, u in await ops.list_members(session, actor, workspace_id)]


async def update_workspace_member(
    workspace_id: UUID, actor_id: UUID, body: WorkspaceMemberUpdateIn, actor: ActorDep, session: SessionDep
) -> WorkspaceMemberOut:
    member, user = await ops.set_role(session, actor, workspace_id, actor_id, body.role)
    return workspace_member_out(member, user)


async def remove_workspace_member(
    workspace_id: UUID, actor_id: UUID, actor: ActorDep, session: SessionDep
) -> None:
    await departures.remove_member(session, actor, workspace_id, actor_id)


async def leave_workspace(workspace_id: UUID, actor: ActorDep, session: SessionDep) -> None:
    await departures.leave_workspace(session, actor, workspace_id)


async def transfer_workspace(
    workspace_id: UUID, body: WorkspaceTransferIn, actor: ActorDep, session: SessionDep
) -> WorkspaceOut:
    workspace = await ops.transfer_ownership(session, actor, workspace_id, body.actor_id)
    return workspace_out(workspace, WorkspaceRole.ADMIN)


async def list_workspace_invites(
    workspace_id: UUID, actor: ActorDep, session: SessionDep
) -> list[WorkspaceInviteOut]:
    return [workspace_invite_out(row) for row in await ops.list_invites(session, actor, workspace_id)]


async def create_workspace_invite(
    workspace_id: UUID, body: WorkspaceInviteIn, actor: ActorDep, session: SessionDep
) -> WorkspaceInviteOut:
    return workspace_invite_out(
        await ops.invite(session, actor, workspace_id, email=body.email, role=body.role)
    )


async def revoke_workspace_invite(
    workspace_id: UUID, invite_id: UUID, actor: ActorDep, session: SessionDep
) -> None:
    await ops.revoke_invite(session, actor, workspace_id, invite_id)


async def accept_workspace_invite(
    body: WorkspaceInviteAcceptIn,
    actor: OrglessSessionActorDep,
    session: SessionDep,
    auth_session_id: MaybeAuthSessionIdDep,
) -> WorkspaceBriefOut:
    workspace, role = await ops.accept_invite(session, actor, body.code, auth_session_id)
    return workspace_brief_out(workspace, role.value)
