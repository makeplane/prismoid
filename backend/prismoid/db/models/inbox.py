# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""InboxItem - a notification to an Actor, read/unread.

**Inbox notifies only**. It never sends outbound mail, never runs anything, and never
retries anything. It points at the gate, the escalation, or the delivery - and the pointer is
the product: an inbox item you cannot act on from is just noise with a timestamp.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Collection
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ColumnElement,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    and_,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceStatus
from prismoid.errors import NotFound, TenantUnbound


class InboxKind(StrEnum):
    TASK_COMPLETED = "task_completed"
    GATE_OPENED = "gate_opened"
    ESCALATION = "escalation"
    BUDGET = "budget"
    PROJECT_SHARED = "project_shared"
    # The way out of a Project, told to the person who lost it: their open views and sidebar
    # learn the room is gone, while the task fetch path still answers NotFound (no existence leak).
    PROJECT_REMOVED = "project_removed"
    # Someone other than the owner asked a bot for work; the card waits for the owner's Start.
    CARD_WAITING = "card_waiting"
    # Routine kinds: the silence rule's outputs. `routine_run` covers both a
    # noteworthy success (per the notify policy) and a run's failure; the others are the
    # standing object's own life events - and every auto-pause carries its one-click resume.
    ROUTINE_RUN = "routine_run"
    ROUTINE_PAUSED = "routine_paused"
    ROUTINE_HELD = "routine_held"
    ROUTINE_RETIRED = "routine_retired"
    # A connector's credential died between tasks:
    # the owner learns before the next mount drops it, not from a task note after.
    CONNECTOR_BROKEN = "connector_broken"
    # A running step asked the person a question (an INPUT gate); the item points at the gate.
    QUESTION = "question"


