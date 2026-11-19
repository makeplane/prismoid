# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Machine time on the ledger (ADR-063).

A warm frame costs per profile per minute (conf, provisional until Phase 5's pricing). The
Machine is billed segment by segment - `metered_until → now` at every release, sleep, reset of a
warm Machine, reconciler fix and frame found gone - never per minute: every task usage row appends
`usage.recorded` and takes the task row lock (`operations/usage.py:75-93`). A throwaway frame bills
once when it is destroyed. `usage_ops.record` labels the row with the turn's author (1a); task and
Project budgets see it at plan boundaries.

Where a segment is billed follows one lock rule: never take the task's row inside a transaction a
step's open transaction may be waiting on, nor under `lock_briefly`'s timeout. So a hand-back
(`release`, `release_parked`) and the reconciler's fix move the watermark under the Machine's lock
and hand the closed `Segment` out: `bill` records it on the caller's session (which may already
hold the task's row - the step's harvest), `settle` in a transaction of its own (for a caller with
no session, or one that has committed). Both are best-effort: the watermark already moved, so a
segment lost on the way is under-billed, never billed twice. `meter_segment` is for a transaction
that may take the task's row under the Machine's lock (sleep's finisher, reset, a rewake), as their
events already do.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import usage as usage_ops
from prismoid.operations.machines.common import db_now
from prismoid.operations.machines.screen import view_of_lease
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.machines import (
    MachineState,
    MachineSummary,
    Segment,
    parse_prices,
    segment_cost_usd,
    segment_seconds,
)
from prismoid.schemas.model_class import Family
from prismoid.schemas.screen import is_live
from prismoid.schemas.usage import Unit, UsageRecord


def prices() -> dict[str, Decimal]:
    return parse_prices(settings.machines.price_per_min_usd)


def later(watermark: dt.datetime | None, now: dt.datetime) -> dt.datetime:
    """Where a watermark moves to at `now`: never backwards (M1). `now` is the transaction's start,
    and a lock wait can make it older than a watermark another transaction just wrote - the
    segment between is already billed (`segment_seconds` reads it as zero), and the later mark
    stands."""
    return max(watermark or now, now)


async def meter_segment(session: AsyncSession, row: Machine, now: dt.datetime) -> None:
    """Bill `metered_until → now` on this session, then move the watermark to `now` (never
    backwards) - in the caller's transaction, under the Machine's row lock. The caller stops the
    meter (None) when the frame goes away."""
    seconds = segment_seconds(row.metered_until, now)
    row.metered_until = later(row.metered_until, now)
    if seconds > 0:
        await _record(session, row.task_id, row.profile, seconds, now, row.driver)


async def bill(session: AsyncSession, segment: Segment) -> None:
    """Record a closed segment on the CALLER's session, in a SAVEPOINT;
    it commits with the caller. Never raises: a session the failure that ended the step poisoned
    loses this one segment, logged - and never bills it twice, because its watermark already moved."""
    if segment.seconds <= 0:
        return
    try:
        async with session.begin_nested():
            at = segment.at if segment.at is not None else await db_now(session)
            await _record(session, segment.task_id, segment.profile, segment.seconds, at, segment.driver)
    except Exception:
        logfire.exception(
            "task {task}: {s}s of machine time could not be billed",
            task=str(segment.task_id),
            s=segment.seconds,
        )


async def settle(segment: Segment) -> None:
    """Record a closed segment in a transaction of its own, bound to the task's workspace - for a caller
    with no session to bill on, or one that has committed or rolled back (so it holds no lock on the
    task's row this would wait on). Never raises; a task that is gone bills nothing."""
    if segment.seconds <= 0:
        return
    try:
        async with row_scope(Task, segment.task_id) as session:
            if tenant.bound_workspace(session) is None:  # the task is gone
                return
            at = segment.at if segment.at is not None else await db_now(session)
            await _record(session, segment.task_id, segment.profile, segment.seconds, at, segment.driver)
            await session.commit()
    except Exception:
        logfire.exception(
            "task {task}: {s}s of machine time could not be billed",
            task=str(segment.task_id),
            s=segment.seconds,
        )


async def meter_throwaway(task_id: uuid.UUID, profile: str, seconds: int) -> None:
    """A throwaway frame's one bill, when it is destroyed, in a transaction of its own (`settle`)."""
    await settle(Segment(task_id, profile, seconds))


async def _record(
    session: AsyncSession, task_id: uuid.UUID, profile: str, seconds: int, at: dt.datetime, driver: str
) -> None:
    task = await session.get(Task, task_id)
    # The driver that ran it (ADR-063): a Machine's row's, or a throwaway frame's
    # own; "" (`meter_throwaway`) is this worker's. Its price is the conf's for that driver, if any.
    provider = driver or settings.worker.frame_driver
    await usage_ops.record(
        session,
        UsageRecord(
            task_id=TaskId(task_id),
            step_id=None,
            project_id=ProjectId(task.project_id) if task is not None and task.project_id else None,
            conversation_id=None,
            provider=provider,
            model=f"machine.{profile}",
            family=Family("machine"),
            units={Unit.SECOND: seconds},
            cost_usd=segment_cost_usd(seconds, profile, prices(), provider),
            price_version=0,
            at=at,
        ),
    )


async def summary(session: AsyncSession, task_id: uuid.UUID) -> MachineSummary | None:
    found = await Machine.with_usage(session, task_id)
    if found is None:
        return None
    row, seconds, usd, holder_name, now = found
    lease = row.lease()
    return MachineSummary(
        state=MachineState(row.state),
        profile=row.profile,
        state_since=row.state_since,
        last_active_at=row.last_active_at,
        checkpoint_at=row.checkpoint_at,
        checkpoint_bytes=row.checkpoint_bytes,
        browser_saved=row.browser_object is not None,
        warm_seconds=seconds,
        cost_usd=usd,
        control=view_of_lease(lease, holder_name or "")
        if lease is not None and is_live(lease, now)
        else None,
        device_name=row.device_name,
        driver=DEVICE_DRIVER if row.driver == DEVICE_DRIVER else None,
    )
