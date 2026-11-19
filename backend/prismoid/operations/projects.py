# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Projects - the shared folder (M4).

A Project adds three things to a set of tasks and nothing else: **membership** (who can see and do
what), a **budget** (a ceiling the Bursar enforces), and a **rollup** (what it has spent). A task
lives in at most one project via a single nullable FK; it is not *owned* by the project, it is *filed*
in it, and it moves in and out freely.

`operations` owns the transactions. Authorisation is `Project.get_for(min_role)` - the one fetch path,
which raises `NotFound` for a stranger (existence is never leaked) and `Forbidden` for a member
who lacks the role for an action (they can already see the project).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import logfire
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.usage import ProjectUsageDaily
from prismoid.db.models.user import User
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.errors import FeatureDisabled, Forbidden, NotFound, ValidationFailed
from prismoid.operations import device_grants
from prismoid.operations import inbox as inbox_ops


@dataclass(frozen=True)
class ProjectRead:
    """A Project as the API reads it, from ONE operation: the row, the caller's role in it, and its
    owner as a person. The owner is a column, never a member row, so no member list
    names them - and a view that fetched the name in a second call would break "one call per view".
    `owner` is `None` only for an owner with no profile row (the API then answers a blank name)."""

    project: Project
    role: Role
    owner: Person


@dataclass(frozen=True)
class Person:
    """Someone a Project names to its caller - its owner, a member - as the caller may know them. A
    workspace guest knows people by name only (`WorkspaceRole.sees_emails`): `email` is None for them, here,
    where every Project read that names people is built, so no view can forget it. A person with no
    profile row reads as blank."""

    actor_id: uuid.UUID
    name: str
    email: str | None


def _as_seen(session: AsyncSession, actor_id: uuid.UUID, user: User | None) -> Person:
    """`user` as this request's caller may see them - the workspace role the door bound."""
    role = tenant.role_of(session)
    sees_emails = role is not None and WorkspaceRole(role).sees_emails
    email = (user.email if user else "") if sees_emails else None
    return Person(actor_id=actor_id, name=user.name if user else "", email=email)


async def _people(session: AsyncSession, actor_ids: list[uuid.UUID]) -> dict[uuid.UUID, Person]:
    """The people behind `actor_ids`, as the caller may see them - one IN-query."""
    users = await User.by_actor_ids(session, actor_ids)
    return {a: _as_seen(session, a, users.get(a)) for a in actor_ids}


async def _owner_of(session: AsyncSession, owner_actor_id: uuid.UUID) -> Person:
    """The owner, named - one keyed read."""
    return (await _people(session, [owner_actor_id]))[owner_actor_id]


async def create(
    session: AsyncSession, actor: Actor, *, name: str, budget_usd: Decimal | None = None
) -> ProjectRead:
    """Create a project. The creator is its **owner** - the column, not a membership row."""
    # The owner-is-human invariant, enforced not merely narrated: `owner_actor_id` targets
    # `actors`, and only a human satellite may hold it. An agent/service actor cannot own a project.
    if not actor.is_human():
        raise Forbidden("only a human actor can own a project")
    project = Project(id=uuid.uuid4(), name=name, owner_actor_id=actor.id, budget_usd=budget_usd)
    session.add(project)
    await session.commit()
    logfire.info("project {project} created by {actor}", project=str(project.id), actor=str(actor.id))
    return ProjectRead(project, Role.OWNER, await _owner_of(session, actor.id))


@dataclass(frozen=True)
class ProjectListed:
    """One row of the Projects list: the Project and the caller's REAL role in it (owner, editor or
    viewer) - the pickers decide what to offer from it (a viewer's Project is no move target)."""

    project: Project
    role: Role


