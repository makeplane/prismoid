# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ledger - **the only place usage is written or aggregated**.

Two rules, and they are the whole file:

1. **Every provider call emits a `UsageEvent`**. The `MeteredModel` guarantees the call
   happens; this guarantees it lands.
2. **No aggregation outside here + the rollup.** A dashboard that scans `usage_events` is a
   dashboard that gets slower every day you use the product. Reads come from
   `project_usage_daily`; the ledger is for the truth, not for the graph.

**The ledger law**: a row is attributed **at the moment of spend** and is never rewritten.
`project_id` is copied onto the row, not joined through `tasks` - so moving a task between projects
cannot change March's invoice in April.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.db.models.usage import ProjectUsageDaily, UsageEvent
from prismoid.db.session import row_scope
from prismoid.operations import allowance
from prismoid.schemas.events import UsageRecorded
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import BudgetFigures
from prismoid.schemas.usage import CostSummary, PersonSpend, Unit, UsageRecord


async def record(session: AsyncSession, usage: UsageRecord) -> UsageEvent:
    """One provider call → one ledger row → one `usage.recorded` event on the stream.

    The event is not decoration. A user watching a task should see the money move as it is spent,
    not discover it on an invoice - that is the difference between a budget and a surprise.
    """
    row = UsageEvent(
        id=uuid.uuid4(),
        task_id=usage.task_id,
        step_id=usage.step_id,
        project_id=usage.project_id,
        conversation_id=usage.conversation_id,
        # The spend label: who opened the epoch this call's work had applied (step,
        # then task, then turn author) - a report column, never a wallet.
        actor_id=UsageEvent.epoch_actor(usage.task_id, usage.step_id),
        provider=usage.provider,
        model=usage.model,
        family=str(usage.family),
        units={str(unit): count for unit, count in usage.units.items()},
        cost_usd=usage.cost_usd,
        price_version=usage.price_version,
        at=usage.at,
        # The ledger stamp copies its parent's workspace in the INSERT; with neither parent
        # it is the bound workspace's machinery spend, and only an unbound/platform unit's stays NULL.
        workspace_id=UsageEvent.parent_workspace(
            usage.task_id, usage.conversation_id, bound=tenant.bound_workspace(session)
        ),
    )
    session.add(row)

    # LOCK ORDER: the task row (its event seq), then the Project's rollup rows. A step's transaction
    # often holds the task row already (an event earlier in it) when it records a call, so a
    # `record` that bumped the rollup first and then waited for the task row deadlocked a sibling
    # doing the reverse. This is the rollup's one writer.
    task = await session.get(Task, usage.task_id) if usage.task_id is not None else None
    seq = await task.next_seq(session) if task is not None else None

    # The rollup is bumped in the same transaction as the ledger row. Two transactions would let
    # a crash between them leave the graph disagreeing with the truth, and the graph is what
    # people look at.
    if usage.project_id is not None:
        await ProjectUsageDaily.bump(
            session, project_id=usage.project_id, day=usage.at.date(), kind="total", cost=usage.cost_usd
        )
        await ProjectUsageDaily.bump(
            session,
            project_id=usage.project_id,
            day=usage.at.date(),
            kind=str(usage.family),
            cost=usage.cost_usd,
        )

    if task is not None and seq is not None:
        await TaskEvent.append(
            session,
            UsageRecorded(
                seq=seq,
                task_id=TaskId(task.id),
                step_id=StepId(usage.step_id) if usage.step_id else None,
                project_id=ProjectId(usage.project_id) if usage.project_id else None,
                at=usage.at,
                provider=usage.provider,
                model=usage.model,
                family=usage.family,
                units=usage.units,
                cost_usd=usage.cost_usd,
            ),
        )

    return row


async def for_task(session: AsyncSession, task_id: uuid.UUID) -> CostSummary:
    """What a task cost, by family, by step, by unit and by person. The database groups the ledger
    (`UsageEvent.task_spend`); the people's names come in one query."""
    spend = await UsageEvent.task_spend(session, task_id)
    names = await Actor.names(session, (actor_id for actor_id, _ in spend.by_spender))
    people = [
        PersonSpend(actor_id=actor_id, name=names.get(actor_id, "Someone"), usd=usd)
        for actor_id, usd in spend.by_spender
    ]
    people.sort(key=lambda p: p.usd, reverse=True)
    return CostSummary(
        total_usd=sum((usd for _, usd, _ in spend.by_family), Decimal("0")),
        by_family={Family(family): usd for family, usd, _ in spend.by_family},
        by_step={StepId(step): usd for step, usd in spend.by_step},
        units={Unit(unit): count for unit, count in spend.units},
        calls=sum(calls for _, _, calls in spend.by_family),
        by_person=tuple(people),
    )


async def routine_month_to_date(
    session: AsyncSession, routine_id: uuid.UUID, *, since: dt.datetime
) -> Decimal:
    """The monthly-cap gate's read: a routine's spend this calendar month, joined
    through `tasks.origin_routine_id`. Composed here because aggregation lives in this
    file or the rollup; the SQL is the ledger's own instrument."""
    return await UsageEvent.total_for_routine(session, routine_id, since=since)


# ── the operator's aggregations (admin surface) ────────────────────────────────────────────────
#
# Aggregation lives HERE or in the rollup - so the admin spend views compose here, from
# the ledger's own query instruments (`UsageEvent.window_totals` and friends - the SQL lives on
# the fat model, the composition lives in this file, and nowhere else reads the ledger). The
# scale path, when the indexed scans outgrow one Postgres: a `usage_daily(day, actor, model)`
# rollup bumped beside `project_usage_daily` in `record()` - these signatures would not change.


