# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Conversation - one thread of talk between a person and one bot (ADR-057).

Talk, never work: a Conversation executes nothing. Its rows are cheap and many ("New chat" per
topic - the named-threads valve, here by construction); work it needs mints
an ordinary Task carrying `origin_conversation_id`. A channel-backed conversation is the same
row with `channel_id` + `thread_key`: the channel is the transport, the conversation is the
thread. `message_seq` is the dense counter (the tasks.event_seq grammar); `replying` +
`replying_since` are the reply lane's LEASE so two replies never race out of
order and a dead worker's claim frees itself.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, String, or_, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql import func

from prismoid.db.base import Base, created_at, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project
from prismoid.errors import NotFound

if TYPE_CHECKING:
    from prismoid.db.models.message import Message


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project still takes its rows with it (CASCADE, as before).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_conversations_project_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_conversations_owner_bot", "owner_actor_id", "bot_name", "last_message_at"),
        Index(
            "uq_conversations_channel_thread",
            "channel_id",
            "thread_key",
            unique=True,
            postgresql_where=sa_text("channel_id IS NOT NULL"),
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
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="web", server_default="web")
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bot_channels.id", ondelete="SET NULL"), default=None
    )
    thread_key: Mapped[str] = mapped_column(
        String(120), nullable=False, default="main", server_default="main"
    )
    # The last external sender (a channel thread's reply-to); empty for web conversations.
    last_sender: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    message_seq: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    replying: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    replying_since: Mapped[dt.datetime | None] = mapped_column(default=None)
    unread: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    last_message_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @classmethod
    async def get_for(cls, session: AsyncSession, conversation_id: uuid.UUID, actor: Actor) -> Conversation:
        """The owner's read, in the bound workspace - 404s anyone else (existence is privileged, like a
        memory row), and the owner's own thread of another workspace too. A project
        conversation also needs the owner's seat in the project, re-checked on every use."""
        row = await session.scalar(
            select(cls).where(cls.id == conversation_id, cls.workspace_id == tenant.workspace_of(session))
        )
        if row is None or row.owner_actor_id != actor.id:
            raise NotFound("no such conversation")
        await row.require_seat(session, actor)
        return row

    async def require_seat(self, session: AsyncSession, actor: Actor) -> None:
        """A project conversation is its owner's only while they hold a role in the project: the
        bot's shared memory and its spend are the project's, so a removed member loses the bot
        (404, as a stranger). A personal conversation needs nothing more."""
        if self.project_id is None:
            return
        try:
            await Project.get_for(session, self.project_id, actor)
        except NotFound:
            raise NotFound("no such conversation") from None

    async def seated_owner(self, session: AsyncSession) -> Actor:
        """The owner, still holding their seat (`require_seat`): the actor the channel door and the
        reply lane act as. A removed member's channel refuses, and a line they left goes unanswered."""
        owner = await session.get(Actor, self.owner_actor_id)
        if owner is None:
            raise NotFound("the conversation's owner is gone")
        try:
            await self.require_seat(session, owner)
        except NotFound:
            raise NotFound("the conversation's owner is no longer in its project") from None
        return owner

    @classmethod
    async def for_bot(
        cls,
        session: AsyncSession,
        owner_actor_id: uuid.UUID,
        bot_name: str,
        *,
        project_id: uuid.UUID | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[Conversation]:
        scope = cls.project_id == project_id if project_id is not None else cls.project_id.is_(None)
        stmt = select(cls).where(
            cls.workspace_id == tenant.workspace_of(session),
            cls.owner_actor_id == owner_actor_id,
            cls.bot_name == bot_name,
            scope,
        )
        if not include_archived:
            stmt = stmt.where(cls.archived_at.is_(None))
        stmt = stmt.order_by(cls.last_message_at.desc().nulls_last(), cls.created_at.desc()).limit(limit)
        return list(await session.scalars(stmt))

    @classmethod
    async def for_channel_thread(
        cls, session: AsyncSession, channel_id: uuid.UUID, thread_key: str
    ) -> Conversation | None:
        stmt = select(cls).where(cls.channel_id == channel_id, cls.thread_key == thread_key)
        return await session.scalar(stmt)

    @classmethod
    async def locked(cls, session: AsyncSession, conversation_id: uuid.UUID) -> Conversation | None:
        return await session.scalar(select(cls).where(cls.id == conversation_id).with_for_update())

    def owners_own(self, lines: Sequence[Message]) -> bool:
        """Is every one of these lines the owner's? Off a channel always (the app's composer is
        hers); on one, only the lines the door judged hers. A stranger's words never pick her
        private memories for a reply they read, and never teach her bot (data)."""
        return self.channel_id is None or all((m.facts or {}).get("sender_is_owner") is True for m in lines)

    async def next_seq(self, session: AsyncSession) -> int:
        """Dense, gap-free: one UPDATE ... RETURNING under the row's own lock."""
        result = await session.execute(
            update(Conversation)
            .where(Conversation.id == self.id)
            .values(message_seq=Conversation.message_seq + 1)
            .returning(Conversation.message_seq)
        )
        seq = int(result.scalar_one())
        self.message_seq = seq
        return seq

    @classmethod
    async def claim_reply(
        cls, session: AsyncSession, conversation_id: uuid.UUID, *, lease_s: float
    ) -> dt.datetime | None:
        """Compare-and-set with a LEASE: the claim's stamp if this caller now holds
        the reply lane - it was free, or its holder's claim is older than `lease_s` (a worker
        that died mid-reply never releases; a claim with no timestamp predates the lease and
        reads stale) - else None. Postgres now() is the time authority (the routines gate's
        law): two workers' clocks never decide whose claim is stale. The stamp is the holder's
        proof for `touch_reply` and `release_reply`."""
        stale = func.now() - dt.timedelta(seconds=lease_s)
        result = await session.execute(
            update(cls)
            .where(
                cls.id == conversation_id,
                or_(cls.replying.is_(False), cls.replying_since.is_(None), cls.replying_since < stale),
            )
            .values(replying=True, replying_since=func.now())
            .returning(cls.replying_since)
        )
        return result.scalar_one_or_none()

    @classmethod
    async def touch_reply(
        cls, session: AsyncSession, conversation_id: uuid.UUID, *, since: dt.datetime
    ) -> dt.datetime | None:
        """Renew the lease between two replies under one claim (found in review: a claim taken
        once and never renewed was stolen mid-generation on the second batch). Returns the new
        stamp, or None when the claim is no longer ours."""
        result = await session.execute(
            update(cls)
            .where(cls.id == conversation_id, cls.replying_since == since)
            .values(replying_since=func.now())
            .returning(cls.replying_since)
        )
        return result.scalar_one_or_none()

    @classmethod
    async def release_reply(
        cls, session: AsyncSession, conversation_id: uuid.UUID, *, since: dt.datetime | None = None
    ) -> None:
        """Free the lane - only OUR claim when `since` is given (compare-and-release), so a
        holder that lost its lease never frees the next holder's."""
        stmt = update(cls).where(cls.id == conversation_id)
        if since is not None:
            stmt = stmt.where(cls.replying_since == since)
        await session.execute(stmt.values(replying=False, replying_since=None))

    async def bump_unread(self, session: AsyncSession) -> None:
        """`unread + 1` as a SQL expression - never a Python read-modify-write on an identity-map
        row (found in review: a worker's stale count overwrote the owner's reset to 0)."""
        result = await session.execute(
            update(Conversation)
            .where(Conversation.id == self.id)
            .values(unread=Conversation.unread + 1)
            .returning(Conversation.unread)
            .execution_options(synchronize_session=False)
        )
        # The mirror is the database's answer, set as COMMITTED state: no attribute history, so
        # the next flush never writes a Python count over the counter (found in review: the
        # ORM's own synchronize plus a mirror assignment bumped every row by two).
        set_committed_value(self, "unread", int(result.scalar_one()))
