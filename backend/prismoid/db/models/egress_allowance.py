# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""egress_allowances - an owner's standing "always allow" hosts for the browser.
Written by a gate's `always` accept and by the settings resource; revoke is soft so the
grant's history stays. A nullable workspace column goes here, not a second table."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.errors import NotFound


class EgressAllowance(Base):
    __tablename__ = "egress_allowances"
    __table_args__ = (
        Index("ix_egress_allowances_owner_host", "owner_actor_id", "host"),
        Index(
            "ux_egress_allowances_live",
            "workspace_id",  # a live grant is per workspace - the reads that honour it are
            "owner_actor_id",
            "host",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False
    )
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # gate | settings
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gates.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = created_at()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @classmethod
    async def live_for_owner(cls, session: AsyncSession, actor_id: uuid.UUID) -> list[EgressAllowance]:
        query = (
            select(cls)
            .where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.owner_actor_id == actor_id,
                cls.revoked_at.is_(None),
            )
            .order_by(cls.created_at.desc(), cls.id.desc())
        )
        return list(await session.scalars(query))

    @classmethod
    async def live_host(cls, session: AsyncSession, actor_id: uuid.UUID, host: str) -> EgressAllowance | None:
        return await session.scalar(
            select(cls).where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.owner_actor_id == actor_id,
                cls.host == host,
                cls.revoked_at.is_(None),
            )
        )

    @classmethod
    async def get_for(cls, session: AsyncSession, allowance_id: uuid.UUID, actor: Actor) -> EgressAllowance:
        row = await session.scalar(
            select(cls).where(
                cls.id == allowance_id,
                cls.workspace_id == tenant.workspace_of(session),
                cls.owner_actor_id == actor.id,
            )
        )
        if row is None:
            raise NotFound("no such allowance")
        return row