def _window(days: int) -> dt.datetime:
    from prismoid.db.base import utcnow

    return utcnow() - dt.timedelta(days=max(1, min(days, 365)))


async def admin_summary(session: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    """The money, at a glance: window totals, MTD, today, top spenders, by-model, and the
    price-version coverage that says whether anything is metering at $0."""
    from prismoid.db.base import utcnow

    now = utcnow()
    since = _window(days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total, calls = await UsageEvent.window_totals(session, since)
    mtd, _ = await UsageEvent.window_totals(session, month_start)
    today, _ = await UsageEvent.window_totals(session, day_start)

    return {
        "days": days,
        "total_usd": total,
        "calls": calls,
        "mtd_usd": mtd,
        "today_usd": today,
        "by_model": [
            {"provider": p, "model": m, "cost_usd": c, "calls": n}
            for p, m, c, n in await UsageEvent.by_model_since(session, since)
        ],
        "top_spenders": [
            {
                "user_id": uid,
                "email": email or "(unattributed)",
                "name": name or "",
                "cost_usd": c,
                "calls": n,
            }
            for uid, email, name, c, n in await UsageEvent.top_spenders_since(session, since)
        ],
        "price_versions": [
            {"version": v, "calls": n} for v, n in await UsageEvent.price_version_counts(session, since)
        ],
        "zero_cost_calls": await UsageEvent.zero_cost_count(session, since),
    }


async def admin_daily(
    session: AsyncSession,
    *,
    days: int = 30,
    group: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Day buckets for the chart. `group` = None | "model" | "user"; `actor_id` narrows the
    whole series to one person's spend - the rows labeled with them (the turn author, else the
    task's creator for history; the user-detail spend block)."""
    rows = await UsageEvent.daily_series(session, _window(days), group=group, creator_id=actor_id)
    return [{"day": d.date().isoformat(), "key": k, "cost_usd": c, "calls": n} for d, k, c, n in rows]


async def creator_spend(
    session: AsyncSession, creator_id: uuid.UUID, *, month_start: dt.datetime
) -> tuple[Decimal, Decimal]:
    """(all-time, month-to-date) for one person - the admin user-detail's two numbers."""
    total = await UsageEvent.total_for_creator(session, creator_id)
    mtd = await UsageEvent.total_for_creator(session, creator_id, since=month_start)
    return total, mtd


def emitter(session: AsyncSession, *, own_transaction: bool = False):
    """Build the `EmitUsage` callable the engine is handed.

    The engine never sees a session, a row, or a table. It is given a function, it calls it, and
    the money lands. That is the whole of "ai/ is persistence-free" - not a slogan, a closure.

    **`own_transaction` is the difference between the two ways a model gets called, and it is not
    cosmetic.**

    * A model called from a **step** (`execute_step`) runs while that step's session sits *committed
      and idle* - the RUNNING transition was committed before the agent started. Its callbacks fire
      from pydantic-ai's *concurrent* tool execution, so they cannot share that session (`Session is
      already flushing`), and `record` claims a seq via `UPDATE tasks … RETURNING`, whose row lock,
      held across a whole agent run, would deadlock the fan-out's other branch. So: its own
      short transaction. This is also right for the ledger on its own terms - money spent is spent,
      and a step that then fails must not roll back the charge (the general form of a bug patched
      locally before).

    * A model called from a **light planning helper** (`clarify`, `generate_title` via
      `run_light_agent`) runs *synchronously inside the caller's transaction* with no concurrency to
      trip on, so it may share that session. (The MAIN planner, `_plan`, is NOT one of these: it can
      run many provider rounds, so it commits first and takes `own_transaction=True` - the step
      path's posture - rather than hold the task row lock across those awaits.)
    """
    from prismoid.db.session import session_scope

    workspace_id = tenant.bound_workspace(session)  # the own transaction acts for the caller's workspace

    async def emit(usage: UsageRecord) -> None:
        if own_transaction:
            async with session_scope(workspace_id) as own:
                await record(own, usage)
                await own.commit()
        else:
            await record(session, usage)

    return emit


def spend_reader(task_id: uuid.UUID) -> Callable[[], Awaitable[BudgetFigures | None]]:
    """`TaskDeps.spend`: the task's live spend against its OWN cap, and whether its workspace has
    spent its allowance (`allowance.py`, ADR-103), each read on its own short session bound to the
    task's workspace - the step's session sits committed and idle across the run. None when nothing
    bounds the task: no cap (unlimited is a real answer) and no allowance. Under an allowance every
    task gets figures, 0 for no cap, so the allowance coming back disarms the guard. The Project's
    ceiling stays the Bursar's, between attempts and at the gate."""

    async def read() -> BudgetFigures | None:
        from prismoid.operations.gates import budget_of

        async with row_scope(Task, task_id) as session:
            task = await session.get(Task, task_id)
            if task is None:
                return None
            limit = budget_of(task.budget)
            left = await allowance.left(session, task.workspace_id)
            if limit is None and left is None:
                return None
            spent = await UsageEvent.total_for_task(session, task_id)
        return BudgetFigures(
            ceiling_usd=limit or Decimal(0),
            spent_usd=spent,
            exhausted=left is not None and left <= 0,
        )

    return read