class InboxItem(Base):
    __tablename__ = "inbox_items"
    __table_args__ = (
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project still takes its rows with it (CASCADE, as before).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_inbox_items_project_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_inbox_items_actor_id_read_at", "actor_id", "read_at"),
        # `task_id`/`gate_id` are CASCADE FKs (a task purge; a replan's gate cleanup); unindexed
        # each such delete scanned the whole inbox (audit finding). Partial - only rows carrying
        # the pointer need finding by it.
        Index("ix_inbox_items_task_id", "task_id", postgresql_where=text("task_id IS NOT NULL")),
        Index("ix_inbox_items_gate_id", "gate_id", postgresql_where=text("gate_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False, default="")

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), default=None
    )
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gates.id", ondelete="CASCADE"), default=None
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    read_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = created_at()

    def is_unread(self) -> bool:
        return self.read_at is None

    # ── the only fetch paths (no select() outside db/models/) ─────────────────────────────────

    @classmethod
    async def list_for(
        cls, session: AsyncSession, actor: Actor, *, unread_only: bool = False, limit: int = 50
    ) -> list[InboxItem]:
        """**Scoped by actor, always.** An inbox is the one place in the product where "whose is
        this?" and "what is this?" are the same question - there is no such thing as an inbox item
        you may read but that is not addressed to you. So the actor is not a filter you could
        forget; it is the first clause of the only query there is."""
        query = select(cls).where(cls.workspace_id == tenant.workspace_of(session), cls.actor_id == actor.id)
        if unread_only:
            query = query.where(cls.read_at.is_(None))
        return list(await session.scalars(query.order_by(cls.created_at.desc()).limit(limit)))

    @classmethod
    async def get_for(cls, session: AsyncSession, item_id: uuid.UUID, actor: Actor) -> InboxItem:
        """NotFound, never Forbidden - a 403 would confirm that somebody else has an item with this
        id, which is exactly the thing an inbox must not leak."""
        item = await session.scalar(
            select(cls).where(
                cls.id == item_id, cls.workspace_id == tenant.workspace_of(session), cls.actor_id == actor.id
            )
        )
        if item is None:
            raise NotFound("no such inbox item")
        return item

    @classmethod
    async def _mark_read(
        cls, session: AsyncSession, at: dt.datetime, *scope: ColumnElement[bool]
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """The one mark-read statement: the bound workspace's unread rows that match `scope` read
        at `at`, in one UPDATE; the `(item, recipient)` of each row it read, so the caller can tell
        those people's phones."""
        result = await session.execute(
            update(cls)
            .where(cls.workspace_id == tenant.workspace_of(session), cls.read_at.is_(None), *scope)
            .values(read_at=at)
            .returning(cls.id, cls.actor_id)
        )
        return [(item_id, actor_id) for item_id, actor_id in result.all()]

    @classmethod
    async def mark_all_read(cls, session: AsyncSession, actor: Actor, at: dt.datetime) -> int:
        """One UPDATE, not a read-modify-write loop. `prismoid inbox read --all` on a thousand
        unread items is one statement, and it is the same statement on one."""
        return len(await cls._mark_read(session, at, cls.actor_id == actor.id))

    @classmethod
    async def mark_task_read(
        cls, session: AsyncSession, actor: Actor, task_id: uuid.UUID, at: dt.datetime
    ) -> list[uuid.UUID]:
        """The person has the task in front of them: their own unread items about it read, nobody
        else's. The ids it read."""
        rows = await cls._mark_read(session, at, cls.actor_id == actor.id, cls.task_id == task_id)
        return [item_id for item_id, _ in rows]

    @classmethod
    async def mark_gates_read(
        cls, session: AsyncSession, gate_ids: Collection[uuid.UUID], at: dt.datetime
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Closed gates ask nobody anything any more, so every recipient's knock about them reads.
        Rides the partial `gate_id` index. The `(item, recipient)` of each knock it read."""
        if not gate_ids:
            return []
        return await cls._mark_read(session, at, cls.gate_id.in_(list(gate_ids)))

    @classmethod
    async def unread_count(cls, session: AsyncSession, actor: Actor) -> int:
        return await cls.unread_count_of(session, actor.id)

    @classmethod
    async def unread_count_of(cls, session: AsyncSession, actor_id: uuid.UUID) -> int:
        """`unread_count` by id: the push sender knows the recipient, not their row."""
        return int(
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(
                    cls.workspace_id == tenant.workspace_of(session),
                    cls.actor_id == actor_id,
                    cls.read_at.is_(None),
                )
            )
            or 0
        )

    @classmethod
    async def unread_total_of(cls, session: AsyncSession, actor_id: uuid.UUID) -> int:
        """A person's unread count across every active workspace they still belong to - the phone's
        badge (ADR-100), which is the person's, not one workspace's. A suspended workspace is left
        out, as at every other membership door: its inbox can't be opened or read, so its items
        would hold the badge up for good. An identity read, so it asks for a
        platform unit of work and fails closed on any other: a workspace-bound session must never
        count past its workspace."""
        if not tenant.is_platform(session):
            raise TenantUnbound("a person's unread total is a platform read")
        return int(
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .join(
                    WorkspaceMember,
                    and_(
                        WorkspaceMember.workspace_id == cls.workspace_id,
                        WorkspaceMember.actor_id == cls.actor_id,
                    ),
                )
                .join(
                    Workspace,
                    and_(
                        Workspace.id == cls.workspace_id,
                        Workspace.status == WorkspaceStatus.ACTIVE.value,
                    ),
                )
                .where(cls.actor_id == actor_id, cls.read_at.is_(None))
            )
            or 0
        )

    @classmethod
    async def exists_for_task(cls, session: AsyncSession, task_id: uuid.UUID, kind: str) -> bool:
        """The double-notification guard: a routine run that escalated already
        carries its diagnosis item - the routine's own failure note would be the same news
        twice. Rides the partial `task_id` index."""
        row = await session.scalar(select(cls.id).where(cls.task_id == task_id, cls.kind == kind).limit(1))
        return row is not None

    @classmethod
    async def unread_about(
        cls, session: AsyncSession, actor_id: uuid.UUID, kind: str, key: str, value: str
    ) -> bool:
        """Is an unread item of this kind, whose payload names `key=value`, already waiting for this
        actor? The cap on knocks that repeat: one unread item per subject, not one per ask."""
        row = await session.scalar(
            select(cls.id)
            .where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.actor_id == actor_id,
                cls.kind == kind,
                cls.read_at.is_(None),
                cls.payload.contains({key: value}),
            )
            .limit(1)
        )
        return row is not None
