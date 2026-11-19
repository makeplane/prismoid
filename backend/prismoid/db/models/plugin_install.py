# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""PluginInstall - one Agent Plugins bundle a user installed, and what each part became.

The receipt, made durable: `GET /v1/plugins` answers "what did I install, and where did it go"
without re-reading any directory. Append-only in spirit - re-installing the same plugin writes a
new row (a bundle's contents change between installs, and the old receipt stays true of the old
install). Ownership is personal, like a connector's: an install lands drafts and rows in the
installer's own scope, so the receipt is theirs too.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, keyset_cursor, uuid_pk
from prismoid.db.models import tenant


class PluginInstall(Base):
    __tablename__ = "plugin_installs"
    __table_args__ = (
        Index("ix_plugin_installs_owner_actor_id", "owner_actor_id"),
        Index("ix_plugin_installs_created_at_id", "created_at", "id"),  # keyset only
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # The whole InstallReport - items and notes. JSONB rather than columns because the receipt
    # is read whole or not at all; nothing queries inside it.
    report: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def for_owner(
        cls, session: AsyncSession, actor_id: uuid.UUID, *, limit: int = 50, cursor: str | None = None
    ) -> list[PluginInstall]:
        query = (
            select(cls)
            .where(cls.workspace_id == tenant.workspace_of(session), cls.owner_actor_id == actor_id)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    def cursor(self) -> str:
        return keyset_cursor(self)
