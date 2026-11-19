# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BotChannel - one external address, bound to one bot (ADR-056).

The addressable unit of the reach pillar: the webhook kind carries a hashed `bc_` token, the
provider kinds carry their own secrets (Fernet in `config_enc` where a BYO token must be
read back, hashes where only equality matters). Caps and the allowlist live on the row -
policy as data, enforced at the door.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, Numeric, String, Text, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from prismoid.db.base import Base, created_at, uuid_pk
from prismoid.db.models import tenant
from prismoid.schemas.channels import ChannelKind, ChannelStatus


class BotChannel(Base):
    __tablename__ = "bot_channels"
    __table_args__ = (
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project still takes its rows with it (CASCADE, as before).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_bot_channels_project_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_bot_channels_owner", "owner_actor_id"),
        Index("ix_bot_channels_bot", "bot_name", "owner_actor_id"),
        Index(
            "uq_bot_channels_kind_address",
            "kind",
            "address",
            unique=True,
            postgresql_where=sa_text("address IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    bot_name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChannelStatus.ACTIVE, server_default="active"
    )
    token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    inbound_secret_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    config_enc: Mapped[str | None] = mapped_column(Text, default=None)
    key_id: Mapped[str | None] = mapped_column(String(16), default=None)
    input_mode: Mapped[str] = mapped_column(String(12), nullable=False, default="data", server_default="data")
    budget_usd_per_turn: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, default=1.0, server_default="1.0"
    )
    daily_message_cap: Mapped[int] = mapped_column(nullable=False, default=100, server_default="100")
    # May this channel's NON-owner senders reach the door's work rungs (propose,
    # start under the bot's grant)? Default false - an outsider talks; the owner decides work.
    work_grant: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    last_message_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @classmethod
    async def for_owner(cls, session: AsyncSession, owner_actor_id: uuid.UUID) -> list[BotChannel]:
        stmt = (
            select(cls)
            .where(cls.workspace_id == tenant.workspace_of(session), cls.owner_actor_id == owner_actor_id)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(100)
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def get_for(
        cls, session: AsyncSession, channel_id: uuid.UUID, owner_actor_id: uuid.UUID
    ) -> BotChannel | None:
        """The owner's row, in the bound workspace - None for anyone else's and for the owner's own
        channel of another workspace. The machine doors (bc_ token, email webhook) read by
        id and bind the channel's workspace instead."""
        return await session.scalar(
            select(cls).where(
                cls.id == channel_id,
                cls.workspace_id == tenant.workspace_of(session),
                cls.owner_actor_id == owner_actor_id,
            )
        )

    @classmethod
    async def for_bot(
        cls, session: AsyncSession, owner_actor_id: uuid.UUID, bot_name: str
    ) -> list[BotChannel]:
        stmt = (
            select(cls)
            .where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.owner_actor_id == owner_actor_id,
                cls.bot_name == bot_name,
            )
            .order_by(cls.created_at.desc(), cls.id.desc())
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def all_active(cls, session: AsyncSession) -> list[BotChannel]:
        """Every active channel - the minute sweep's read."""
        stmt = select(cls).where(cls.status == ChannelStatus.ACTIVE.value)
        return list(await session.scalars(stmt))

    @classmethod
    async def by_address(cls, session: AsyncSession, kind: str, address: str) -> BotChannel | None:
        stmt = select(cls).where(cls.kind == kind, cls.address == address.strip().lower())
        return await session.scalar(stmt)

    @classmethod
    async def locked(cls, session: AsyncSession, channel_id: uuid.UUID) -> BotChannel | None:
        """The row, FOR UPDATE - the ledger's seq counter serializes on this lock."""
        return await session.scalar(select(cls).where(cls.id == channel_id).with_for_update())

    def allowlist(self) -> list[str]:
        return [str(s).lower() for s in (self.config or {}).get("allowlist", [])]

    def is_active(self) -> bool:
        return self.status == ChannelStatus.ACTIVE.value and self.kind in {k.value for k in ChannelKind}
