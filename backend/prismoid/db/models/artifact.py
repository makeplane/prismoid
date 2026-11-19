# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Artifact - a content-addressed deliverable created only by publish or revise, carrying its
provenance.

There are exactly two doors, and both mint immutable rows: `frame.publish()` → manifest →
harvest, and `operations.artifacts.revise` - a human or follow-up edit that never
mutates bytes, it appends the next **revision** on the same lineage. Nothing
outside those doors exists, so an artifact whose provenance is missing is not a lesser
artifact - it is an impossible one.

**A Project contains artifacts *through its Tasks*.** There is deliberately no artifact→project FK
(a design rule): provenance keeps exactly one parent chain, and `GET /v1/projects/{id}/artifacts` is a
derived query joining via `tasks.project_id`. A second parent is a second truth.

**Lineage:** `lineage_id` is the root row's own id (no FK - a follow-up's revision outlives its
parent task's deletion); `revision` counts from 1; display paths show lineage-latest via a
NOT-EXISTS (the `Task._visible_to` idiom), while GC and provenance walks read every row.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, and_, exists, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, aliased, mapped_column

from prismoid.db.base import Base, created_at, escape_like, keyset_before, keyset_cursor, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.errors import NotFound


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_task_id", "task_id"),
        Index("ix_artifacts_sha256", "sha256"),
        # The shelf query filters task_id + role; declared here so metadata matches
        # migration c4d6e8f0a2b3 (the drift the audit flagged - the index existed, the model omitted it).
        Index("ix_artifacts_task_role", "task_id", "role"),
        # A replan's `plan_steps` delete fires SET NULL onto `step_id`; unindexed that cascade was
        # a seq scan of the whole table (audit finding). Partial - only rows still naming a step
        # need finding by it.
        Index("ix_artifacts_step_id", "step_id", postgresql_where=text("step_id IS NOT NULL")),
        # `for_project` keyset-orders on (created_at, id); the shelf had no matching index.
        Index("ix_artifacts_created_at_id", "created_at", "id"),
        # `uq_`, not `ux_`: the unique-index prefix in NAMING_CONVENTION - renamed to end the
        # convention break the audit flagged (the model and its migration are renamed together).
        Index("uq_artifacts_lineage_revision", "lineage_id", "revision", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="SET NULL"), default=None
    )

    # The revision chain. `lineage_id` deliberately has no FK - see the docstring.
    lineage_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), default=None
    )

    type: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)

    # A named version: "final draft", "sent to client" - the user's word for THIS
    # revision, shown in the history picker. Presentation metadata like the sidecar pointers
    # (the content stays immutable); NULL = unnamed, set/cleared via the label door.
    label: Mapped[str | None] = mapped_column(String(80), default=None)

    # Content-addressed: the bytes live in MinIO under their own hash, so the same content
    # published twice costs one object and the download can never disagree with the verification.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(nullable=False, default=0)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)

    # The render sidecar: a small JSON preview built IN A FRAME - at harvest in the
    # work frame so the row is born with it, refreshed by the verify frame for
    # spreadsheets. Its own CAS blob; NULL = no preview, honestly.
    preview_sha256: Mapped[str | None] = mapped_column(String(64), default=None)

    # The FULL-FIDELITY rendition: LibreOffice's faithful render, made in the work
    # frame at harvest - docx/pptx as PDF ("application/pdf" → the browser's own viewer),
    # xlsx as styled HTML ("text/html" → the sandboxed iframe cage). The preview above is the
    # instant extraction; this is THE DOCUMENT. Both NULL = no rendition, honestly.
    rendition_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    rendition_media: Mapped[str | None] = mapped_column(String(32), default=None)

    # deliverable | working | superseded. Stamped at harvest FROM THE STEP SPEC -
    # the model cannot touch it (the publish tool signature is unchanged). Working rows are the
    # lane's machinery: outline, sections, figures - they cross steps through the one publish door,
    # still appear on the record, but never reach the shelf and never enqueue verification. A
    # superseded row is output a steer overtook (`supersede`): on the record, never mounted or shown.
    role: Mapped[str] = mapped_column(
        String(12), nullable=False, default="deliverable", server_default="deliverable"
    )

    # The ManifestEntry, verbatim (schemas/artifacts.py): who made it, with which model, from
    # which sources, pinned to which skill versions. The chain that makes a claim checkable.
    provenance: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def titles_for_step(
        cls, session: AsyncSession, step_id: uuid.UUID, *, role: str, artifact_type: str
    ) -> set[str]:
        """The titles already published on a step, through one role+type lens. The figure
        job's idempotent re-entry reads this : a fig id that already landed
        on the outline step is never bought twice - a recovered worker re-buys nothing."""
        rows = await session.scalars(
            select(cls.title).where(cls.step_id == step_id, cls.role == role, cls.type == artifact_type)
        )
        return set(rows.all())

    @classmethod
    async def for_project(
        cls,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Artifact]:
        """Every artifact produced by a task *filed in* this project. A query over the
        project's tasks - never a direct FK, because an artifact belongs to its task, and the task is
        what moves between projects. Attribution follows the task at read time, which is the honest
        shape: nothing to migrate when a task moves.

        Keyset-paginated on `(created_at, id)` - a project accumulates artifacts without
        bound, so this list, unlike the small ones, must never be an unpaged full-table read."""
        query = (
            select(cls)
            .join(Task, Task.id == cls.task_id)
            # The role lens: the shelf shows what the user asked FOR - working rows
            # (sections, outlines, figures) are the lane's machinery, visible on the record only.
            .where(Task.project_id == project_id, cls.role == "deliverable", cls._is_latest())
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if q:
            query = query.where(cls.title.ilike(f"%{escape_like(q)}%", escape="\\"))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    @classmethod
    def _is_latest(cls, as_of: dt.datetime | None = None):
        """No newer revision on my lineage - the display predicate (the `_visible_to` idiom:
        one NOT-EXISTS every list shares, so 'latest' cannot mean two things). With `as_of`, no
        newer revision that existed THEN."""
        newer = aliased(cls)
        later = and_(newer.lineage_id == cls.lineage_id, newer.revision > cls.revision)
        if as_of is not None:
            later = and_(later, newer.created_at <= as_of)
        return ~exists(select(newer.id).where(later))

    def cursor(self) -> str:
        """The keyset cursor for this row - opaque to the client, meaningful to the `(created_at, id)` index."""
        return keyset_cursor(self)

    @classmethod
    async def by_agent(
        cls,
        session: AsyncSession,
        agent: str,
        *,
        creator_actor_id: uuid.UUID,
        limit: int = 30,
    ) -> list[tuple[Artifact, str]]:
        """The newest deliverables an AGENT published across the creator's tasks, with each
        task's goal - the bot page's Artifacts shelf. Joined through the step's spec
        because provenance lives on the plan, and creator-scoped for the same reason the runs
        feed is: a shared bot's work still belongs to whoever paid for the task."""
        from prismoid.db.models.plan import PlanStep

        rows = await session.execute(
            select(cls, Task.goal)
            .join(PlanStep, PlanStep.id == cls.step_id)
            .join(Task, Task.id == cls.task_id)
            .where(
                PlanStep.spec["agent"].as_string() == agent,
                Task.workspace_id == tenant.workspace_of(session),  # the bound workspace's work only
                Task.creator_actor_id == creator_actor_id,
                cls.role != "superseded",  # output a steer overtook is not the bot's work
                cls._is_latest(),
            )
            .order_by(cls.created_at.desc())
            .limit(limit)
        )
        return [(artifact, goal) for artifact, goal in rows.all()]

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[Artifact]:
        """EVERY row, all revisions - the provenance/GC walk. Display paths want `latest_for_task`."""
        return list(await session.scalars(select(cls).where(cls.task_id == task_id).order_by(cls.created_at)))

    @classmethod
    async def for_step(cls, session: AsyncSession, step_id: uuid.UUID) -> list[Artifact]:
        """What one step harvested, oldest first - the VERIFYING re-entry's resume-at-review
        read: the produce is durable; only the review is owed."""
        return list(await session.scalars(select(cls).where(cls.step_id == step_id).order_by(cls.created_at)))

    @classmethod
    async def latest_for_task(
        cls, session: AsyncSession, task_id: uuid.UUID, *, as_of: dt.datetime | None = None
    ) -> list[Artifact]:
        """The task's shelf: one row per lineage, the newest revision of each - or, with `as_of`,
        the shelf as it stood then (a fork's inheritance: its parent's work at the fork, never a
        revision or a file the parent made afterwards)."""
        query = select(cls).where(cls.task_id == task_id, cls._is_latest(as_of))
        if as_of is not None:
            query = query.where(cls.created_at <= as_of)
        return list(await session.scalars(query.order_by(cls.created_at)))

    @classmethod
    async def revisions_of(
        cls, session: AsyncSession, lineage_id: uuid.UUID, *, actor: Actor | None
    ) -> list[Artifact]:
        """The lineage, newest first - the version picker's list. Bounded by construction (a chain
        of human/agent saves), so an explicit generous cap stands in for a cursor; if a lineage
        ever grows past it, the cap is the bug report. A lineage can span tasks (a fork's
        republish chains onto its parent's), and those tasks can differ in who sees them, so an
        `actor` gets only the rows on tasks they can see; `None` is the whole lineage, for the
        head checks that keep revision numbers unique and never answer a person."""
        rows: list[Artifact] = list(
            await session.scalars(
                select(cls).where(cls.lineage_id == lineage_id).order_by(cls.revision.desc()).limit(200)
            )
        )
        if actor is None:
            return rows
        seen: set[uuid.UUID] = set()
        for task_id in {r.task_id for r in rows}:  # a handful: the parent and its forks
            try:
                seen.add((await Task.get_for(session, task_id, actor)).id)
            except NotFound:
                continue
        return [r for r in rows if r.task_id in seen]

    @classmethod
    async def get_for(cls, session: AsyncSession, artifact_id: uuid.UUID, actor: Actor) -> Artifact:
        """The ONLY artifact fetch path. Scope follows the **Task**, because an artifact
        belongs to a Project *through* its Task and there is deliberately no second parent - and to a
        workspace the same way: the join applies the task's workspace before anything else."""
        row = await session.scalar(
            select(cls)
            .join(Task, Task.id == cls.task_id)
            .where(cls.id == artifact_id, Task.workspace_id == tenant.workspace_of(session))
        )
        if row is None:
            raise NotFound("no such artifact")
        await Task.get_for(session, row.task_id, actor)  # raises NotFound if you cannot see it
        return row

    @classmethod
    async def by_digest(cls, session: AsyncSession, step_id: uuid.UUID, sha256: str) -> Artifact | None:
        """What this STEP already published with these bytes - harvest's idempotency is per step
        (`operations.artifacts.harvest`). A superseded row never answers (R52): output a steer
        overtook is not taken back by a byte-identical republish, which becomes a new row on the
        same blob."""
        return await session.scalar(
            select(cls).where(cls.step_id == step_id, cls.sha256 == sha256, cls.role != "superseded")
        )

    @classmethod
    async def supersede(cls, session: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
        """A steer overtook this output (ADR-064 fences): the rows stay - provenance keeps every
        one - but in the `superseded` role, which no mount, shelf or chat card shows. They are
        never taken back: a re-run that republishes the same bytes is a new row (R52)."""
        if ids:
            await session.execute(update(cls).where(cls.id.in_(list(ids))).values(role="superseded"))

    @classmethod
    async def any_with_digest(cls, session: AsyncSession, sha256: str) -> bool:
        """Does ANY artifact still point at this content-addressed blob? The dedup guard before a
        blob is garbage-collected - the same bytes can be published by more than one task, and a
        digest may be referenced as CONTENT by one row, as a preview SIDECAR by another, or as a
        RENDITION by a third. Every pointer class the model carries is checked here;
        adding a *_sha256 column without extending this guard is how a live blob gets collected."""
        query = (
            select(cls.id)
            .where((cls.sha256 == sha256) | (cls.preview_sha256 == sha256) | (cls.rendition_sha256 == sha256))
            .limit(1)
        )
        return await session.scalar(query) is not None
