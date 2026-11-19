# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BotRecord - one bot version: a user-authored agent as a row. draft/published/archived.

Bots are **data, not deploys** - the move `skills_registry` made for judgment, made for
agents. `(scope, name, version)` is how a plan pins one, so publishing v2 never changes what a
running task is doing.

**Scope is the one thing this table has that `skills_registry` does not.** A bot belongs to a
Project (shared with its members under the role matrix) or to nobody but its author (`project_id IS
NULL` - personal). Two partial unique indexes carry that: within a project a name is unique, and a
personal name is unique per owner. Postgres treats NULLs as distinct, so one plain constraint
would let one author hold two personal `reviewer` v1 rows while also colliding two authors'
personal bots across the whole install - both wrong, in opposite directions.

Publishing requires green evals, and that is **an HTTP 409, not a review comment** - only
possible because `eval_status` is a column the publish path reads.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ColumnElement,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, keyset_cursor, uuid_pk, vocabulary
from prismoid.db.models import tenant


class BotStatus(StrEnum):
    DRAFT = "draft"  # usable only by its author, via `reviewer@draft`
    PUBLISHED = "published"
    ARCHIVED = "archived"


class BotOrigin(StrEnum):
    """Where the row came from. No builtins today - the built-in roster stays code (`all.py`)."""

    USER = "user"
    IMPORTED = "imported"  # from an Agent Plugin directory; provenance, never a privilege


