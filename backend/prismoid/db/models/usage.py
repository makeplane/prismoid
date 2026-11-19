# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""UsageEvent - one provider call's native units + write-time cost. The money ledger.

Two tables, one file, because they are one subject seen at two resolutions: the ledger
(`usage_events`, append-only, exact) and the rollup (`project_usage_daily`, the thing you actually
read). The rule: *no usage aggregation outside operations/usage.py + rollup* - a dashboard that scans
the ledger is a dashboard that gets slower every day you use the product.

**The ledger law**: usage is attributed **at time of spend** and is never rewritten. Moving a
task between projects does not touch a single row here. Spend before the move stays where it
happened; spend after accrues to the new project. That is why `project_id` is a plain column and not
a join through `tasks` - the join would silently rewrite history the moment a task moved.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    cast,
    func,
    literal,
    select,
    text,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, uuid_pk


@dataclass(frozen=True)
class TaskSpend:
    """One task's ledger, grouped by the database (`UsageEvent.task_spend`). Every list is in
    first-spend order; the family sums add up to the task's total and their counts to its calls."""

    by_family: list[tuple[str, Decimal, int]]
    by_step: list[tuple[uuid.UUID, Decimal]]
    by_spender: list[tuple[uuid.UUID, Decimal]]
    units: list[tuple[str, int]]


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_project_id_at", "project_id", "at"),
        Index("ix_usage_events_task_id", "task_id"),
        # A replan's `plan_steps` delete fires SET NULL onto `step_id`; unindexed that cascade
        # full-scanned the ledger (audit finding), and `for_step` reads it directly. Partial -
        # only rows still bound to a step need finding by it.
        Index("ix_usage_events_step_id", "step_id", postgresql_where=text("step_id IS NOT NULL")),
        # The operator's spend views (admin surface): "last N days" is a global time-window scan,
        # and (project_id, at) cannot serve it. Per-user attribution reads the labeled spender
        # (`spender()`: the epoch's actor, else - for unlabeled history - the task's creator).
        Index("ix_usage_events_at", "at"),
        # One workspace's usage over time: a workspace's spend reads, and the foreign-key check on the
        # workspace row (the composite's leading column serves it).
        Index("ix_usage_events_workspace_at", "workspace_id", "at"),
        # The label's per-person read (`total_for_creator`): `actor_id = X` over
        # labeled rows. Partial - history before the label is NULL and never searched by it.
        Index("ix_usage_events_actor_id", "actor_id", postgresql_where=text("actor_id IS NOT NULL")),
        Index(
            "ix_usage_events_conversation_id",
            "conversation_id",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), default=None
    )

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="SET NULL"), default=None
    )
    # Denormalised on purpose - see the ledger law in the module docstring.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), default=None
    )
    # The spend LABEL: who opened the epoch the spending work had
    # applied, copied in the INSERT by `epoch_actor()`. A reporting label, never a wallet.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    # A chat reply's spend: attributed to its conversation at time of spend, the
    # task/step columns null - spend per conversation is a join on this, never a table.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), default=None
    )

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    family: Mapped[str] = mapped_column(String(40), nullable=False)

    # Native units, exactly as the provider billed them. No metering, no merge - a
    # synthetic "token" that averages an image and a second of audio is a number nobody can audit.
    units: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    # NUMERIC(20,12), not (14,6) - and certainly not a float.
    #
    # Six decimal places sounds generous until you price a real call: 35 input + 2 output tokens on
    # a cheap model costs $0.00000328, which NUMERIC(14,6) stores as **0.000003**. The row is wrong
    # by 9% before it is even written. A million such calls rounds away up to $0.50 - and
    # the ledger reconciles *to the cent*, not to the nearest fifty.
    #
    # Twelve places is what the price book already uses (a per-token price genuinely is that
    # small). The ledger must be at least as precise as the prices that fed it, or the arithmetic
    # is exact right up to the moment it is stored.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 12), nullable=False)
    price_version: Mapped[int] = mapped_column(nullable=False, default=1)

    at: Mapped[dt.datetime] = mapped_column(nullable=False)

    @staticmethod
    def parent_workspace(
        task_id: uuid.UUID | None, conversation_id: uuid.UUID | None, *, bound: uuid.UUID | None = None
    ) -> Any:
        """The ledger stamp: a scalar subquery copying the parent's workspace inside the
        INSERT. Task first, then conversation, then the recording unit's BOUND workspace - a
        workspace-initiated machinery call (a bot's selection eval, a skill's routing eval) is that
        workspace's spend. Neither parent in a platform unit (catalog probes, the operator console
        binds nothing) = platform spend = NULL."""
        from prismoid.db.models.conversation import Conversation
        from prismoid.db.models.task import Task

        if not (task_id or conversation_id):
            return bound
        parents: list[Any] = [
            select(Task.workspace_id).where(Task.id == task_id).scalar_subquery(),
            select(Conversation.workspace_id).where(Conversation.id == conversation_id).scalar_subquery(),
        ]
        if bound is not None:
            parents.append(literal(bound, type_=Uuid))
        return select(func.coalesce(*parents)).scalar_subquery()

    @staticmethod
    def epoch_actor(task_id: uuid.UUID | None, step_id: uuid.UUID | None) -> Any:
        """The spend label, copied in the INSERT: who opened the epoch the spending
        work had applied - the step's epoch actor, else the task's (the planner, a re-plan), else
        the turn's author (rows from before 1c). No task = no label."""
        from prismoid.db.models.plan import PlanStep
        from prismoid.db.models.task import Task

        if task_id is None:
            return None
        parents: list[Any] = []
        if step_id is not None:
            parents.append(select(PlanStep.epoch_actor_id).where(PlanStep.id == step_id).scalar_subquery())
        parents.append(select(Task.epoch_actor_id).where(Task.id == task_id).scalar_subquery())
        parents.append(select(Task.turn_actor_id).where(Task.id == task_id).scalar_subquery())
        return select(func.coalesce(*parents)).scalar_subquery()

    @staticmethod
    def spender(task: Any) -> Any:
        """Who a row's spend is labeled with: the epoch's actor, else - for rows written
        before per-turn attribution - the task's creator, which is what those rows always meant."""
        return func.coalesce(UsageEvent.actor_id, task.creator_actor_id)

    @classmethod
    async def labels(
        cls, session: AsyncSession, ids: Iterable[uuid.UUID | None], *, workspace_id: uuid.UUID
    ) -> dict[uuid.UUID, tuple[str, str]]:
        """(family, model) per usage event - what each usage line paid for (B6). One query for a page
        of ledger lines; ids that are None (grants, adjustments, an orphaned burn) are skipped.
        Fenced to `workspace_id`, the workspace whose ledger asks: usage_events is a stamp table
        with no read-side RLS and the ledger's FK is single-column, so the read says whose it is."""
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        rows = await session.execute(
            select(cls.id, cls.family, cls.model).where(cls.id.in_(wanted), cls.workspace_id == workspace_id)
        )
        return {row.id: (row.family, row.model) for row in rows}

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[UsageEvent]:
        return list(await session.scalars(select(cls).where(cls.task_id == task_id).order_by(cls.at)))

    @classmethod
    async def task_spend(cls, session: AsyncSession, task_id: uuid.UUID) -> TaskSpend:
        """A task's spend grouped four ways, each a GROUP BY on `ix_usage_events_task_id`: by family,
        by step, by spender (`spender()`: the row's label, else the task's creator) and by native unit
        (a unit count is truncated to an integer per row before it is summed)."""
        from prismoid.db.models.task import Task

        first = func.min(cls.at)
        mine = cls.task_id == task_id
        families = await session.execute(
            select(cls.family, func.sum(cls.cost_usd), func.count())
            .where(mine)
            .group_by(cls.family)
            .order_by(first)
        )
        steps = await session.execute(
            select(cls.step_id, func.sum(cls.cost_usd))
            .where(mine, cls.step_id.is_not(None))
            .group_by(cls.step_id)
            .order_by(first)
        )
        spender = cls.spender(Task)
        spenders = await session.execute(
            select(spender, func.sum(cls.cost_usd))
            .select_from(cls)
            .join(Task, Task.id == cls.task_id, isouter=True)
            .where(mine, spender.is_not(None))
            .group_by(spender)
            .order_by(first)
        )
        unit = func.jsonb_each_text(cls.units).table_valued("key", "value").render_derived()
        units = await session.execute(
            select(unit.c.key, func.sum(func.trunc(cast(unit.c.value, Numeric))))
            .select_from(cls)
            .join(unit, true())
            .where(mine)
            .group_by(unit.c.key)
            .order_by(first, unit.c.key)
        )
        return TaskSpend(
            by_family=[(f, Decimal(c), int(n)) for f, c, n in families.tuples().all()],
            by_step=[(s, Decimal(c)) for s, c in steps.tuples().all() if s is not None],
            by_spender=[(a, Decimal(c)) for a, c in spenders.tuples().all()],
            units=[(k, int(n)) for k, n in units.tuples().all()],
        )

    @classmethod
    async def total_for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> Decimal:
        """A task's spend so far, summed here - the run guard asks before every model request."""
        query = select(func.coalesce(func.sum(cls.cost_usd), 0)).where(cls.task_id == task_id)
        return Decimal(await session.scalar(query) or 0)

    # ── the operator's aggregates (admin surface) ─────────────────────────────────────────────
    # The SQL lives here (queries on the fat model); the COMPOSITION lives in
    # `operations/usage.py`, the ledger's one aggregation home - these are its instruments.
    # All ride `ix_usage_events_at`; per-user reads join the indexed `task_id` and label the row
    # with `spender()` - the epoch's actor, else the task's creator for history.

    @classmethod
    async def window_totals(cls, session: AsyncSession, since: dt.datetime) -> tuple[Decimal, int]:
        row = (
            await session.execute(
                select(func.coalesce(func.sum(cls.cost_usd), 0), func.count()).where(cls.at >= since)
            )
        ).one()
        return Decimal(row[0] or 0), int(row[1] or 0)

    @classmethod
    async def by_model_since(
        cls, session: AsyncSession, since: dt.datetime
    ) -> list[tuple[str, str, Decimal, int]]:
        rows = await session.execute(
            select(cls.provider, cls.model, func.sum(cls.cost_usd), func.count())
            .where(cls.at >= since)
            .group_by(cls.provider, cls.model)
            .order_by(func.sum(cls.cost_usd).desc())
        )
        return [(p, m, Decimal(c or 0), int(n or 0)) for p, m, c, n in rows.tuples().all()]

    @classmethod
    async def top_spenders_since(
        cls, session: AsyncSession, since: dt.datetime, *, limit: int = 10
    ) -> list[tuple[uuid.UUID | None, str | None, str | None, Decimal, int]]:
        """(user_id, email, name, cost, calls), costliest first. Outer joins keep the rows whose
        task (and so whose human) is gone - unattributed spend must not vanish from the total."""
        from prismoid.db.models.task import Task
        from prismoid.db.models.user import User

        rows = await session.execute(
            select(User.actor_id, User.email, User.name, func.sum(cls.cost_usd), func.count())
            .select_from(cls)
            .join(Task, Task.id == cls.task_id, isouter=True)
            .join(User, User.actor_id == cls.spender(Task), isouter=True)
            .where(cls.at >= since)
            .group_by(User.actor_id, User.email, User.name)
            .order_by(func.sum(cls.cost_usd).desc())
            .limit(limit)
        )
        return [(uid, e, n, Decimal(c or 0), int(k or 0)) for uid, e, n, c, k in rows.tuples().all()]

    @classmethod
    async def price_version_counts(cls, session: AsyncSession, since: dt.datetime) -> list[tuple[int, int]]:
        rows = await session.execute(
            select(cls.price_version, func.count())
            .where(cls.at >= since)
            .group_by(cls.price_version)
            .order_by(cls.price_version.desc())
        )
        return [(int(v), int(n or 0)) for v, n in rows.tuples().all()]

    @classmethod
    async def zero_cost_count(cls, session: AsyncSession, since: dt.datetime) -> int:
        """Rows metered at $0 - the price book's blind spot, surfaced (`models doctor`'s twin)."""
        return int(await session.scalar(select(func.count()).where(cls.at >= since, cls.cost_usd == 0)) or 0)

    @classmethod
    async def daily_series(
        cls,
        session: AsyncSession,
        since: dt.datetime,
        *,
        group: str | None = None,
        creator_id: uuid.UUID | None = None,
    ) -> list[tuple[dt.datetime, str | None, Decimal, int]]:
        """(day, key, cost, calls) buckets. `group` = None | "model" | "user"; `creator_id`
        narrows the series to one person's spend - the rows labeled with them (`spender()`: the
        epoch's actor, else the task's creator for history; the admin user-detail's spend block)."""
        from prismoid.db.models.task import Task
        from prismoid.db.models.user import User

        day = func.date_trunc("day", cls.at).label("day")
        if group == "model":
            key = (cls.provider + ":" + cls.model).label("key")
        elif group == "user":
            key = func.coalesce(User.email, "(unattributed)").label("key")
        else:
            key = None

        cols = [day, func.sum(cls.cost_usd), func.count()]
        if key is not None:
            cols.insert(1, key)
        query = select(*cols).where(cls.at >= since)
        if group == "user" or creator_id is not None:
            # Outer, always: a labeled row with no task still counts for its author, so the
            # chart agrees with `total_for_creator`.
            query = query.join(Task, Task.id == cls.task_id, isouter=True)
            if group == "user":
                query = query.join(User, User.actor_id == cls.spender(Task), isouter=True)
        if creator_id is not None:
            query = query.where(cls.spender(Task) == creator_id)
        group_cols = [day] if key is None else [day, key]
        rows = (await session.execute(query.group_by(*group_cols).order_by(day))).all()
        if key is None:
            return [(d, None, Decimal(c or 0), int(n or 0)) for d, c, n in rows]
        return [(d, k, Decimal(c or 0), int(n or 0)) for d, k, c, n in rows]

    @classmethod
    async def total_for_creator(
        cls, session: AsyncSession, creator_id: uuid.UUID, *, since: dt.datetime | None = None
    ) -> Decimal:
        """One person's spend - the rows labeled with them (the epoch's actor; else the
        task's creator, for history) - all-time, or windowed by `since`."""
        from prismoid.db.models.task import Task

        # Two index-driven halves, never `coalesce(actor_id, creator) = X` over an outer join
        # (Postgres cannot index that - it would scan the whole ledger per view): the rows
        # LABELED with them (`ix_usage_events_actor_id`), plus the unlabeled history whose task
        # they created (tasks by creator, then `ix_usage_events_task_id`). Same semantics as
        # `spender(Task) == creator_id`, which stays the row-level definition for the rollups.
        labeled = select(cls.cost_usd.label("cost")).where(cls.actor_id == creator_id)
        history = (
            select(cls.cost_usd.label("cost"))
            .join(Task, Task.id == cls.task_id)
            .where(cls.actor_id.is_(None), Task.creator_actor_id == creator_id)
        )
        if since is not None:
            labeled = labeled.where(cls.at >= since)
            history = history.where(cls.at >= since)
        parts = union_all(labeled, history).subquery()
        return Decimal(await session.scalar(select(func.coalesce(func.sum(parts.c.cost), 0))) or 0)

    @classmethod
    async def total_for_routine(
        cls, session: AsyncSession, routine_id: uuid.UUID, *, since: dt.datetime
    ) -> Decimal:
        """A routine's spend, joined through its minted tasks (a join, never a
        table - `tasks.origin_routine_id` is the provenance the monthly cap reads). Windowed
        always: the cap's question is month-to-date, never all-time."""
        from prismoid.db.models.task import Task

        query = (
            select(func.coalesce(func.sum(cls.cost_usd), 0))
            .join(Task, Task.id == cls.task_id)
            .where(Task.origin_routine_id == routine_id, cls.at >= since)
        )
        return Decimal(await session.scalar(query) or 0)

    @classmethod
    async def for_step(cls, session: AsyncSession, step_id: uuid.UUID) -> list[UsageEvent]:
        """Which models served this step, in order. **The one place that knows for certain.**

        `resolve()` builds a fallback chain, so the model that actually answered is only known
        after it answers - and Rule 8 says every provider call lands here. An artifact's
        provenance reads its `model` from this, not from the plan (which says who we *hoped*
        would serve it) and not from the model (which would be reporting on itself).
        """
        return list(await session.scalars(select(cls).where(cls.step_id == step_id).order_by(cls.at)))


