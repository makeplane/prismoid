# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Memory - one remembered sentence in the context ledger: scoped, attributed, supersedable.

Three scopes, one table. `scope` names the lane and a CHECK constraint pins exactly one target
column per scope - a user's private memory (`user_id`), a project's shared note (`project_id`), or a
skill's usage note (`skill_name`; the name is the stable identity - registry rows are versioned).
**Automation is private; sharing is deliberate**: memory_extract mints private rows only, so
every shared row was typed by a person and says who (`created_by_actor_id`).

Rows are never deleted. Consolidation **supersedes** (`superseded_by` keeps the lineage), people
**retire** - both leave the audit trail intact, exactly like plan edits. `use_count`/`last_used_at`
are the decay signal: assembly bumps them, the ledger shows them, and the per-scope cap retires
the least-recently-used first.

Retrieval v1 is Postgres FTS (`text_tsv`, english-stemmed, recall-biased OR-queries) + recency.
That carries a capped scope (≤{ACTIVE_CAP} active rows) comfortably; the pgvector hybrid upgrade
and its triggers are pre-planned in docs/adr/011-pgvector-retrieval.md.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from collections.abc import Iterator
from enum import StrEnum
from itertools import groupby
from typing import Final, TypedDict

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    select,
    update,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import ColumnElement

from prismoid.db.base import Base, created_at, escape_like, keyset_before, keyset_cursor, uuid_pk, vocabulary
from prismoid.db.models import tenant


class MemoryScope(StrEnum):
    USER = "user"  # private - follows the person across every project
    PROJECT = "project"  # shared with the project's members, human-authored
    SKILL = "skill"  # platform-wide usage notes beside a skill, human-authored
    # A bot's standing state. Keyed by NAME like the skill scope - registry rows
    # are versioned, the name is the stable identity - plus the carrier pair: a personal bot's
    # rows ride user_id = owner, a project bot's ride project_id, mirroring bots_registry's two
    # partial unique indexes. Erasure and project deletion cascade with the carrier for free.
    BOT = "bot"


class MemoryKind(StrEnum):
    INSTRUCTION = "instruction"  # "always/never …" - the user said so
    FACT = "fact"  # stable truth about the user or their workspace
    PREFERENCE = "preference"  # tone, format, length
    NOTE = "note"  # everything else worth keeping


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # replaced by a newer row (consolidation lineage)
    RETIRED = "retired"  # removed by a person or the per-scope cap


ACTIVE_CAP: Final = 200  # active rows per scope target - the rot ceiling
TEXT_MAX: Final = 500

_TSQUERY_TERMS: Final = 24


def words_of(text: str) -> Iterator[str]:
    """The lowercased words of `text` in any script: runs of letters, marks (a Hindi vowel sign)
    and digits - nothing else, so every word is a safe tsquery operand."""
    for is_word, chars in groupby(text.lower(), key=lambda c: unicodedata.category(c)[0] in "LMN"):
        if is_word:
            yield "".join(chars)


def tsquery_of(q: str) -> str:
    """A goal as a recall-biased OR-tsquery: `words_of` >=3 chars, deduped, capped at {_TSQUERY_TERMS} terms;
    the regconfig owns stemming and stopwords, this only tokenises."""
    seen: dict[str, None] = {}
    for word in words_of(q):
        if len(word) >= 3 and word not in seen:
            seen[word] = None
        if len(seen) >= _TSQUERY_TERMS:
            break
    return " | ".join(seen)


class MemoryOwner(TypedDict, total=False):
    """Whose ledger a query reads, as the keyword arguments `Memory`'s queries take: the person or
    the Project."""

    user_id: uuid.UUID
    project_id: uuid.UUID