class BotRecord(Base):
    __tablename__ = "bots_registry"
    __table_args__ = (
        vocabulary("status", BotStatus),
        vocabulary("origin", BotOrigin),
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project still takes its rows with it (CASCADE, as before).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_bots_registry_project_workspace",
            ondelete="CASCADE",
        ),
        # Within a project, a name is one bot. See the module docstring for why this is partial.
        Index(
            "uq_bots_registry_project_name_version",
            "project_id",
            "name",
            "version",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
        # A personal bot (project_id IS NULL) is unique per owner within a workspace - two people may
        # both have a `reviewer`, and neither may have two in one workspace (a person's bots
        # in one workspace are invisible from another, so the name is free there).
        Index(
            "uq_bots_registry_personal_name_version",
            "workspace_id",
            "owner_actor_id",
            "name",
            "version",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
        ),
        Index("ix_bots_registry_project_id_status", "project_id", "status"),
        Index("ix_bots_registry_owner_actor_id", "owner_actor_id"),
        Index("ix_bots_registry_created_at_id", "created_at", "id"),  # keyset only
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=BotStatus.DRAFT)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default=BotOrigin.USER)

    # The author. NOT NULL and CASCADE: unlike a skill (where `created_by` is attribution and a
    # departed author leaves the skill standing), a bot's draft lane is author-scoped, so a row
    # whose owner is gone has no one who can publish it.
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False
    )
    # NULL ⇒ personal to the owner. Set ⇒ visible to that Project's members.
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    manifest: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    # The instructions, inline. A skill body lives in MinIO under its hash because a skill is a
    # BUNDLE - a body plus scripts, references and assets up to 256 KiB, copied into a frame on
    # every step. A bot is a manifest and ≤5,000 tokens of judgment with no files, read once per
    # step; the object store would buy only dedup and cost a bucket, a loader, a cache and a
    # fetch path. Boring beats clever.
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Kept even though the bytes are right here: it is what makes `push` a no-op when nothing
    # changed, and what `bots diff` compares without reading two blobs.
    instructions_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    eval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="untested")
    eval_report: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    # Free-text provenance for an imported bot - the plugin it came from. Never a privilege.
    source: Mapped[str | None] = mapped_column(Text, default=None)

    # The bot's face (the Project precedent): `lucide:<slug>` / `emoji:<char>` stored verbatim -
    # the server never interprets it - and a hue index into the web's categorical ramp.
    # PER BOT, not per version: mutable columns kept identical across the name's rows by
    # `operations/bots.set_face`, deliberately outside the manifest that versions immutably.
    icon: Mapped[str | None] = mapped_column(String(80), default=None)
    icon_hue: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    # The face's third piece: a human title ("Contracts desk"), per name like the
    # icon - never the routing surface, which stays the evaled description.
    title: Mapped[str | None] = mapped_column(String(120), default=None)

    # The work dials, per name like the face: may the bot start work by itself
    # from a conversation (ask | auto | never), and what one chat-born task may cost. `auto`
    # is the default (see ADR-060): the owner's cap is the consent; strangers, over-cap
    # work and service writes still ask.
    work_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="auto", server_default="auto")
    task_budget_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, default=2.0, server_default="2.0"
    )
    # The threads dial, per name like the work dials: `one` - the bot IS its one
    # conversation (no list, no New chat; a second create returns the same row) - or `many`,
    # a chat per topic. A setting, never authored judgment.
    threads: Mapped[str] = mapped_column(String(8), nullable=False, default="many", server_default="many")

    # Sidebar presence (the messenger's pin law): a bot shows in the left nav ONLY when pinned.
    # Per-bot like the face - kept identical across a name's versions by `set_face`.
    pinned: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")

    created_at: Mapped[dt.datetime] = created_at()

    # ── scope ─────────────────────────────────────────────────────────────────────────────────

    @classmethod
    def in_scope(cls, *, project_id: uuid.UUID | None, owner_actor_id: uuid.UUID) -> ColumnElement[bool]:
        """The one place "which bots does this name mean" is decided.

        A project scope is the project's bots - every member sees the same set, which is what makes a
        bot shareable. No project means the actor's own personal bots. Defined once so a caller
        cannot accidentally widen it, and so the two partial unique indexes above stay the exact
        mirror of this predicate.
        """
        if project_id is not None:
            return cls.project_id == project_id
        return (cls.project_id.is_(None)) & (cls.owner_actor_id == owner_actor_id)

    @classmethod
    def _visible(
        cls, session: AsyncSession, *, project_id: uuid.UUID | None, owner_actor_id: uuid.UUID
    ) -> ColumnElement[bool]:
        """`in_scope`, inside the bound workspace - what every product fetch path
        reads. The operator console alone rides the bare `in_scope` (cross-workspace by design)."""
        return (cls.workspace_id == tenant.workspace_of(session)) & cls.in_scope(
            project_id=project_id, owner_actor_id=owner_actor_id
        )

    # ── fetch paths ───────────────────────────────────────────────────────────────────────────

    @classmethod
    async def all_versions(
        cls, session: AsyncSession, name: str, *, project_id: uuid.UUID | None, owner_actor_id: uuid.UUID
    ) -> list[BotRecord]:
        return list(
            await session.scalars(
                select(cls)
                .where(
                    cls.name == name,
                    cls._visible(session, project_id=project_id, owner_actor_id=owner_actor_id),
                )
                .order_by(cls.version.desc())
            )
        )

    @classmethod
    async def at_version(
        cls,
        session: AsyncSession,
        name: str,
        version: int,
        *,
        project_id: uuid.UUID | None,
        owner_actor_id: uuid.UUID,
    ) -> BotRecord | None:
        """One exact (scope, name, version) - what a plan pin resolves to. A running task that
        pinned v1 keeps loading v1 after v2 publishes; that reproducibility is why pins exist."""
        return await session.scalar(
            select(cls).where(
                cls.name == name,
                cls.version == version,
                cls._visible(session, project_id=project_id, owner_actor_id=owner_actor_id),
            )
        )

    @classmethod
    async def head(
        cls, session: AsyncSession, name: str, *, project_id: uuid.UUID | None, owner_actor_id: uuid.UUID
    ) -> BotRecord | None:
        """The published version a plan pins when it names this bot."""
        return await session.scalar(
            select(cls)
            .where(
                cls.name == name,
                cls.status == BotStatus.PUBLISHED.value,
                cls._visible(session, project_id=project_id, owner_actor_id=owner_actor_id),
            )
            .order_by(cls.version.desc())
            .limit(1)
        )

    def pinnable_by(self, actor_id: uuid.UUID) -> bool:
        """A published version pins for anyone in scope; a draft only for its author, whether it
        is named `@draft` or by its number - a co-editor's workbench is not the Project's."""
        return self.status != BotStatus.DRAFT.value or self.owner_actor_id == actor_id

    @classmethod
    async def latest_draft(
        cls,
        session: AsyncSession,
        name: str,
        author_id: uuid.UUID,
        *,
        project_id: uuid.UUID | None,
    ) -> BotRecord | None:
        """The author's newest draft - what `@draft` pins and `publish` promotes.

        **Author-scoped, even inside a shared project.** An editor may push their own draft of a
        project bot without a co-editor's half-finished draft resolving for them; the project is what
        the *published* head is shared through, not the workbench. One query behind both "pin my
        draft" and "publish my draft", so the author boundary is defined in exactly one place.
        """
        query = select(cls).where(
            cls.workspace_id == tenant.workspace_of(session),
            cls.name == name,
            cls.status == BotStatus.DRAFT.value,
            cls.owner_actor_id == author_id,
        )
        query = (
            query.where(cls.project_id == project_id)
            if project_id is not None
            else query.where(cls.project_id.is_(None))
        )
        return await session.scalar(query.order_by(cls.version.desc()).limit(1))

    @classmethod
    async def published_heads(
        cls, session: AsyncSession, *, project_id: uuid.UUID | None, owner_actor_id: uuid.UUID
    ) -> list[BotRecord]:
        """The roster: the highest published version of every bot this task can see.

        A project task sees the project's bots **and** the actor's personal ones - the union is
        deliberate. A bot you built for yourself should not vanish because you filed the task in
        a folder; and a folder's bots should not follow you out of it. Grouped in the database so
        "the current reviewer" is one row, not a list the caller has to reduce.

        A bot with only draft versions does not appear. That is the whole point of the split.
        """
        scope = cls.in_scope(project_id=project_id, owner_actor_id=owner_actor_id)
        if project_id is not None:
            scope = scope | ((cls.project_id.is_(None)) & (cls.owner_actor_id == owner_actor_id))
        workspace = tenant.workspace_of(session)  # the roster is the bound workspace's
        # The scope INSIDE the max: another owner's higher personal version must never outrank
        # (and so hide) this owner's own head.
        published = (
            select(cls)
            .where(cls.workspace_id == workspace, cls.status == BotStatus.PUBLISHED.value, scope)
            .subquery()
        )
        latest = (
            select(published.c.project_id, published.c.name, func.max(published.c.version).label("v"))
            .group_by(published.c.project_id, published.c.name)
            .subquery()
        )
        return list(
            await session.scalars(
                select(cls)
                .join(
                    latest,
                    (cls.name == latest.c.name)
                    & (cls.version == latest.c.v)
                    & (cls.project_id.is_not_distinct_from(latest.c.project_id)),
                )
                # The scope again on the outer rows: the join keys (project, name, version) match a
                # DIFFERENT owner's personal bot of the same name.
                .where(cls.workspace_id == workspace, cls.status == BotStatus.PUBLISHED.value, scope)
                .order_by(cls.name)
            )
        )

    @classmethod
    async def listing(
        cls,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | None,
        owner_actor_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[BotRecord]:
        """Every version in scope, newest first - what `GET /v1/bots` pages through."""
        query = (
            select(cls)
            .where(cls._visible(session, project_id=project_id, owner_actor_id=owner_actor_id))
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    # ── the operator's queries (admin surface) ────────────────────────────────────────────────
    # Whole-population reads for the console (operations/admin_ops law 1): every bot NAME's
    # newest row per scope (owner, project, name), whoever authored it. SQL here, policy there.

    @classmethod
    def _admin_heads(cls, status: str | None = None):
        """Subquery: the newest version per (owner, project, name) - one row per bot.

        `status` filters BEFORE the max, deliberately: "published" must mean "this bot's newest
        PUBLISHED version", not "bots whose current head happens to be published" - a bot with a
        draft ahead of its published head is still a published bot, and hiding it from the
        Published view misled the first live look."""
        query = select(
            cls.owner_actor_id.label("o"),
            cls.project_id.label("sp"),
            cls.name.label("n"),
            func.max(cls.version).label("v"),
        )
        if status:
            query = query.where(cls.status == status)
        return query.group_by(cls.owner_actor_id, cls.project_id, cls.name).subquery()

    @classmethod
    async def admin_page(
        cls,
        session: AsyncSession,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[tuple[BotRecord, Any]]:
        """(head row, author User | None), newest first, across every owner and project."""
        from prismoid.db.models.user import User

        heads = cls._admin_heads(status)
        query = (
            select(cls, User)
            .join(
                heads,
                (cls.owner_actor_id == heads.c.o)
                & (
                    func.coalesce(cls.project_id, cls.owner_actor_id)
                    == func.coalesce(heads.c.sp, cls.owner_actor_id)
                )
                & (cls.name == heads.c.n)
                & (cls.version == heads.c.v),
            )
            .join(User, User.actor_id == cls.owner_actor_id, isouter=True)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if q:
            from prismoid.db.base import escape_like

            query = query.where(cls.name.ilike(f"%{escape_like(q)}%", escape="\\"))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return [(b, u) for b, u in (await session.execute(query)).tuples().all()]

    @classmethod
    async def admin_versions(
        cls, session: AsyncSession, owner_actor_id: uuid.UUID, name: str, project_id: uuid.UUID | None
    ) -> list[BotRecord]:
        """Every version of one bot, newest first - the admin detail's history."""
        return list(
            await session.scalars(
                select(cls)
                .where(cls.in_scope(project_id=project_id, owner_actor_id=owner_actor_id), cls.name == name)
                .order_by(cls.version.desc())
            )
        )

    def cursor(self) -> str:
        return keyset_cursor(self)
