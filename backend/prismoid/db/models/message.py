# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Message - one line of a Conversation (ADR-057): a person's, the bot's, or the machine's.

The record of talk. A USER row wears the door's verdict and its reason (`verdict`,
`verdict_reason`, `facts`, `consent`) - "why did it start a task" and "why didn't it" are
answered here, never guessed. A BOT row may carry a `proposal` (the card). A SYSTEM row
reports the machine: a task started, delivered, failed, or waiting on a gate (`task_id`,
`gate_id`, `artifact_ids`). Dense `(conversation_id, seq)` is the poll cursor; the partial
unique on `provider_message_id` absorbs at-least-once transports.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, func, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_provider",
            "conversation_id",
            "provider_message_id",
            unique=True,
            postgresql_where=sa_text("provider_message_id IS NOT NULL"),
        ),
        Index("ix_messages_task_id", "task_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    sender: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    verdict: Mapped[str] = mapped_column(String(12), nullable=False, default="none", server_default="none")
    verdict_reason: Mapped[str] = mapped_column(String(400), nullable=False, default="", server_default="")
    consent: Mapped[str] = mapped_column(String(12), nullable=False, default="none", server_default="none")
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="text", server_default="text")
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    gate_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    artifact_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="sent", server_default="sent")
    detail: Mapped[str] = mapped_column(String(400), nullable=False, default="", server_default="")
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def since(
        cls, session: AsyncSession, conversation_id: uuid.UUID, *, since: int = 0, limit: int = 200
    ) -> list[Message]:
        stmt = (
            select(cls)
            .where(cls.conversation_id == conversation_id, cls.seq > since)
            .order_by(cls.seq.asc())
            .limit(limit)
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def newest(
        cls, session: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 30
    ) -> list[Message]:
        """The tail, newest first - the reply lane's history read."""
        stmt = select(cls).where(cls.conversation_id == conversation_id).order_by(cls.seq.desc()).limit(limit)
        return list(await session.scalars(stmt))

    @classmethod
    async def before(
        cls, session: AsyncSession, conversation_id: uuid.UUID, seq: int, *, limit: int = 30
    ) -> list[Message]:
        """The rows up to and including `seq`, newest first - the thread prelude's read (bounded on
        the ROW'S OWN seq so it is the same set forever:
        `newest` shrank the prelude as the conversation went on)."""
        stmt = (
            select(cls)
            .where(cls.conversation_id == conversation_id, cls.seq <= seq)
            .order_by(cls.seq.desc())
            .limit(limit)
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def get(cls, session: AsyncSession, conversation_id: uuid.UUID, seq: int) -> Message | None:
        return await session.get(cls, (conversation_id, seq))

    @classmethod
    async def by_provider_id(
        cls, session: AsyncSession, conversation_id: uuid.UUID, provider_message_id: str
    ) -> Message | None:
        stmt = select(cls).where(
            cls.conversation_id == conversation_id, cls.provider_message_id == provider_message_id
        )
        return await session.scalar(stmt)

    @classmethod
    async def pending_proposal(cls, session: AsyncSession, conversation_id: uuid.UUID) -> Message | None:
        """The newest bot row whose proposal card is still waiting."""
        stmt = (
            select(cls)
            .where(
                cls.conversation_id == conversation_id,
                cls.role == "bot",
                cls.proposal.is_not(None),
                cls.proposal["status"].as_string() == "pending",
            )
            .order_by(cls.seq.desc())
            .limit(1)
        )
        return await session.scalar(stmt)

    @classmethod
    async def unanswered_user(cls, session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
        """User rows that still await a reply: verdict answer, status still `sent`. The STATUS
        is the mark (`answered` / `failed` are stamped by the reply lane), never "a bot row
        landed after it" - a line that arrived while the bot was generating, or a task's
        continued-turn delivery (a BOT row too), used to orphan a question forever."""
        stmt = (
            select(cls)
            .where(
                cls.conversation_id == conversation_id,
                cls.role == "user",
                cls.verdict == "answer",
                cls.status == "sent",
            )
            .order_by(cls.seq.asc())
            .limit(50)
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def answered_after(cls, session: AsyncSession, conversation_id: uuid.UUID, seq: int) -> bool:
        """Is the line at `seq` no longer waiting? The reply job's already-done check - read
        off the row's own status, the durable per-message mark."""
        stmt = select(cls.status).where(cls.conversation_id == conversation_id, cls.seq == seq)
        status = await session.scalar(stmt)
        return status is not None and status != "sent"

    @classmethod
    async def today_count_for_channel(cls, session: AsyncSession, channel_id: uuid.UUID) -> int:
        """Inbound lines since midnight UTC across every thread of one channel - the daily
        cap's read (the retired ledger's `today_count`, one join over)."""
        from prismoid.db.models.conversation import Conversation

        midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = (
            select(func.count())
            .select_from(cls)
            .join(Conversation, Conversation.id == cls.conversation_id)
            .where(
                Conversation.channel_id == channel_id,
                cls.role == "user",
                cls.created_at >= midnight,
                # a refused line spent nothing: strangers must not close the owner's door
                cls.verdict != "refused",
            )
        )
        return int(await session.scalar(stmt) or 0)

    @classmethod
    async def attached(
        cls, session: AsyncSession, conversation_id: uuid.UUID, since: int, until: int | None = None
    ) -> list[Message]:
        """The person's lines from `since` (to `until`, if given) that carried files (`meta.inputs`), newest first."""
        stmt = select(cls).where(
            cls.conversation_id == conversation_id,
            cls.role == "user",
            cls.seq >= since,
            cls.meta.has_key("inputs"),
        )
        if until is not None:
            stmt = stmt.where(cls.seq <= until)
        return list(await session.scalars(stmt.order_by(cls.seq.desc()).limit(50)))

    @classmethod
    async def any_input_with_digest(cls, session: AsyncSession, sha256: str) -> bool:
        """Does any conversation line hold this content-addressed blob as a file (`meta.inputs`)? Blob GC's guard."""
        stmt = select(cls.seq).where(cls.meta.contains({"inputs": [{"sha256": sha256}]})).limit(1)
        return await session.scalar(stmt) is not None

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[Message]:
        stmt = select(cls).where(cls.task_id == task_id).order_by(cls.seq.asc())
        return list(await session.scalars(stmt))
