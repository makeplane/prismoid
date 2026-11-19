# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Capture - a soft intake record (note/url/idea), promotable into a Task.

**Capture runs nothing**. That is the whole design: the cost of writing something down has
to be near zero - `<50 ms` is a performance budget - or people stop writing things down. Promotion is
the moment a thought becomes work, and it is explicit.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk, vocabulary
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.errors import NotFound


class CaptureStatus(StrEnum):
    OPEN = "open"
    PROMOTED = "promoted"
    ARCHIVED = "archived"


class Capture(Base):
    __tablename__ = "captures"
    __table_args__ = (
        vocabulary("status", CaptureStatus),
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project unfiles the row (only project_id nulls; workspace_id stays).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_captures_project_workspace",
            ondelete="SET NULL (project_id)",
        ),
        Index("ix_captures_status", "status"),
        Index("ix_captures_actor_id", "actor_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    text: Mapped[str] = mapped_column(String(8000), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2000), default=None)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CaptureStatus.OPEN)

    # Round-trips: the capture points at what it became, and the task keeps no back-pointer -
    # one direction, one truth.
    promoted_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def open_for(cls, session: AsyncSession, actor: Actor) -> list[Capture]:
        return list(
            await session.scalars(
                select(cls)
                .where(
                    cls.workspace_id == tenant.workspace_of(session),
                    cls.actor_id == actor.id,
                    cls.status == CaptureStatus.OPEN.value,
                )
                .order_by(cls.created_at.desc())
            )
        )

    @classmethod
    async def get_for(cls, session: AsyncSession, capture_id: uuid.UUID, actor: Actor) -> Capture | None:
        """The capture, if this actor may see it: their own, or one in a project they belong to.
        NotFound (via a None return the caller raises on) - never a leak of a stranger's note, nor
        of the actor's own note in another workspace."""
        cap = await session.scalar(
            select(cls).where(cls.id == capture_id, cls.workspace_id == tenant.workspace_of(session))
        )
        if cap is None:
            return None
        if cap.actor_id == actor.id:
            return cap
        if cap.project_id is not None:
            from prismoid.db.models.project import Project

            # Suppress only the not-visible signal (Project.get_for raises NotFound below viewer),
            # never bare Exception - a real DB error must surface as a 500, not be silently
            # laundered into "no such capture" (audit finding).
            with contextlib.suppress(NotFound):
                await Project.get_for(session, cap.project_id, actor)
                return cap
        return None
