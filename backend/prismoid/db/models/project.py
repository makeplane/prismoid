# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project - a simple folder for Tasks: optional, owned by exactly one person, shareable.

Think of Claude's Projects. Tasks are created out of thin air and moved in, out, or between
projects at any time; the folder adds membership, budgets and rollups, and nothing else.

**"One project owner exists" is a column, not a convention.** `owner_actor_id` is on the row, so it
cannot drift out of sync with a membership table, and transfer is an explicit owner-only endpoint
rather than an UPDATE somebody remembers to make.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, escape_like, keyset_before, keyset_cursor, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.project_member import ProjectMember
from prismoid.errors import Forbidden, NotFound


class Role(StrEnum):
    """The role matrix. OWNER is the column; EDITOR and VIEWER are rows in project_members."""

    VIEWER = "viewer"  # read the project, its tasks, their streams, artifacts, usage
    EDITOR = "editor"  # + create tasks, promote captures, cancel/steer, resolve gates
    OWNER = "owner"  # + PATCH (name/budget), members, transfer


_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.EDITOR: 1, Role.OWNER: 2}


def at_least(have: Role, need: Role) -> bool:
    return _RANK[have] >= _RANK[need]


# What a viewer hears when they reach for work: they watch, and the owner is who
# grants more. Every viewer-refusing door says this one sentence.
VIEWERS_WATCH = "viewers watch; ask the Project owner for edit access"


def refusal(have: Role, need: Role) -> str:
    """Why a member who can see the project may not do this in it."""
    if have is Role.VIEWER and need is Role.EDITOR:
        return VIEWERS_WATCH
    return f"this action needs the {need.value} role; you are {have.value}"


