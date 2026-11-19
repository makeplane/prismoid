# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""call_log - one logged unit of a step attempt (a model request, a tool call's intent or result), so
a crash or a park replays it instead of buying it again. A tenant child of `plan_steps → tasks`;
payloads are ciphertext (`operations/call_log`), and a task's rows go at each terminal transition."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CHAR,
    ForeignKey,
    LargeBinary,
    SmallInteger,
    String,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, StrEnumColumn, rows_affected, vocabulary
from prismoid.db.engine import idempotent_write
from prismoid.db.models.plan import PlanStep
from prismoid.schemas.call_log import CallPhase


class CallLogEntry(Base):
    __tablename__ = "call_log"
    __table_args__ = (vocabulary("phase", CallPhase),)

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(primary_key=True)
    unit: Mapped[str] = mapped_column(String(300), primary_key=True)
    phase: Mapped[CallPhase] = mapped_column(StrEnumColumn(CallPhase, 8), primary_key=True)
    log_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload: Mapped[bytes | None] = mapped_column(LargeBinary)  # NULL when a ref below holds it
    payload_ref: Mapped[str | None] = mapped_column(CHAR(64))  # sha256 of an earlier row's plaintext
    blob_ref: Mapped[str | None] = mapped_column(String)  # `call_log/{step_id}/{sha}`, over 64 KB
    fingerprint: Mapped[str | None] = mapped_column(CHAR(16))
    at: Mapped[dt.datetime] = mapped_column(server_default=func.clock_timestamp(), nullable=False)

    @classmethod
    async def latest(cls, session: AsyncSession, step_id: uuid.UUID) -> list[CallLogEntry]:
        """The step's rows in its highest generation (none when the step has no rows)."""
        top = select(func.max(cls.generation)).where(cls.step_id == step_id).scalar_subquery()
        return list(await session.scalars(select(cls).where(cls.step_id == step_id, cls.generation == top)))

    @classmethod
    async def below(cls, session: AsyncSession, step_id: uuid.UUID, generation: int) -> list[CallLogEntry]:
        """The step's rows in every generation under `generation`, lowest first."""
        lower = select(cls).where(cls.step_id == step_id, cls.generation < generation)
        return list(await session.scalars(lower.order_by(cls.generation)))

    @classmethod
    async def add(cls, **values: object) -> bool:
        """Insert one row, durable on return (its own autocommit); False when the unit's phase was
        already logged (the first one stands)."""
        added = await idempotent_write(pg_insert(cls).values(**values).on_conflict_do_nothing())
        return rows_affected(added) == 1

    @classmethod
    async def forget(cls, session: AsyncSession, task_id: uuid.UUID) -> set[uuid.UUID]:
        """Delete the task's rows in the caller's transaction; returns the steps that had blobs."""
        gone = await session.execute(
            delete(cls)
            .where(cls.step_id == PlanStep.id, PlanStep.task_id == task_id)
            .returning(cls.step_id, cls.blob_ref)
        )
        return {step_id for step_id, blob_ref in gone.tuples() if blob_ref is not None}
