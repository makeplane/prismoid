# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routine - a standing automation: a saved composer payload plus a When; every fire mints an
ordinary Task. · RoutineRun - one fire decision, append-only: every fire AND every
non-fire, each wearing its reason. · RoutineEventCursor - the event matcher's durable watermark
(one row, the Beat pattern; written from W4).

Scope is the bots shape verbatim: `project_id NULL` is personal to the owner; set, the routine is
shared under the role matrix. Two partial unique indexes carry the name for the same reason
`bots_registry`'s do - Postgres treats NULLs as distinct, and one plain constraint would
neither keep a personal name unique per owner nor a project name unique per project.

The durability keystone is `uq_routine_runs_routine_id_occurrence`: one occurrence, one row,
whoever's sweep enqueued it - the third of the fire path's three idempotent layers (the DBOS
workflow id and `tasks.client_token` are the other two). `next_fire_at` is only ever advanced
by compare-and-set against the occurrence that fired (the edit race closes there), and `due()`
reads **Postgres `now()`** - the database is the time authority (the pg-boss lesson), so a
skewed worker clock shifts nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Row,
    SmallInteger,
    String,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, keyset_cursor, uuid_pk, vocabulary
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project, Role
from prismoid.errors import Forbidden, NotFound
from prismoid.schemas.routines import RoutineStatus, TriggerKind, occurrence_for_clock