class ProjectUsageDaily(Base):
    """The (project, day, kind) rollup - what `/projects/{id}/usage` reads. Never the ledger."""

    __tablename__ = "project_usage_daily"
    __table_args__ = (UniqueConstraint("project_id", "day", "kind", name="project_usage_daily_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[dt.date] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="total")  # or a family
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 12), nullable=False, default=Decimal("0")
    )  # same precision as the ledger it sums - a rollup that rounds is a rollup that drifts
    calls: Mapped[int] = mapped_column(nullable=False, default=0)

    @classmethod
    async def bump(
        cls, session: AsyncSession, *, project_id: uuid.UUID, day: dt.date, kind: str, cost: Decimal
    ) -> None:
        """Add to the rollup, atomically, with no read-modify-write.

        `INSERT … ON CONFLICT DO UPDATE SET cost = cost + :cost` - one statement, one round trip,
        and **no race**. A read-then-write would lose increments under exactly the concurrency the
        frontier fan-out creates: eight steps metering at once is eight writers to the same
        (project, day) row, and a lost increment is money that vanishes from the graph but not from
        the bill.
        """
        stmt = insert(cls).values(
            id=uuid.uuid4(), project_id=project_id, day=day, kind=kind, cost_usd=cost, calls=1
        )
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="project_usage_daily_key",
                set_={
                    "cost_usd": cls.__table__.c.cost_usd + cost,
                    "calls": cls.__table__.c.calls + 1,
                },
            )
        )

    @classmethod
    async def month_to_date(cls, session: AsyncSession, project_id: uuid.UUID, *, on: dt.date) -> Decimal:
        """The project's total spend so far this calendar month, from the rollup. The rollup is
        for the graph; the ledger is for the truth - and a test proves they agree to the cent.

        **No upper bound on `day`, and that is deliberate.** "Month to date" is everything from the
        1st onward; bounding it by `on` (a Python `date.today()`) drops any row the *database* stamped
        with a later clock - which is exactly what happened the first time: the rollup held today's
        $0.0488, the app clock was a day behind, and `day <= on` filtered it to zero while the ledger
        showed the truth. The month is the filter; "today" only picks which month.
        """
        first = on.replace(day=1)
        total = await session.scalar(
            select(func.coalesce(func.sum(cls.cost_usd), 0)).where(
                cls.project_id == project_id, cls.kind == "total", cls.day >= first
            )
        )
        return Decimal(total or 0)
