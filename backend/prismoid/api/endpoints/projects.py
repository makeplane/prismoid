# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Projects - simple folders. One owner (a column), members (editor|viewer).

Every route requires an Actor - authentication is never a feature that arrives later.
Ownership transfer (owner-only) is the last route here.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, PageDep, SessionDep
from prismoid.api.serializers.artifacts import ArtifactOut, artifact_out
from prismoid.api.serializers.common import PageOut
from prismoid.api.serializers.projects import (
    MemberIn,
    MemberOut,
    ProjectCreateIn,
    ProjectListOut,
    ProjectOut,
    ProjectUpdateIn,
    ProjectUsageOut,
    RemovedOut,
    RoleIn,
    TransferIn,
    member_out,
    project_list_out,
    project_out,
)
from prismoid.db.models.project import Role
from prismoid.operations import artifacts as artifact_ops
from prismoid.operations import projects as ops


def _out(read: ops.ProjectRead) -> ProjectOut:
    """The one Project read, from the one operation that produced it: row, the caller's role, owner."""
    return project_out(read.project, read.role.value, read.owner)


async def create_project(body: ProjectCreateIn, actor: ActorDep, session: SessionDep) -> ProjectOut:
    return _out(await ops.create(session, actor, name=body.name, budget_usd=body.budget_usd))


async def list_projects(
    actor: ActorDep, session: SessionDep, page: PageDep, q: str | None = None
) -> PageOut[ProjectListOut]:
    rows = await ops.list_projects(session, actor, q=q, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1].project.cursor() if len(rows) == page.limit else None
    return PageOut(items=[project_list_out(r.project, r.role.value) for r in rows], next_cursor=next_cursor)


async def get_project(project_id: UUID, actor: ActorDep, session: SessionDep) -> ProjectOut:
    # the DETAIL read carries the actor's REAL role - an editor is not a "viewer" (list stays
    # cheap) - and names the owner
    return _out(await ops.get(session, project_id=project_id, actor=actor))


async def update_project(
    project_id: UUID, body: ProjectUpdateIn, actor: ActorDep, session: SessionDep
) -> ProjectOut:
    read = await ops.update(
        session,
        actor,
        project_id,
        name=body.name,
        budget_usd=body.budget_usd,
        clear_budget=body.clear_budget,
        instructions=body.instructions,
        icon=body.icon,
        icon_hue=body.icon_hue,
        clear_icon=body.clear_icon,
        clear_hue=body.clear_hue,
    )
    return _out(read)  # only an owner reaches here


async def get_project_usage(project_id: UUID, actor: ActorDep, session: SessionDep) -> ProjectUsageOut:
    mtd = await ops.usage_mtd(session, actor, project_id)
    project = (await ops.get(session, project_id=project_id, actor=actor)).project
    remaining = (project.budget_usd - mtd) if project.budget_usd is not None else None
    return ProjectUsageOut(
        project_id=str(project_id),
        month_to_date_usd=str(mtd),
        budget_usd=str(project.budget_usd) if project.budget_usd is not None else None,
        remaining_usd=str(remaining) if remaining is not None else None,
    )


async def add_member(project_id: UUID, body: MemberIn, actor: ActorDep, session: SessionDep) -> MemberOut:
    return member_out(
        *await ops.add_member(session, actor, project_id, email=body.email, role=Role(body.role))
    )


async def list_members(project_id: UUID, actor: ActorDep, session: SessionDep) -> list[MemberOut]:
    return [member_out(m, person) for m, person in await ops.members_with_users(session, actor, project_id)]


async def change_role(
    project_id: UUID, actor_id: UUID, body: RoleIn, actor: ActorDep, session: SessionDep
) -> MemberOut:
    return member_out(*await ops.change_role(session, actor, project_id, actor_id, Role(body.role)))


async def remove_member(project_id: UUID, actor_id: UUID, actor: ActorDep, session: SessionDep) -> RemovedOut:
    await ops.remove_member(session, actor, project_id, actor_id)
    return RemovedOut(removed=str(actor_id))


async def list_project_artifacts(
    project_id: UUID, page: PageDep, actor: ActorDep, session: SessionDep, q: str | None = None
) -> PageOut[ArtifactOut]:
    arts = await ops.artifacts(session, actor, project_id, q=q, limit=page.limit, cursor=page.cursor)
    next_cursor = arts[-1].cursor() if len(arts) == page.limit else None
    return PageOut(items=[artifact_out(artifact_ops.view(a)) for a in arts], next_cursor=next_cursor)


async def transfer_project(
    project_id: UUID, body: TransferIn, actor: ActorDep, session: SessionDep
) -> ProjectOut:
    # the caller was the owner; the read answers as they now stand (an editor), naming the new owner
    return _out(await ops.transfer(session, actor, project_id, new_owner_email=body.email))
