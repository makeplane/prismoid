# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""SkillRecord - one skill version: draft/published/archived, origin, manifest, eval status.

Skills are **data, not deploys**. The body lives in MinIO under its content hash; this row
is the addressable version. The builtins are seeded as v1 at migrate as platform rows (`workspace_id`
NULL) and are never mutated in place; every other version belongs to one workspace.
A workspace resolves its own versions and the builtins, never another workspace's, and cannot draft or
publish under a builtin's name (a 409) - the name means the builtin everywhere.

The doctrine that publishing requires green evals is **an HTTP 409, not a review comment**
- which is only possible because `eval_status` is a column that the publish path reads.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ColumnElement,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    delete,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk, vocabulary
from prismoid.db.models import tenant


class SkillStatus(StrEnum):
    DRAFT = "draft"  # usable only by its author, via `report@draft`
    PUBLISHED = "published"
    ARCHIVED = "archived"


class SkillOrigin(StrEnum):
    BUILTIN = "builtin"
    USER = "user"


class EvalStatus(StrEnum):
    UNTESTED = "untested"
    GREEN = "green"
    RED = "red"


class SkillRecord(Base):
    __tablename__ = "skills_registry"
    __table_args__ = (
        vocabulary("status", SkillStatus),
        vocabulary("origin", SkillOrigin),
        # A workspace's own versions are unique within it; a builtin (workspace_id NULL) is unique platform-
        # wide - NULLs never collide in a plain unique, hence the partial index.
        UniqueConstraint("workspace_id", "name", "version", name="uq_skills_workspace_name_version"),
        Index(
            "ux_skills_builtin_name_version",
            "name",
            "version",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        Index("ix_skills_registry_name_status", "name", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, default=None
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SkillStatus.DRAFT)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default=SkillOrigin.USER)

    manifest: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # Content-addressed body in MinIO. The loader caches by hash, so a pull is a no-op when the
    # bytes have not changed - which is half of the sub-60-second edit loop.
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    body_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    eval_status: Mapped[str] = mapped_column(String(16), nullable=False, default=EvalStatus.UNTESTED)
    eval_report: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    def _readable(cls, session: AsyncSession) -> ColumnElement[bool]:
        """What a workspace resolves: its own versions and the platform builtins
        (`workspace_id` NULL) - never another workspace's. Every product read below rides this."""
        return or_(cls.workspace_id == tenant.workspace_of(session), cls.workspace_id.is_(None))

    @classmethod
    async def exists(cls, session: AsyncSession, name: str, *, version: int) -> bool:
        """Is this (name, version) seeded as a BUILTIN? The seed's idempotency read - platform
        rows only, so it runs in any unit of work (a migrate binds no workspace)."""
        row = await session.scalar(
            select(cls.id).where(cls.name == name, cls.version == version, cls.workspace_id.is_(None))
        )
        return row is not None

    @classmethod
    async def builtin_holds(cls, session: AsyncSession, name: str) -> bool:
        """Does a platform builtin own this name? A workspace can neither draft nor publish under it
        (a 409 at the door) - the name means the builtin in every workspace."""
        row = await session.scalar(
            select(cls.id).where(cls.name == name, cls.workspace_id.is_(None)).limit(1)
        )
        return row is not None

    @classmethod
    async def all_versions(cls, session: AsyncSession, name: str) -> list[SkillRecord]:
        return list(
            await session.scalars(
                select(cls).where(cls.name == name, cls._readable(session)).order_by(cls.version.desc())
            )
        )

    @classmethod
    async def at_version(cls, session: AsyncSession, name: str, version: int) -> SkillRecord | None:
        """One exact (name, version) - what a plan pin resolves to. A running task that pinned
        v1 keeps loading v1 after v2 publishes; that reproducibility is the whole reason pins exist."""
        return await session.scalar(
            select(cls).where(cls.name == name, cls.version == version, cls._readable(session))
        )

    def pinnable_by(self, actor_id: uuid.UUID | None) -> bool:
        """A published version pins for anyone; a draft only for its author, whether it is named
        `@draft` or by its number - and never where there is no author to ask."""
        return self.status != SkillStatus.DRAFT.value or (
            actor_id is not None and self.created_by == actor_id
        )

    @classmethod
    async def latest_draft(cls, session: AsyncSession, name: str, author_id: uuid.UUID) -> SkillRecord | None:
        """The author's newest draft of a skill - what `@draft` pins and `publish` promotes.

        **Author-scoped.** A draft resolves only for whoever wrote it, so a second user's `@draft` finds
        nothing (a 404, not someone else's work). This is the one query behind both "pin my draft" and
        "publish my draft", so the author boundary is defined in exactly one place."""
        return await session.scalar(
            select(cls)
            .where(
                cls.workspace_id
                == tenant.workspace_of(session),  # a draft is the author's, in this workspace
                cls.name == name,
                cls.status == SkillStatus.DRAFT.value,
                cls.created_by == author_id,
            )
            .order_by(cls.version.desc())
            .limit(1)
        )

    @classmethod
    async def delete_builtins(cls, session: AsyncSession) -> None:
        """Drop every `origin=builtin` row so `seed --force` can re-write them. Touches
        only builtins - a user's own versions of a builtin skill are `origin=user` and untouched."""
        await session.execute(delete(cls).where(cls.origin == SkillOrigin.BUILTIN.value))

    @classmethod
    async def published_heads(cls, session: AsyncSession) -> list[SkillRecord]:
        """The highest published version of each skill - the menu a router chooses from.

        Grouped in the database so "the current report skill" is one row, not a list a caller has to
        reduce. A skill with only draft versions does not appear - an unpublished skill is not part
        of the menu, which is the whole point of the draft/publish split.
        """
        readable = cls._readable(session)
        latest = (
            select(cls.name, func.max(cls.version).label("v"))
            .where(cls.status == SkillStatus.PUBLISHED.value, readable)
            .group_by(cls.name)
            .subquery()
        )
        return list(
            await session.scalars(
                select(cls)
                .join(latest, (cls.name == latest.c.name) & (cls.version == latest.c.v))
                .where(readable)  # (name, version) is unique per workspace - rejoin inside what we read
                .order_by(cls.name)
            )
        )

    @classmethod
    async def head(cls, session: AsyncSession, name: str) -> SkillRecord | None:
        """The published version a plan pins when it names this skill."""
        return await session.scalar(
            select(cls)
            .where(cls.name == name, cls.status == SkillStatus.PUBLISHED.value, cls._readable(session))
            .order_by(cls.version.desc())
            .limit(1)
        )
