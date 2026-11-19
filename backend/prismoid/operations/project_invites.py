# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project join links (ADR-062): the owner mints, one person in the workspace holding the code joins.

Every link is single-use and time-bound: the accept that grants
membership spends it, and from then on the code lets nobody new in - a second holder and a member
the owner later removes get the same 404 as any dead code. Re-opening a spent link you are already
in through (the Back button, a double submit) is not an error: the people already in - at the
link's role or higher, or the owner - are answered with their role, spending and knocking nothing,
so a spent code is an oracle only to those who can see the Project anyway. A revoked or expired link
is dead for everyone. A link is dead too while its Project's owner cannot act in the workspace (removed,
left, or suspended): their private Project is archived, never opened by a stale link.

The code is a bearer secret shown once; only its hash is stored. Redemption is one transaction:
find the live row by hash (unfenced - the code IS the credential), refuse anyone outside the
row's workspace in the same words as an unknown code, bind the row's workspace, then join at the link's
role - never lowering one, never spending the link on someone who already holds that role or
higher (they accepted nothing new), never making the owner a member row.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project, Role, at_least
from prismoid.db.models.project_invite import InviteRole, ProjectInvite
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.session import AuthSession
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceMember
from prismoid.errors import FeatureDisabled, NotFound
from prismoid.operations import device_grants
from prismoid.operations import inbox as inbox_ops

_DEAD = "this invite is invalid or has expired"


async def create_invite(
    session: AsyncSession,
    actor: Actor,
    project_id: uuid.UUID,
    *,
    role: InviteRole,
    expires_in_days: int | None,
) -> tuple[ProjectInvite, str]:
    await Project.get_for(session, project_id, actor, min_role=Role.OWNER)
    row, code = await ProjectInvite.mint(
        session,
        project_id=project_id,
        role=role,
        expires_in_days=expires_in_days or settings.workspaces.project_invite_ttl_days,  # never unbounded
        created_by=actor.id,
    )
    await session.commit()
    return row, code


async def list_invites(session: AsyncSession, actor: Actor, project_id: uuid.UUID) -> list[ProjectInvite]:
    """Newest first, at most 200 rows, every state - the owner sees what each link did."""
    await Project.get_for(session, project_id, actor, min_role=Role.OWNER)
    return await ProjectInvite.for_project(session, project_id)


async def revoke_invite(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID, invite_id: uuid.UUID
) -> None:
    await Project.get_for(session, project_id, actor, min_role=Role.OWNER)
    row = await ProjectInvite.get_in_project(session, project_id, invite_id)
    if row is None:
        raise NotFound("no such invite")
    if row.revoked_at is None:  # idempotent: a second revoke keeps the first moment
        row.revoked_at = utcnow()
    await session.commit()


async def _owner_present(session: AsyncSession, project: Project) -> bool:
    """Whether the Project's owner can still act in its workspace: an active account (a suspended one is
    the kill switch) holding a membership row there. A departing person's private Project stays
    theirs and is archived, so a link they minted before leaving must
    not open it - it answers as a dead code, the channel door's rule for an owner who is gone."""
    owner = await session.get(Actor, project.owner_actor_id)
    if owner is None or not owner.is_active():
        return False
    return await WorkspaceMember.get(session, project.workspace_id, project.owner_actor_id) is not None


async def accept(
    session: AsyncSession, actor: Actor, code: str, auth_session_id: uuid.UUID | None
) -> tuple[Project, str]:
    if settings.environment == "local":
        raise FeatureDisabled("members are not available in local mode")
    # One redemption of a link at a time; a spent link is read too, for the people already in.
    row = await ProjectInvite.by_code(session, code, lock=True, spent_ok=True)
    if row is None:
        raise NotFound(_DEAD)
    membership = await WorkspaceMember.get(session, row.workspace_id, actor.id)
    workspace = await Workspace.get(session, row.workspace_id)
    if membership is None or workspace is None or not workspace.is_active():
        raise NotFound(_DEAD)  # never confirm another workspace exists
    tenant.bind(
        session, row.workspace_id, membership.role
    )  # the row's workspace, not the caller's active one
    project = await session.get(Project, row.project_id)
    if project is None or not await _owner_present(session, project):
        raise NotFound(_DEAD)

    if actor.id == project.owner_actor_id:
        role = Role.OWNER.value  # the owner is the column, never a row: no use, no member row
    else:
        existing = await ProjectMember.get(session, project.id, actor.id)
        if existing is not None and at_least(Role(existing.role), Role(row.role)):
            role = existing.role  # a link never lowers a role, and holding it spends nothing
        elif row.state(utcnow()) != "live":
            raise NotFound(_DEAD)  # spent: it lets nobody new in, in the words of an unknown code
        else:
            await device_grants.refuse_members(session, project)  # a device task stays private
            if not await ProjectInvite.take_use(session, row.id):
                raise NotFound(_DEAD)  # lost the race for the link's one use
            inviter = row.created_by_actor_id or project.owner_actor_id
            granted = await ProjectMember.grant(
                session, project.id, actor.id, row.role, added_by=inviter, raise_only=True
            )
            by = await User.by_actor_ids(session, [inviter])
            name = by[inviter].name if inviter in by else "Someone"
            await inbox_ops.project_shared(session, actor_id=actor.id, project=project, by_name=name)
            role = granted.role

    if auth_session_id is not None:  # follow the link into its workspace, as a workspace invite does
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == auth_session_id, AuthSession.user_id == actor.id)
            .values(active_workspace_id=row.workspace_id)
        )
    await session.commit()
    logfire.info(
        "project {project}: {actor} joined through link {invite} as {role}",
        project=str(project.id),
        actor=str(actor.id),
        invite=str(row.id),
        role=role,
    )
    return project, role
