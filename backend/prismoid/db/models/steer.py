# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""steers - one durable, attributed steering command (ADR-064). A tenant child of `tasks`: it is
reached only through its task, carries no workspace_id, and dies with it. Mutable by compare-and-set,
forward only (accepted → applied | rejected | retracted); `plan_edits{steer}` and
`plan.edited{steer}` stay its append-only record."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, utcnow, uuid_pk, vocabulary
from prismoid.schemas.steering import SteerState

_LIVE = (SteerState.ACCEPTED.value, SteerState.APPLIED.value)


class Steer(Base):
    __tablename__ = "steers"
    __table_args__ = (
        vocabulary("state", SteerState),
        UniqueConstraint("task_id", "command_id", name="uq_steers_task_command"),
        UniqueConstraint("task_id", "epoch", name="uq_steers_task_epoch"),
        # The ON DELETE SET NULL from `plan_steps` (a re-plan deletes unstarted steps): partial,
        # only rows that name a step - most never do.
        Index(
            "ix_steers_applied_step_id",
            "applied_step_id",
            postgresql_where=text("applied_step_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    turn: Mapped[int] = mapped_column(nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    issued_epoch: Mapped[int | None] = mapped_column(default=None)
    epoch: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=SteerState.ACCEPTED.value)
    applied_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="SET NULL"), default=None
    )
    safe_point: Mapped[str | None] = mapped_column(String(16), default=None)
    reason: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    at: Mapped[dt.datetime] = created_at()
    settled_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    @classmethod
    async def by_command(
        cls, session: AsyncSession, task_id: uuid.UUID, command_id: uuid.UUID
    ) -> Steer | None:
        return await session.scalar(select(cls).where(cls.task_id == task_id, cls.command_id == command_id))

    @classmethod
    async def newer_than(
        cls, session: AsyncSession, task_id: uuid.UUID, *, turn: int, epoch: int
    ) -> list[Steer]:
        """The turn's live steers past `epoch`, oldest first - what a safe point has not told the run."""
        return list(
            await session.scalars(
                select(cls)
                .where(cls.task_id == task_id, cls.turn == turn, cls.epoch > epoch, cls.state.in_(_LIVE))
                .order_by(cls.epoch)
            )
        )

    @classmethod
    async def seen_by(
        cls, session: AsyncSession, task_id: uuid.UUID, *, floor: int, epoch: int
    ) -> list[Steer]:
        """The live steers with `floor < epoch <= epoch`, oldest first - what a step has taken."""
        return list(
            await session.scalars(
                select(cls)
                .where(cls.task_id == task_id, cls.epoch > floor, cls.epoch <= epoch, cls.state.in_(_LIVE))
                .order_by(cls.epoch)
            )
        )

    @classmethod
    async def unapplied(
        cls, session: AsyncSession, task_id: uuid.UUID, *, turn: int | None = None
    ) -> list[Steer]:
        """Accepted and not yet taken by anything (fence 2, the cancel/fail reject)."""
        q = select(cls).where(cls.task_id == task_id, cls.state == SteerState.ACCEPTED.value)
        if turn is not None:
            q = q.where(cls.turn == turn)
        return list(await session.scalars(q.order_by(cls.epoch)))

    @classmethod
    async def since(cls, session: AsyncSession, task_id: uuid.UUID, epoch: int) -> list[Steer]:
        """Every steer past `epoch`, any state, oldest first - the 409's `missed`."""
        return list(
            await session.scalars(
                select(cls).where(cls.task_id == task_id, cls.epoch > epoch).order_by(cls.epoch)
            )
        )

    @classmethod
    async def states_of(
        cls, session: AsyncSession, task_id: uuid.UUID, command_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Each command's state as committed now - a column read, so the session's own copies of
        the rows (expire_on_commit=False) never answer it (the boundary re-plan's re-check)."""
        if not command_ids:
            return {}
        rows = await session.execute(
            select(cls.command_id, cls.state).where(
                cls.task_id == task_id, cls.command_id.in_(list(command_ids))
            )
        )
        return {row.command_id: row.state for row in rows}

    @classmethod
    async def at_epoch(cls, session: AsyncSession, task_id: uuid.UUID, epoch: int) -> Steer | None:
        return await session.scalar(select(cls).where(cls.task_id == task_id, cls.epoch == epoch))

    @classmethod
    async def settle(
        cls,
        session: AsyncSession,
        task_id: uuid.UUID,
        command_ids: Sequence[uuid.UUID],
        *,
        to: SteerState,
        safe_point: str | None = None,
        step_id: uuid.UUID | None = None,
        reason: str = "",
    ) -> list[Steer]:
        """Compare-and-set `accepted → to` for these commands; returns the rows THIS call moved.
        A row another writer already settled is left alone (its first answer stands)."""
        if to is SteerState.ACCEPTED:
            raise ValueError("a settle only moves forward")
        if not command_ids:
            return []
        moved = await session.scalars(
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.command_id.in_(list(command_ids)),
                cls.state == SteerState.ACCEPTED.value,
            )
            .values(
                state=to.value,
                safe_point=safe_point,
                applied_step_id=step_id,
                reason=reason[:400],
                settled_at=utcnow(),
            )
            .returning(cls)
            .execution_options(synchronize_session="fetch")
        )
        return list(moved)
