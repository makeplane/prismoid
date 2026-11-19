# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BotTemplateInstall - the receipt one click leaves (ADR-056), the `plugin_installs` twin.

Points at the EXACT template version installed, which is what makes the update chip a read:
the installer's newest receipt's version against the slug's head version, computed at read
time, no event, no knock.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk
from prismoid.db.models import tenant


class BotTemplateInstall(Base):
    __tablename__ = "bot_template_installs"
    __table_args__ = (
        Index("ix_bot_template_installs_installer", "installer_actor_id"),
        Index("ix_bot_template_installs_template", "template_id"),
        Index("ix_bot_template_installs_created_at_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bot_templates.id", ondelete="CASCADE"), nullable=False
    )
    installer_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False
    )
    bot_name: Mapped[str] = mapped_column(String(64), nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def for_installer(
        cls, session: AsyncSession, installer_actor_id: uuid.UUID, *, limit: int = 100
    ) -> list[BotTemplateInstall]:
        stmt = (
            select(cls)
            .where(
                cls.workspace_id == tenant.workspace_of(session), cls.installer_actor_id == installer_actor_id
            )
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        return list(await session.scalars(stmt))
