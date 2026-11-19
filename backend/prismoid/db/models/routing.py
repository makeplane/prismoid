# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""RoutingTable - versioned per-class chain ordering over enabled catalog entries.

The chain is *which model to try first, and what to fall back to*. Versioned so that a routing
change is a diff you can read and revert, not an argument about what production is doing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk


class RoutingTable(Base):
    __tablename__ = "routing_table"
    __table_args__ = (
        UniqueConstraint("version", "capability_class", name="routing_table_key"),
        Index("ix_routing_table_version", "version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    capability_class: Mapped[str] = mapped_column(String(32), nullable=False)

    # Ordered [{provider, model_id}, …]. Chains reference the catalog by (provider, model_id) -
    # never by a model string in code.
    chain: Mapped[list[str]] = mapped_column(nullable=False, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = created_at()

    # The queries live here (no select() outside db/models); operations composes these.
    @classmethod
    async def active_rows(cls, session: AsyncSession) -> list[RoutingTable]:
        return list(await session.scalars(select(cls).where(cls.active.is_(True))))

    @classmethod
    async def active_version(cls, session: AsyncSession) -> int:
        return int(await session.scalar(select(func.max(cls.version)).where(cls.active.is_(True))) or 0)

    @classmethod
    async def max_version(cls, session: AsyncSession) -> int:
        return int(await session.scalar(select(func.max(cls.version))) or 0)

    @classmethod
    async def deactivate_all(cls, session: AsyncSession) -> None:
        await session.execute(update(cls).where(cls.active.is_(True)).values(active=False))