class Routine(Base):
    __tablename__ = "routines"
    __table_args__ = (
        vocabulary("status", RoutineStatus),
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project unfiles the row (only project_id nulls; workspace_id stays).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_routines_project_workspace",
            ondelete="SET NULL (project_id)",
        ),
        # Within a project, a name is one routine; a personal name is unique per owner. The bots
        # precedent, and partial for the bots reason (module docstring).
        Index(
            "uq_routines_project_name",
            "project_id",
            "name",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "uq_routines_personal_name",
            "workspace_id",  # per workspace: another workspace's routines are not there to collide with
            "owner_actor_id",
            "name",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
        ),
        # The sweep's one query: due, active, schedule-kind. Partial, so the index holds only
        # rows the sweep could ever return.
        Index(
            "ix_routines_due",
            "next_fire_at",
            postgresql_where=text("status = 'active' AND trigger_kind = 'schedule'"),
        ),
        Index("ix_routines_owner_actor_id", "owner_actor_id"),
        Index("ix_routines_project_id_status", "project_id", "status"),
        Index("ix_routines_created_at_id", "created_at", "id"),  # keyset only
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Becomes the minted task's `goal` verbatim (same cap) - the run's task IS the instruction
    # snapshot, which is why no versioning table exists.
    instruction: Mapped[str] = mapped_column(String(8000), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RoutineStatus.ACTIVE)
    paused_reason: Mapped[str | None] = mapped_column(String(24), default=None)
    pause_note: Mapped[str | None] = mapped_column(String(400), default=None)  # Temporal's notes

    # The owner: a human (enforced in operations, the Project.create invariant). RESTRICT like
    # tasks.creator - attribution outlives the profile; erasure PAUSES, never orphans.
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False
    )
    # ONE nullable FK, the task law: SET NULL - a deleted project flips its routines personal
    # (paused by the same operation) rather than erasing their history.
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    # The When. One trigger per routine (multi-trigger is deferred); `trigger` is the
    # kind-shaped spec (`ScheduleSpec` for schedule), validated in schemas.
    trigger_kind: Mapped[str] = mapped_column(String(12), nullable=False, default=TriggerKind.SCHEDULE)
    trigger: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    next_fire_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    last_fired_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # The overlap=queue buffer, bounded to ONE by being a column (W2).
    buffered_occurrence: Mapped[str | None] = mapped_column(String(64), default=None)

    # The saved composer payload (schemas.routines.RoutineTemplate.dump()).
    template: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    # ── behaviours (owner-confirmed defaults). Money is a COLUMN, not a template
    # key - the admin console and the caps read it without parsing JSON. REQUIRED: unattended
    # work is always capped.
    budget_usd_per_run: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    overlap: Mapped[str] = mapped_column(String(8), nullable=False, default="skip")
    catch_up: Mapped[str] = mapped_column(String(8), nullable=False, default="skip")
    notify: Mapped[str] = mapped_column(String(12), nullable=False, default="findings")
    posture: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")
    min_interval_s: Mapped[int] = mapped_column(nullable=False, default=300)
    daily_fire_cap: Mapped[int] = mapped_column(nullable=False, default=24)
    monthly_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), default=None)
    pause_after_failures: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    # Seasons: standing ≠ eternal. Either retires the routine honestly, journaled.
    until_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    max_runs: Mapped[int | None] = mapped_column(default=None)
    runs_minted: Mapped[int] = mapped_column(nullable=False, default=0)

    # The inbound door's credential (W3): `rt_` fire token, sha256 at rest, shown once.
    fire_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ── scope ─────────────────────────────────────────────────────────────────────────────────

    @classmethod
    async def get_for(
        cls, session: AsyncSession, routine_id: uuid.UUID, actor: Actor, *, min_role: Role = Role.VIEWER
    ) -> Routine:
        """The ONE fetch path, project-aware: a personal routine exists only for its owner
        (NotFound - existence is never leaked); a project routine rides `Project.get_for`, so a
        viewer reads, an editor manages, and a stranger sees nothing. Always in the bound workspace
        (ADR-061): another workspace's routine - even the actor's own - is not there."""
        row = await session.scalar(
            select(cls).where(cls.id == routine_id, cls.workspace_id == tenant.workspace_of(session))
        )
        if row is None:
            raise NotFound("no such routine")
        if row.project_id is None:
            if row.owner_actor_id != actor.id:
                raise NotFound("no such routine")
            return row
        await Project.get_for(session, row.project_id, actor, min_role=min_role)
        return row

    def require_owner(self, actor: Actor) -> None:
        """Retire and the fire token are owner verbs even in a shared project."""
        if self.owner_actor_id != actor.id:
            raise Forbidden("only the routine's owner can do that")

    @classmethod
    async def active_count_for(cls, session: AsyncSession, owner_actor_id: uuid.UUID) -> int:
        """How many non-retired routines this owner holds - the max_active_per_user gate's read
        (a 409 at create, never a silent drop)."""
        q = (
            select(func.count())
            .select_from(cls)
            .where(cls.owner_actor_id == owner_actor_id, cls.status != RoutineStatus.RETIRED)
        )
        return int(await session.scalar(q) or 0)

    @classmethod
    async def list_for(
        cls,
        session: AsyncSession,
        actor: Actor,
        *,
        project_id: uuid.UUID | None,
        limit: int,
        cursor: str | None,
    ) -> list[Routine]:
        """`project_id` set ⇒ that project's routines (viewer-gated); omitted ⇒ the actor's own,
        every scope - "my routines" (in the bound workspace). Keyset on `(created_at, id)` only."""
        q = select(cls).where(cls.workspace_id == tenant.workspace_of(session))
        if project_id is not None:
            await Project.get_for(session, project_id, actor, min_role=Role.VIEWER)
            q = q.where(cls.project_id == project_id)
        else:
            q = q.where(cls.owner_actor_id == actor.id)
        if cursor:
            q = q.where(keyset_before(cls, cursor))
        q = q.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list((await session.execute(q)).scalars())

    @classmethod
    async def for_keeper(
        cls,
        session: AsyncSession,
        actor: Actor,
        *,
        bot_name: str,
        project_id: uuid.UUID | None,
        limit: int = 50,
    ) -> list[Routine]:
        """The routines whose keeper wears this
        bot's name, within what the viewer can see - a project bot's rows viewer-gated through
        the project, a personal bot's rows the actor's own. A JSONB WHERE at today's scale; a
        partial index on `template->>'bot'` is the named valve if rosters grow keepers."""
        q = select(cls).where(
            cls.workspace_id == tenant.workspace_of(session), cls.template["bot"].as_string() == bot_name
        )
        if project_id is not None:
            await Project.get_for(session, project_id, actor, min_role=Role.VIEWER)
            q = q.where(cls.project_id == project_id)
        else:
            q = q.where(cls.owner_actor_id == actor.id)
        q = q.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list((await session.execute(q)).scalars())

    def cursor(self) -> str:
        return keyset_cursor(self)

    # ── the sweep's read, and the fire's writes ───────────────────────────────────────────────

    @classmethod
    async def clock(cls, session: AsyncSession) -> dt.datetime:
        """Postgres `now()` - the same authority `due()` filters by. The fire gate's judgments
        (miss grace, throttle, seasons, advancement) ride THIS clock, never the worker's host
        clock: a worker minutes fast must not journal every on-time fire as missed (the
        pg-boss lesson, applied to the gate too)."""
        return (await session.execute(select(func.now()))).scalar_one()

    @classmethod
    async def due(cls, session: AsyncSession, *, limit: int = 500) -> list[Row[Any]]:
        """(id, next_fire_at) for every active schedule routine whose fire time has arrived -
        by **Postgres `now()`**, never a host clock. Read-only: the sweep stays dumb, and all
        correctness lives in the fire path's idempotent layers."""
        q = (
            select(cls.id, cls.next_fire_at)
            .where(
                cls.status == RoutineStatus.ACTIVE,
                cls.trigger_kind == TriggerKind.SCHEDULE,
                cls.next_fire_at.is_not(None),
                cls.next_fire_at <= func.now(),
            )
            .order_by(cls.next_fire_at)
            .limit(limit)
        )
        return list((await session.execute(q)).all())

    @classmethod
    async def admin_page(
        cls,
        session: AsyncSession,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[tuple[Routine, Any]]:
        """(routine, owner User | None), newest first, across every owner and project - the
        operator's read (admin surface recipe step 1)."""
        from prismoid.db.base import escape_like
        from prismoid.db.models.user import User

        query = select(cls, User).join(User, User.actor_id == cls.owner_actor_id, isouter=True)
        if status:
            query = query.where(cls.status == status)
        if q:
            query = query.where(cls.name.ilike(f"%{escape_like(q)}%", escape="\\"))
        if cursor:
            query = query.where(keyset_before(cls, cursor))
        query = query.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return [(r[0], r[1]) for r in (await session.execute(query)).all()]

    @classmethod
    async def auto_paused_count(cls, session: AsyncSession) -> int:
        """Routines that paused THEMSELVES (failures/budget) - each one is asking for a human,
        which is exactly what the operator's alert list collects."""
        q = (
            select(func.count())
            .select_from(cls)
            .where(cls.status == RoutineStatus.PAUSED, cls.paused_reason.in_(["failures", "budget"]))
        )
        return int(await session.scalar(q) or 0)

    @classmethod
    async def active_event_routines(cls, session: AsyncSession) -> list[Routine]:
        """Every live listener - the matcher's roster read. Small by construction (the
        max_active_per_user cap bounds it); rides ix_routines_project_id_status."""
        q = select(cls).where(cls.status == RoutineStatus.ACTIVE, cls.trigger_kind == TriggerKind.EVENT)
        return list((await session.execute(q)).scalars())

    @classmethod
    async def parked(cls, session: AsyncSession) -> list[Row[Any]]:
        """(id, buffered_occurrence) for every active schedule routine holding a parked fire -
        the sweep's DRAIN arm. The parked fire is re-offered each tick and the GATE decides:
        still waiting → BUFFERED and the workflow ends; drained or expired → journaled. Sweep-
        driven so a parked fire holds no queue slot and survives deploys (the in-workflow park held
        one of the fire lane's eight slots for up to a day - eight
        parks froze every routine instance-wide, the ADR-027 ghost shape - and a deploy's
        version sweep could cancel the sleeping workflow and strand the buffer forever).
        Small by construction (max_active_per_user bounds the table)."""
        q = select(cls.id, cls.buffered_occurrence).where(
            cls.status == RoutineStatus.ACTIVE,
            cls.trigger_kind == TriggerKind.SCHEDULE,
            cls.buffered_occurrence.is_not(None),
        )
        return list((await session.execute(q)).all())

    @classmethod
    async def lock(cls, session: AsyncSession, routine_id: uuid.UUID) -> Routine | None:
        """The fire gate's re-read: FOR UPDATE, judged on the CURRENT row - a routine paused,
        retired, or rewritten between enqueue and execution is judged as it is now."""
        q = select(cls).where(cls.id == routine_id).with_for_update()
        return (await session.execute(q)).scalar_one_or_none()

    def advance(self, *, occurrence_fire_at: dt.datetime | None, to: dt.datetime | None) -> bool:
        """Compare-and-advance: move `next_fire_at` only if it still equals the occurrence that
        fired. False ⇒ an edit superseded this fire (journal it, touch nothing). A fire with NO
        occurrence instant (token/API/event sources) may never move the clock - advancing on
        None was unconditional, and a webhook burst landing in the throttle/overlap branches
        silently rescheduled pending clock occurrences out of existence.
        The compare runs at OCCURRENCE precision (seconds - `occurrence_for_clock`'s), so an
        instant re-derived from its key still matches a stored value carrying microseconds."""
        if occurrence_fire_at is None or self.next_fire_at is None:
            return False
        if occurrence_for_clock(self.next_fire_at) != occurrence_for_clock(occurrence_fire_at):
            return False
        self.next_fire_at = to
        return True


class RoutineRun(Base):
    __tablename__ = "routine_runs"
    __table_args__ = (
        # THE exactly-once guarantee: one occurrence, one receipt, across every racing sweep,
        # replayed workflow, and re-scanned watermark window.
        Index("uq_routine_runs_routine_id_occurrence", "routine_id", "occurrence", unique=True),
        Index("ix_routine_runs_routine_id_created_at_id", "routine_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    routine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routines.id", ondelete="CASCADE"), nullable=False
    )
    occurrence: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)  # clock|api|event|manual
    decision: Mapped[str] = mapped_column(String(20), nullable=False)  # RunDecision
    # The run's OUTCOME is the task's own status and verdict - the ledger records the mint
    # decision; screens join the task (the infra-green ≠ task-success split, by construction).
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    # The intended instant; `created_at − scheduled_for` is the honest lateness stamp
    # (graphile-worker's `_cron {ts, backfilled}`).
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(default=None)
    chain_depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def minted_count_since(
        cls, session: AsyncSession, routine_id: uuid.UUID, *, since: dt.datetime
    ) -> int:
        """Fires that actually minted (held counts - it spent a mint) since `since` - the daily
        cap's read, windowed by the routine's own day."""
        q = (
            select(func.count())
            .select_from(cls)
            .where(
                cls.routine_id == routine_id,
                cls.decision.in_(["minted", "held"]),
                cls.created_at >= since,
            )
        )
        return int(await session.scalar(q) or 0)

    @classmethod
    async def by_occurrence(
        cls, session: AsyncSession, routine_id: uuid.UUID, occurrence: str
    ) -> RoutineRun | None:
        q = select(cls).where(cls.routine_id == routine_id, cls.occurrence == occurrence)
        return (await session.execute(q)).scalar_one_or_none()

    @classmethod
    async def page_with_status(
        cls, session: AsyncSession, routine_id: uuid.UUID, *, limit: int, cursor: str | None
    ) -> list[tuple[RoutineRun, str | None]]:
        """The runs page with each minted task's CURRENT status joined in - the ledger records
        the mint decision; the task carries the outcome (the infra-green ≠ task-success split).
        One LEFT JOIN, keyset only."""
        from prismoid.db.models.task import Task

        q = (
            select(cls, Task.status)
            .join(Task, Task.id == cls.task_id, isouter=True)
            .where(cls.routine_id == routine_id)
        )
        if cursor:
            q = q.where(keyset_before(cls, cursor))
        q = q.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return [(row[0], row[1]) for row in (await session.execute(q)).all()]

    def cursor(self) -> str:
        return keyset_cursor(self)


class RoutineEventCursor(Base):
    """The event matcher's durable watermark (W4) - one row, the Beat pattern. Created with the
    tables so W4 is code, not a migration. Seeded at NOW on first read: the lane starts from
    its birth, never replaying history into fires."""

    __tablename__ = "routine_event_cursor"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, autoincrement=False)
    watermark: Mapped[dt.datetime] = mapped_column(nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Flush-only, both of them: the CALLER commits (operations/jobs own transactions - the
    # rule these two quietly broke as the only model-layer commits in db/models). The event sweep
    # commits the seed and the advance explicitly.

    @classmethod
    async def read_or_seed(cls, session: AsyncSession, *, seed_at: dt.datetime) -> dt.datetime:
        row = await session.get(cls, 1)
        if row is None:
            session.add(cls(id=1, watermark=seed_at))
            return seed_at
        return row.watermark

    @classmethod
    async def advance(cls, session: AsyncSession, *, to: dt.datetime) -> None:
        row = await session.get(cls, 1)
        if row is not None and row.watermark < to:
            row.watermark = to