class Project(Base):
    __tablename__ = "projects"
    # The target of every tenant root's composite (project_id, workspace_id) reference.
    __table_args__ = (UniqueConstraint("id", "workspace_id", name="uq_projects_id_workspace"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Targets `actors`, with the human requirement kept as a fat-model invariant:
    # every FK in the system points at one spine, and agent-owned projects later become a policy
    # change rather than a migration. The API still presents the owner as a *user*.
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), default=None)

    # The charter - standing instructions injected verbatim into every task filed here (owner
    # voice; the project's memory *ledger* is the editors' voice). Plain user text, never parsed.
    instructions: Mapped[str] = mapped_column(String(4000), nullable=False, default="", server_default="")

    # The project's face - user-chosen identity, presentation only (never parsed for meaning
    # server-side). `icon` is self-describing text: "lucide:<slug>" names a glyph the web
    # renders, "emoji:<char>" is a literal emoji; NULL = the default folder. `icon_hue`
    # indexes the web's categorical hue ramp (0..7); NULL = neutral. Adding icons is a web
    # change - the server stores, never interprets.
    icon: Mapped[str | None] = mapped_column(String(80), default=None)
    icon_hue: Mapped[int | None] = mapped_column(SmallInteger, default=None)

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def role_of(self, actor_id: uuid.UUID, members: dict[uuid.UUID, Role]) -> Role | None:
        """This actor's role in this project, or None if they have no access.

        The owner outranks any membership row - `owner_actor_id` is the column precisely so that
        "who owns this" cannot drift out of sync with a table. A member is whatever their row
        says; everyone else is a stranger, and a stranger gets `None`, never a default of viewer.
        """
        if actor_id == self.owner_actor_id:
            return Role.OWNER
        return members.get(actor_id)

    @classmethod
    async def get_for(
        cls,
        session: AsyncSession,
        project_id: uuid.UUID,
        actor: Actor,
        *,
        min_role: Role = Role.VIEWER,
    ) -> Project:
        """The ONE fetch path for a project, and it is role-aware.

        A **stranger** (no role) gets `NotFound` - a 404 that does not confirm the project exists. A
        **member** who lacks `min_role` for this action gets `Forbidden` - they can already see the
        project, so "not found" would be a lie. Existence is privileged; a role boundary among people
        who share the project is not (see the branch below).
        """
        # In the bound workspace only: another workspace's project is a stranger's project - a 404, so a
        # task can never be filed or moved across the workspace line through this door.
        project = await session.scalar(
            select(cls).where(cls.id == project_id, cls.workspace_id == tenant.workspace_of(session))
        )
        if project is None:
            raise NotFound("no such project")

        # One point lookup, not the roster: this gate runs on every project-scoped request, and
        # only the *requesting actor's* role decides it - loading all N members to answer a
        # one-row question made every such request O(members).
        role: Role | None = Role.OWNER if actor.id == project.owner_actor_id else None
        if role is None:
            member = await ProjectMember.get(session, project_id, actor.id)
            role = Role(member.role) if member is not None else None

        # **Two different denials, and the difference is what the actor already knows.**
        #
        # A *stranger* - no role at all - gets a 404: they were never told this project exists, and a
        # 403 would confirm it does and leak its name. But a *member* who can already see the project
        # and simply lacks the role for *this* action gets a 403: they know it exists, so refusing
        # with "not found" would be a lie, and an unhelpful one. Existence is privileged; a role
        # boundary among people who share the project is not.
        if role is None:
            raise NotFound("no such project")
        if not at_least(role, min_role):
            raise Forbidden(refusal(role, min_role))
        return project

    @classmethod
    async def hand_over(
        cls, session: AsyncSession, workspace_id: uuid.UUID, departing: uuid.UUID, heir: uuid.UUID
    ) -> list[uuid.UUID]:
        """Every SHARED Project (one with a member row) `departing` owns in `workspace_id` passes to
        `heir`, its members and contents as they were; the owner is the column, so the heir's
        member row goes. A private Project is never handed over: it stays `departing`'s (archived -
        see `count_owned`). Returns the Projects handed over."""
        moved = list(
            await session.scalars(
                update(cls)
                .where(
                    cls.workspace_id == workspace_id,
                    cls.owner_actor_id == departing,
                    cls.id.in_(select(ProjectMember.project_id)),
                )
                .values(owner_actor_id=heir)
                .returning(cls.id)
            )
        )
        if moved:
            await session.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_(moved), ProjectMember.actor_id == heir
                )
            )
        return moved

    @classmethod
    async def count_owned(cls, session: AsyncSession, workspace_id: uuid.UUID, owner: uuid.UUID) -> int:
        """How many Projects `owner` holds in `workspace_id` - after a hand-over, the private ones it
        left archived with their departed owner."""
        return int(
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.workspace_id == workspace_id, cls.owner_actor_id == owner)
            )
            or 0
        )

    @classmethod
    async def list_for(
        cls,
        session: AsyncSession,
        actor: Actor,
        *,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Project]:
        """Projects this actor owns or belongs to - newest first, keyset-paged, optional
        name search. Hundreds of projects is the design point: pickers search and scroll;
        nothing ever loads the whole set."""
        owned = select(cls.id).where(cls.owner_actor_id == actor.id)
        member = select(ProjectMember.project_id).where(ProjectMember.actor_id == actor.id)
        query = (
            select(cls)
            .where(cls.workspace_id == tenant.workspace_of(session), cls.id.in_(owned.union(member)))
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if q:
            query = query.where(cls.name.ilike(f"%{escape_like(q)}%", escape="\\"))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    # ── the operator's queries (admin surface) ────────────────────────────────────────────────
    # The console observes the whole population - no membership filter, AdminDep-only routes
    # (operations/admin_ops law 1). SQL here, policy there.

    @classmethod
    def _admin_facts(cls, month_start: dt.datetime):
        """(member_count, task_count, mtd_spend) scalar subqueries - the list's three numbers.
        MTD reads the ROLLUP (project_usage_daily), never the ledger."""
        from prismoid.db.models.project_member import ProjectMember
        from prismoid.db.models.task import Task
        from prismoid.db.models.usage import ProjectUsageDaily

        members = select(func.count()).where(ProjectMember.project_id == cls.id).scalar_subquery()
        tasks = select(func.count()).where(Task.project_id == cls.id).scalar_subquery()
        mtd = (
            select(func.coalesce(func.sum(ProjectUsageDaily.cost_usd), 0))
            .where(
                ProjectUsageDaily.project_id == cls.id,
                ProjectUsageDaily.kind == "total",
                ProjectUsageDaily.day >= month_start.date(),
            )
            .scalar_subquery()
        )
        return members, tasks, mtd

    @classmethod
    async def admin_page(
        cls,
        session: AsyncSession,
        *,
        month_start: dt.datetime,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[tuple[Project, Any, int, int, Decimal]]:
        """(project, owner User | None, member_count, task_count, mtd_spend), newest first."""
        from prismoid.db.models.user import User

        members, tasks, mtd = cls._admin_facts(month_start)
        query = (
            select(cls, User, members, tasks, mtd)
            .join(User, User.actor_id == cls.owner_actor_id, isouter=True)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if q:
            try:
                query = query.where(cls.id == uuid.UUID(q.strip()))
            except ValueError:
                query = query.where(cls.name.ilike(f"%{escape_like(q)}%", escape="\\"))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return [
            (sp, u, int(m or 0), int(t or 0), Decimal(c or 0))
            for sp, u, m, t, c in (await session.execute(query)).tuples().all()
        ]

    @classmethod
    async def admin_row(
        cls, session: AsyncSession, project_id: uuid.UUID, *, month_start: dt.datetime
    ) -> tuple[Project, Any, int, int, Decimal] | None:
        from prismoid.db.models.user import User

        members, tasks, mtd = cls._admin_facts(month_start)
        row = (
            await session.execute(
                select(cls, User, members, tasks, mtd)
                .join(User, User.actor_id == cls.owner_actor_id, isouter=True)
                .where(cls.id == project_id)
            )
        ).first()
        if row is None:
            return None
        sp, u, m, t, c = row
        return (sp, u, int(m or 0), int(t or 0), Decimal(c or 0))

    def cursor(self) -> str:
        """The keyset cursor for this row. Opaque to the client, meaningful to the index."""
        return keyset_cursor(self)