class MemoryCarrier(MemoryOwner, total=False):
    """A ledger's owner and, on a bot lane, the bot's name."""

    bot_name: str


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        vocabulary("kind", MemoryKind),
        vocabulary("status", MemoryStatus),
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project still takes its rows with it (CASCADE, as before).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_memories_project_workspace",
            ondelete="CASCADE",
        ),
        # Exactly one target column per scope - the lane cannot be ambiguous on any row.
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL AND project_id IS NULL AND skill_name IS NULL"
            " AND bot_name IS NULL)"
            " OR (scope = 'project' AND project_id IS NOT NULL AND user_id IS NULL AND skill_name IS NULL"
            " AND bot_name IS NULL)"
            " OR (scope = 'skill' AND skill_name IS NOT NULL AND user_id IS NULL AND project_id IS NULL"
            " AND bot_name IS NULL)"
            " OR (scope = 'bot' AND bot_name IS NOT NULL AND skill_name IS NULL"
            " AND ((user_id IS NOT NULL) <> (project_id IS NOT NULL)))",
            name="scope_target",
        ),
        Index("ix_memories_text_tsv", "text_tsv", postgresql_using="gin"),
        # One index per lane, in the ledger's sort order. The target column is the leading key -
        # equality-bound on every query - so a lane's cost is O(page), never O(all memories):
        # a shared `(scope, user_id, project_id, …)` composite left its non-leading targets
        # unbounded and scanned every row of the scope. `IS NOT NULL` keeps the other lanes'
        # rows out of each btree; a strict `= $1` (queries and FK enforcement alike) still
        # matches the predicate, so actor/project deletes stay index-backed too.
        Index(
            "ix_memories_user_lane",
            "user_id",
            "status",
            "created_at",
            "id",
            postgresql_where=sa_text("user_id IS NOT NULL"),
        ),
        Index(
            "ix_memories_project_lane",
            "project_id",
            "status",
            "created_at",
            "id",
            postgresql_where=sa_text("project_id IS NOT NULL"),
        ),
        Index(
            "ix_memories_skill_lane",
            "skill_name",
            "status",
            "created_at",
            "id",
            postgresql_where=sa_text("skill_name IS NOT NULL"),
        ),
        # The bot lane rides its carrier: personal rows lead with the owner, project
        # rows with the project, then the name - equality-bound on every query, so a bot's ledger
        # is O(page) like the others. The older user/project partials absorb bot rows as bloat
        # (bounded by the per-target cap); tightening their predicates is a later migration.
        Index(
            "ix_memories_bot_personal_lane",
            "user_id",
            "bot_name",
            "status",
            "created_at",
            "id",
            postgresql_where=sa_text("scope = 'bot' AND user_id IS NOT NULL"),
        ),
        Index(
            "ix_memories_bot_project_lane",
            "project_id",
            "bot_name",
            "status",
            "created_at",
            "id",
            postgresql_where=sa_text("scope = 'bot' AND project_id IS NOT NULL"),
        ),
        # FK-enforcement paths with no lane index of their own: a task or actor delete must not
        # seq-scan an append-forever table.
        Index(
            "ix_memories_source_task_id",
            "source_task_id",
            postgresql_where=sa_text("source_task_id IS NOT NULL"),
        ),
        Index("ix_memories_created_by_actor_id", "created_by_actor_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    scope: Mapped[str] = mapped_column(String(8), nullable=False)

    # The scope target - one of the three, per the CHECK above. `user_id` holds the human's id,
    # which IS their actor id (users.actor_id is PK/FK); the FK targets the actor spine like
    # every other FK in the system.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), default=None
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    skill_name: Mapped[str | None] = mapped_column(String(80), default=None)
    # The bot scope's name key - always beside exactly one carrier (user_id for a
    # personal bot, project_id for a project bot), per the CHECK above.
    bot_name: Mapped[str | None] = mapped_column(String(64), default=None)

    kind: Mapped[str] = mapped_column(String(12), nullable=False, default=MemoryKind.NOTE)
    text: Mapped[str] = mapped_column(String(TEXT_MAX), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=MemoryStatus.ACTIVE)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), default=None
    )

    # Provenance: the task that minted it (memory_extract) or None (typed by a person) - and always
    # who. Every memory can answer "where did you come from".
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False
    )

    use_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    last_used_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Generated in Postgres, deferred in Python: search reads it, row loads should not carry it.
    text_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=False,
        deferred=True,
    )

    @staticmethod
    def target_filter(
        scope: MemoryScope,
        *,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        skill_names: list[str] | None = None,
        bot_name: str | None = None,
    ) -> list[ColumnElement[bool]]:
        """The WHERE for one scope target - every query path goes through this, so a lane can
        never accidentally read across its boundary (a user filter without the user is an error,
        not an unbounded scan)."""
        if scope is MemoryScope.USER:
            if user_id is None:
                raise ValueError("user scope needs user_id")
            return [Memory.scope == MemoryScope.USER.value, Memory.user_id == user_id]
        if scope is MemoryScope.PROJECT:
            if project_id is None:
                raise ValueError("project scope needs project_id")
            return [Memory.scope == MemoryScope.PROJECT.value, Memory.project_id == project_id]
        if scope is MemoryScope.BOT:
            # The name plus exactly one carrier - the registry's scope predicate,
            # restated as a lane wall: ambiguous carriers are an error, never a broad read.
            if not bot_name:
                raise ValueError("bot scope needs bot_name")
            if (user_id is None) == (project_id is None):
                raise ValueError("bot scope needs exactly one of user_id or project_id")
            carrier = Memory.user_id == user_id if user_id is not None else Memory.project_id == project_id
            return [Memory.scope == MemoryScope.BOT.value, Memory.bot_name == bot_name, carrier]
        if not skill_names:
            raise ValueError("skill scope needs skill_names")
        return [Memory.scope == MemoryScope.SKILL.value, Memory.skill_name.in_(skill_names)]

    @classmethod
    def lane(
        cls,
        session: AsyncSession,
        scope: MemoryScope,
        *,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        skill_names: list[str] | None = None,
        bot_name: str | None = None,
    ) -> list[ColumnElement[bool]]:
        """`target_filter` inside the bound workspace - the WHERE every lane query
        runs. A person's memory is per (person, workspace): the same user lane read from another workspace is
        another ledger, and a skill note is the workspace's, never the platform's."""
        return [
            cls.workspace_id == tenant.workspace_of(session),
            *cls.target_filter(
                scope, user_id=user_id, project_id=project_id, skill_names=skill_names, bot_name=bot_name
            ),
        ]

    @classmethod
    async def get_for(cls, session: AsyncSession, memory_id: uuid.UUID) -> Memory | None:
        """One row by id, in the bound workspace - the single-row door `operations/memory` authorises
        on (the lane matrix is there). Another workspace's row is simply not found."""
        return await session.scalar(
            select(cls).where(cls.id == memory_id, cls.workspace_id == tenant.workspace_of(session))
        )

    @classmethod
    async def for_scope(
        cls,
        session: AsyncSession,
        scope: MemoryScope,
        *,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        skill_names: list[str] | None = None,
        bot_name: str | None = None,
        status: MemoryStatus | None = MemoryStatus.ACTIVE,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Memory]:
        """The ledger list - newest first, keyset-paged, optional ILIKE narrowing.
        `status=None` means every status: the ledger can show lineage, not just the living."""
        query = (
            select(cls)
            .where(
                *cls.lane(
                    session,
                    scope,
                    user_id=user_id,
                    project_id=project_id,
                    skill_names=skill_names,
                    bot_name=bot_name,
                )
            )
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if status is not None:
            query = query.where(cls.status == status.value)
        if q:
            query = query.where(cls.text.ilike(f"%{escape_like(q)}%", escape="\\"))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    @classmethod
    async def search(
        cls,
        session: AsyncSession,
        scope: MemoryScope,
        *,
        query_text: str,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        skill_names: list[str] | None = None,
        bot_name: str | None = None,
        limit: int = 24,
    ) -> list[Memory]:
        """Active memories relevant to `query_text`, best first: FTS rank, then recently-used,
        then recently-made. Recall-biased by construction (OR-query) - at ≤{ACTIVE_CAP} active
        rows per scope the token budget is the real filter, not this ranking."""
        tsq = tsquery_of(query_text)
        if not tsq:
            return []
        ts = func.to_tsquery("english", tsq)
        stmt = (
            select(cls)
            .where(
                *cls.lane(
                    session,
                    scope,
                    user_id=user_id,
                    project_id=project_id,
                    skill_names=skill_names,
                    bot_name=bot_name,
                ),
                cls.status == MemoryStatus.ACTIVE.value,
                cls.text_tsv.op("@@")(ts),
            )
            .order_by(
                func.ts_rank_cd(cls.text_tsv, ts).desc(),
                cls.last_used_at.desc().nulls_last(),
                cls.created_at.desc(),
                cls.id.desc(),
            )
            .limit(limit)
        )
        return list(await session.scalars(stmt))

    @classmethod
    async def mark_used(cls, session: AsyncSession, ids: list[uuid.UUID], *, at: dt.datetime) -> None:
        """One UPDATE: assembly just spent these - bump the decay signal."""
        if not ids:
            return
        await session.execute(
            update(cls).where(cls.id.in_(ids)).values(use_count=cls.use_count + 1, last_used_at=at)
        )

    @classmethod
    async def by_ids(cls, session: AsyncSession, ids: list[uuid.UUID]) -> list[Memory]:
        if not ids:
            return []
        return list(await session.scalars(select(cls).where(cls.id.in_(ids))))

    @classmethod
    async def active_count(
        cls,
        session: AsyncSession,
        scope: MemoryScope,
        *,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        skill_names: list[str] | None = None,
        bot_name: str | None = None,
    ) -> int:
        stmt = select(func.count()).where(
            *cls.lane(
                session,
                scope,
                user_id=user_id,
                project_id=project_id,
                skill_names=skill_names,
                bot_name=bot_name,
            ),
            cls.status == MemoryStatus.ACTIVE.value,
        )
        return int(await session.scalar(stmt) or 0)

    @classmethod
    async def over_cap_victims(
        cls,
        session: AsyncSession,
        scope: MemoryScope,
        *,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        skill_names: list[str] | None = None,
        bot_name: str | None = None,
        cap: int = ACTIVE_CAP,
    ) -> list[Memory]:
        """The rows past the cap, in deterministic retirement order: least-recently-used first
        (never-used counts as oldest), then oldest-made. The caller retires them - this only
        chooses, so the choice is testable without a write."""
        count = await cls.active_count(
            session, scope, user_id=user_id, project_id=project_id, skill_names=skill_names, bot_name=bot_name
        )
        if count <= cap:
            return []
        stmt = (
            select(cls)
            .where(
                *cls.lane(
                    session,
                    scope,
                    user_id=user_id,
                    project_id=project_id,
                    skill_names=skill_names,
                    bot_name=bot_name,
                ),
                cls.status == MemoryStatus.ACTIVE.value,
            )
            .order_by(cls.last_used_at.asc().nulls_first(), cls.created_at.asc(), cls.id.asc())
            .limit(count - cap)
        )
        return list(await session.scalars(stmt))

    def cursor(self) -> str:
        return keyset_cursor(self)


class MemoryUse(Base):
    """One memory, spent by one task - written in the same transaction as `context.assembled`.

    The observability join: "which tasks used this memory" is a SELECT here, forever, instead of
    a JSONB scan over an append-only event table that gets slower every week (the append-only posture).
    """

    __tablename__ = "memory_uses"
    __table_args__ = (
        # `id` rides along so `tasks_for`'s (created_at DESC, id DESC) page never sorts;
        # `task_id` alone backs the CASCADE when a task is deleted - this table grows one row
        # per memory per task, forever, and a delete must not seq-scan it.
        Index("ix_memory_uses_memory_id_created_at_id", "memory_id", "created_at", "id"),
        Index("ix_memory_uses_task_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    def record_all(cls, session: AsyncSession, memory_ids: list[uuid.UUID], task_id: uuid.UUID) -> None:
        """Rows into the caller's transaction - the caller owns the commit, like every operation."""
        for mid in memory_ids:
            session.add(cls(memory_id=mid, task_id=task_id))

    @classmethod
    async def tasks_for(
        cls, session: AsyncSession, memory_id: uuid.UUID, *, limit: int = 20
    ) -> list[MemoryUse]:
        """The blast radius, newest first - every task this memory shaped."""
        stmt = (
            select(cls)
            .where(cls.memory_id == memory_id)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        return list(await session.scalars(stmt))
