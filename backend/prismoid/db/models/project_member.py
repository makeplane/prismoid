# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""ProjectMember - one shared-access grant: (project, actor, role editor|viewer).

**The owner is never a row here.** It is `projects.owner_actor_id`. Duplicating it into the
membership table would create two sources of truth for the one question that must never have
two answers - *who can transfer this project away from me*.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Index, String, case, delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (Index("ix_project_members_actor_id", "actor_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    added_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False)
    added_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def for_project(cls, session: AsyncSession, project_id: uuid.UUID) -> list[ProjectMember]:
        return list(await session.scalars(select(cls).where(cls.project_id == project_id)))

    @classmethod
    async def any_besides(cls, session: AsyncSession, project_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
        """The project has a member who is not `owner_id` (one EXISTS, never the roster)."""
        query = select(exists().where(cls.project_id == project_id, cls.actor_id != owner_id))
        return bool(await session.scalar(query))

    @classmethod
    async def get(
        cls, session: AsyncSession, project_id: uuid.UUID, actor_id: uuid.UUID
    ) -> ProjectMember | None:
        return await session.get(cls, {"project_id": project_id, "actor_id": actor_id})

    @classmethod
    async def roles_in(
        cls, session: AsyncSession, actor_id: uuid.UUID, project_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """This actor's membership role in each of these Projects, in ONE query (a list page's
        roles, never one lookup per row). A Project they own has no row - the owner is a column."""
        if not project_ids:
            return {}
        rows = await session.execute(
            select(cls.project_id, cls.role).where(cls.actor_id == actor_id, cls.project_id.in_(project_ids))
        )
        return dict(rows.tuples().all())

    @classmethod
    async def grant(
        cls,
        session: AsyncSession,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        role: str,
        *,
        added_by: uuid.UUID,
        raise_only: bool = False,
    ) -> ProjectMember:
        """Upsert one grant, in one statement - a racing insert (two join links, or a link racing
        the owner's add) cannot 500 - and answer the row as it now stands. `added_by` is the
        FIRST grant's: written on insert, never rewritten. `raise_only` is a join link's rule:
        it never lowers a role, so a row already at or above `role` is left as it
        is; without it (the owner's explicit set) the role moves either way."""
        from prismoid.db.models.project import Role, at_least  # project imports this module

        rank = {r.value: sum(at_least(r, other) for other in Role) for r in Role}
        stmt = insert(cls).values(project_id=project_id, actor_id=actor_id, role=role, added_by=added_by)
        held = cls.__table__.c.role
        stmt = stmt.on_conflict_do_update(
            index_elements=[cls.__table__.c.project_id, cls.__table__.c.actor_id],
            set_={"role": stmt.excluded.role},
            where=(case(rank, value=held) < case(rank, value=stmt.excluded.role)) if raise_only else None,
        )
        row = await session.scalar(stmt.returning(cls), execution_options={"populate_existing": True})
        if row is None:  # raise_only kept a role at or above this one
            row = await session.get(
                cls, {"project_id": project_id, "actor_id": actor_id}, populate_existing=True
            )
        if row is None:  # unreachable: the refused update conflicted on this very key
            raise LookupError(f"no grant for {actor_id} in project {project_id}")
        return row

    @classmethod
    async def remove(cls, session: AsyncSession, project_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
        """Delete the grant; True when there was one to delete."""
        gone = await session.execute(
            delete(cls).where(cls.project_id == project_id, cls.actor_id == actor_id).returning(cls.actor_id)
        )
        return gone.first() is not None