async def list_projects(
    session: AsyncSession,
    actor: Actor,
    *,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[ProjectListed]:
    """The Projects this actor owns or belongs to, each with their role - the page plus ONE role
    query (N+1-free), not the owner-or-viewer guess the list used to answer."""
    rows = await Project.list_for(session, actor, q=q, limit=limit, cursor=cursor)
    roles = await ProjectMember.roles_in(
        session, actor.id, [s.id for s in rows if s.owner_actor_id != actor.id]
    )
    return [
        ProjectListed(
            s, Role.OWNER if s.owner_actor_id == actor.id else Role(roles.get(s.id, Role.VIEWER.value))
        )
        for s in rows
    ]


async def get(session: AsyncSession, project_id: uuid.UUID, actor: Actor) -> ProjectRead:
    """Read a project, the actor's effective role in it, and its owner. Viewer suffices to read - a
    stranger still gets 404 from `get_for` - and the role rides back so the caller presents only
    the controls the actor may use, computed once on the server rather than guessed client-side."""
    project = await Project.get_for(session, project_id, actor)  # the one fetch path; viewer is enough
    member = await ProjectMember.get(session, project_id, actor.id)
    members = {member.actor_id: Role(member.role)} if member is not None else {}
    role = project.role_of(actor.id, members) or Role.VIEWER  # get_for proved ≥viewer; never None
    return ProjectRead(project, role, await _owner_of(session, project.owner_actor_id))


async def add_member(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID, *, email: str, role: Role
) -> tuple[ProjectMember, Person]:
    """Share a project - **owner only**. `add_member` takes an **email**, not an actor id: the API
    speaks in users, and the owner adding a colleague knows their email, not their internal id.

    The user must already exist. Inviting-by-email a person with no account is an onboarding flow, not
    a sharing one, and conflating them is how you end up emailing strangers; that door stays closed.
    """
    if settings.environment == "local":
        raise FeatureDisabled("members are not available in local mode")
    project = await Project.get_for(session, project_id, actor, min_role=Role.OWNER)

    user = await _person_in_workspace_of(session, project, email)
    if user.actor_id == project.owner_actor_id:
        raise ValidationFailed("the owner is already in the project", fields={"email": "is the owner"})
    await device_grants.refuse_members(session, project)  # a device task stays private

    # An upsert, not an add: re-sharing to change a role is idempotent, not a 409, and a join link
    # racing this add cannot 500 it. The owner's set moves the role either way (a link only raises).
    member = await ProjectMember.grant(session, project_id, user.actor_id, role.value, added_by=actor.id)
    by = await User.by_actor_ids(session, [actor.id])
    await inbox_ops.project_shared(
        session,
        actor_id=user.actor_id,
        project=project,
        by_name=by[actor.id].name if actor.id in by else "Someone",
    )
    await session.commit()
    # By id, never the email (personal data stays out of the logs).
    logfire.info(
        "project {project}: {m} added as {role}",
        project=str(project_id),
        m=str(user.actor_id),
        role=role.value,
    )
    return member, _as_seen(session, user.actor_id, user)


async def _person_in_workspace_of(session: AsyncSession, project: Project, email: str) -> User:
    """The account behind `email`, IF they belong to the project's workspace: a project
    is shared within its workspace, never across it. Someone outside the workspace is refused in exactly the
    words an unknown email gets, so the door does not confirm that an address has an account."""
    user = await User.by_email(session, email)
    if user is None or await WorkspaceMember.get(session, project.workspace_id, user.actor_id) is None:
        raise ValidationFailed(f"no user with email {email}", fields={"email": "unknown"})
    return user


async def transfer(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID, *, new_owner_email: str
) -> ProjectRead:
    """Transfer ownership - **owner only**. The new owner is named by email (the API speaks in
    users); the previous owner is kept on as an editor, so handing over control is not a self-eviction.
    The read answers as the CALLER now stands: an editor, in a room whose owner is the new one."""
    if not actor.is_human():  # the owner-is-human invariant, enforced at every owner-setting path
        raise Forbidden("only a human actor can own a project")
    project = await Project.get_for(session, project_id, actor, min_role=Role.OWNER)
    new_owner = await _person_in_workspace_of(session, project, new_owner_email)
    if new_owner.actor_id == project.owner_actor_id:
        raise ValidationFailed("that user already owns the project", fields={"email": "is the owner"})
    # The previous owner stays on as a member: a Project holding a device task takes none.
    await device_grants.refuse_members(session, project)

    previous_owner = project.owner_actor_id
    project.owner_actor_id = new_owner.actor_id
    # The new owner's membership row (if any) is now redundant - the owner is the column, not a row.
    await session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.actor_id == new_owner.actor_id
        )
    )
    session.add(
        ProjectMember(
            project_id=project_id, actor_id=previous_owner, role=Role.EDITOR.value, added_by=actor.id
        )
    )
    await session.commit()
    logfire.info("project {project} transferred to {m}", project=str(project_id), m=str(new_owner.actor_id))
    return ProjectRead(project, Role.EDITOR, _as_seen(session, new_owner.actor_id, new_owner))


async def list_members(session: AsyncSession, actor: Actor, project_id: uuid.UUID) -> list[ProjectMember]:
    await Project.get_for(session, project_id, actor)  # any member may see the membership
    return await ProjectMember.for_project(session, project_id)


