# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BotTemplate - a bot published as an immutable snapshot that travels as a link (ADR-056).

One row per template VERSION, append-only: publishing again under the same slug appends
`template_version + 1`, so what a recipient saw is forever what installs from that link's
history. No project FK (a template outlives the project); `author_actor_id` SET NULL with a
byline snapshot (a template outlives its author). The slug is plaintext by design - a
capability for broadcast content, never a credential (schemas/ids.py).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, SmallInteger, String, Text, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, keyset_cursor, uuid_pk
from prismoid.schemas.bot_templates import TemplateStatus, TemplateVisibility


class BotTemplate(Base):
    __tablename__ = "bot_templates"
    __table_args__ = (
        Index("uq_bot_templates_slug_version", "share_slug", "template_version", unique=True),
        Index("ix_bot_templates_author_actor_id", "author_actor_id"),
        Index("ix_bot_templates_created_at_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    share_slug: Mapped[str] = mapped_column(String(48), nullable=False)
    template_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TemplateStatus.PUBLISHED, server_default="published"
    )
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TemplateVisibility.UNLISTED, server_default="unlisted"
    )
    author_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")

    # The snapshot: the exact bytes the plugin export writes, plus a parsed manifest (cases
    # included) as the cheap read model the preview serves without re-parsing.
    bot_md: Mapped[str] = mapped_column(Text, nullable=False)
    cases_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # The face travels (never the pin - sidebar presence is the installer's own act).
    icon: Mapped[str | None] = mapped_column(String(80), default=None)
    icon_hue: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    title: Mapped[str | None] = mapped_column(String(120), default=None)

    source_bot_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    install_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def head(cls, session: AsyncSession, slug: str) -> BotTemplate | None:
        """The slug's newest PUBLISHED version - what the anon preview serves and what installs.
        Unlisted and removed rows never come back from here: the link honestly 404s."""
        stmt = (
            select(cls)
            .where(cls.share_slug == slug, cls.status == TemplateStatus.PUBLISHED.value)
            .order_by(cls.template_version.desc())
            .limit(1)
        )
        return await session.scalar(stmt)

    @classmethod
    async def at_version(cls, session: AsyncSession, slug: str, version: int) -> BotTemplate | None:
        stmt = select(cls).where(cls.share_slug == slug, cls.template_version == version)
        return await session.scalar(stmt)

    @classmethod
    async def head_version(cls, session: AsyncSession, slug: str) -> int:
        """The newest version number under a slug, any status - the append counter's read."""
        stmt = select(func.max(cls.template_version)).where(cls.share_slug == slug)
        return int(await session.scalar(stmt) or 0)

    @classmethod
    async def for_author(
        cls, session: AsyncSession, author_actor_id: uuid.UUID, *, limit: int = 50, cursor: str | None = None
    ) -> list[BotTemplate]:
        """The author's own templates, newest first, keyset-paged - "Shared by you"."""
        stmt = (
            select(cls)
            .where(cls.author_actor_id == author_actor_id)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            stmt = stmt.where(keyset_before(cls, cursor))
        return list(await session.scalars(stmt))

    @classmethod
    async def all_for_slug(
        cls, session: AsyncSession, slug: str, *, author_actor_id: uuid.UUID | None = None
    ) -> list[BotTemplate]:
        """Every version under a slug, any status - the unlist verbs sweep the whole family.
        With `author_actor_id`, only that author's rows (the owner's kill switch)."""
        stmt = select(cls).where(cls.share_slug == slug)
        if author_actor_id is not None:
            stmt = stmt.where(cls.author_actor_id == author_actor_id)
        return list(await session.scalars(stmt))

    @classmethod
    async def slug_for_author(
        cls, session: AsyncSession, author_actor_id: uuid.UUID, name: str
    ) -> str | None:
        """The author's existing slug for a bot name, any status - so a republish is an update,
        not a fork. A direct predicate, never a page of the author's rows filtered in Python
        (found in review: past 100 version rows a republish forked a second slug)."""
        stmt = (
            select(cls.share_slug)
            .where(cls.author_actor_id == author_actor_id, cls.name == name)
            .order_by(cls.template_version.desc())
            .limit(1)
        )
        return await session.scalar(stmt)

    @classmethod
    async def heads_for_author(cls, session: AsyncSession, author_actor_id: uuid.UUID) -> list[BotTemplate]:
        """The newest version per slug the author published - "Shared by you", any status."""
        stmt = (
            select(cls)
            .where(cls.author_actor_id == author_actor_id)
            .distinct(cls.share_slug)
            .order_by(cls.share_slug, cls.template_version.desc())
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def by_ids(cls, session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, BotTemplate]:
        """One IN-query for a set of rows (the `User.by_actor_ids` shape)."""
        if not ids:
            return {}
        rows = await session.scalars(select(cls).where(cls.id.in_(ids)))
        return {r.id: r for r in rows}

    @classmethod
    async def head_versions(cls, session: AsyncSession, slugs: list[str]) -> dict[str, int]:
        """The newest version number per slug, any status, in one grouped query."""
        if not slugs:
            return {}
        stmt = (
            select(cls.share_slug, func.max(cls.template_version))
            .where(cls.share_slug.in_(slugs))
            .group_by(cls.share_slug)
        )
        return {str(slug): int(version or 0) for slug, version in (await session.execute(stmt)).all()}

    @classmethod
    async def bump_install_count(cls, session: AsyncSession, template_id: uuid.UUID) -> None:
        """Atomic - two installs racing both count (the caller's transaction carries it)."""
        await session.execute(
            update(cls).where(cls.id == template_id).values(install_count=cls.install_count + 1)
        )

    def cursor(self) -> str:
        return keyset_cursor(self)