async def members_with_users(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID
) -> list[tuple[ProjectMember, Person]]:
    """The membership as people, not ids (a member list that shows opaque ids is
    anti-collaborative), each as the caller may see them - one IN-query."""
    members = await list_members(session, actor, project_id)
    people = await _people(session, [m.actor_id for m in members])
    return [(m, people[m.actor_id]) for m in members]


async def change_role(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID, member_actor_id: uuid.UUID, role: Role
) -> tuple[ProjectMember, Person]:
    """Promote or demote a member - **owner only**. Answers with the member as a person."""
    await Project.get_for(session, project_id, actor, min_role=Role.OWNER)
    member = await ProjectMember.get(session, project_id, member_actor_id)
    if member is None:
        raise NotFound("no such member")
    member.role = role.value
    await session.commit()
    return member, (await _people(session, [member_actor_id]))[member_actor_id]


async def remove_member(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID, member_actor_id: uuid.UUID
) -> None:
    """Revoke access - **owner only**, and it takes effect on the member's **next request**, because
    `Project.get_for` reads the membership fresh every time. No session to expire, no token to
    revoke: the grant is a row, and the row is gone. The removed person is told (their inbox), so an
    open page learns the room is gone without the task fetch path ever answering anything but
    NotFound."""
    project = await Project.get_for(session, project_id, actor, min_role=Role.OWNER)
    removed = await ProjectMember.remove(session, project_id, member_actor_id)
    if removed and member_actor_id != actor.id:
        by = await User.by_actor_ids(session, [actor.id])
        await inbox_ops.project_removed(
            session,
            actor_id=member_actor_id,
            project=project,
            by_name=by[actor.id].name if actor.id in by else "Someone",
        )
    await session.commit()
    logfire.info("project {project}: member {m} removed", project=str(project_id), m=str(member_actor_id))


async def usage_mtd(session: AsyncSession, actor: Actor, project_id: uuid.UUID) -> Decimal:
    """The project's month-to-date spend, from the rollup - any member may see it."""
    from datetime import date

    await Project.get_for(session, project_id, actor)
    return await ProjectUsageDaily.month_to_date(session, project_id, on=date.today())


async def artifacts(
    session: AsyncSession,
    actor: Actor,
    project_id: uuid.UUID,
    *,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[Artifact]:
    """Every artifact produced by a task filed in this project - a query over its tasks,
    keyset-paginated because a project's artifacts grow without bound. `q` narrows by
    title - the quick-find a shared shelf needs."""
    await Project.get_for(session, project_id, actor)
    return await Artifact.for_project(session, project_id, q=q, limit=limit, cursor=cursor)


async def update(
    session: AsyncSession,
    actor: Actor,
    project_id: uuid.UUID,
    *,
    name: str | None = None,
    budget_usd: Decimal | None = None,
    clear_budget: bool = False,
    instructions: str | None = None,
    icon: str | None = None,
    icon_hue: int | None = None,
    clear_icon: bool = False,
    clear_hue: bool = False,
) -> ProjectRead:
    """Rename, re-budget, re-charter or re-face - **owner only**. `get_for(min_role=OWNER)` is
    the gate: a member who is not the owner gets a 403 (they can see the project), a stranger a 404
    (they cannot).

    `clear_budget` distinguishes "set no budget" (unlimited) from "leave the budget alone", which a
    nullable field cannot express with a single value - PATCH semantics need the difference.
    `instructions` needs no such flag: it is a string, and `""` IS the cleared charter. The icon
    speaks the budget's grammar: `clear_icon` returns the project to the default face (glyph AND hue),
    `clear_hue` alone returns just the hue to neutral; shapes are validated at the door
    (ProjectUpdateIn) - here they are stored, never interpreted.
    """
    project = await Project.get_for(session, project_id, actor, min_role=Role.OWNER)

    if name is not None:
        if not name.strip():
            raise ValidationFailed("a project name cannot be blank", fields={"name": "required"})
        project.name = name
    if instructions is not None:
        project.instructions = instructions.strip()
    if clear_budget:
        project.budget_usd = None
    elif budget_usd is not None:
        if budget_usd <= 0:
            raise ValidationFailed("a budget must be positive", fields={"budget_usd": "must be > 0"})
        project.budget_usd = budget_usd
    if clear_icon:
        project.icon = None
        project.icon_hue = None
    elif icon is not None:
        project.icon = icon
    if clear_hue:
        project.icon_hue = None
    elif icon_hue is not None:
        project.icon_hue = icon_hue

    await session.commit()
    logfire.info("project {project} updated by owner {actor}", project=str(project.id), actor=str(actor.id))
    return ProjectRead(project, Role.OWNER, await _owner_of(session, project.owner_actor_id))
