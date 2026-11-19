# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The operator console's health card and alert list (`/v1/admin/health`, `/v1/admin/alerts`),
computed on read from one reading: the worker's beat, the stuck scan and the catalog doctor. It
reads and never acts, so it lives apart from the console's verbs in `admin_ops`."""

from __future__ import annotations

import datetime as dt
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.beat import STALE_AFTER_S, WORKER_BEAT, Beat
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.routine import Routine
from prismoid.db.models.usage import UsageEvent
from prismoid.operations import models as model_ops
from prismoid.operations.admin_ops import stuck_count


class Vitals(NamedTuple):
    """The three facts the health card and the alert list are BOTH computed from.

    Gathered once per read, deliberately: the stuck scan and the catalog doctor are the two
    most expensive queries on this surface, and the dashboard polls health and alerts together
    every 30 s. Two independent readers meant four such queries a tick, per open console.
    """

    beat_age_s: float | None
    beat_info: dict[str, Any]
    stuck_tasks: int
    doctor: list[dict[str, str]]


async def read_vitals(session: AsyncSession) -> Vitals:
    age, info = await Beat.read(session, WORKER_BEAT)
    return Vitals(age, info, await stuck_count(session), await model_ops.doctor(session))


async def admin_health(session: AsyncSession, vitals: Vitals | None = None) -> dict[str, Any]:
    """The machine-readable sibling of `/healthz/full` - same sources, structure instead of
    strings. Queue vitals come off the worker's beat row (the API process cannot ask DBOS
    itself - the scheduler lives in the worker); doctor is the catalog's own physician.

    Carries `alerts` so the dashboard is ONE request against ONE `read_vitals` - the card and
    the list are the same reading, and computing them apart was computing them twice."""
    v = vitals or await read_vitals(session)
    migration = await session.scalar(text("select version_num from alembic_version")) or "unknown"
    age, info = v.beat_age_s, v.beat_info
    worker = {
        "beat_age_s": None if age is None else int(age),
        "status": "never" if age is None else ("ok" if age < STALE_AFTER_S else "stale"),
    }
    queues = [
        {
            "name": name,
            "enqueued": int(q.get("enqueued", 0)),
            "running": int(q.get("running", 0)),
            "oldest_wait_s": int(q.get("oldest_wait_s", 0)),
            "clogged": name in (info.get("clogged") or []),
        }
        for name, q in sorted((info.get("queues") or {}).items())
    ]
    return {
        "migration": migration,
        "worker": worker,
        "queues": queues,
        "version_ghosts": int(info.get("ghosts") or 0),
        "stuck_tasks": v.stuck_tasks,
        "doctor": v.doctor,
        "alerts": await alerts(session, v),
    }


async def alerts(session: AsyncSession, vitals: Vitals | None = None) -> list[dict[str, Any]]:
    """Everything currently asking for a human, computed on read - no storage, no daemon.
    Provider ERROR rate is deliberately absent: the ledger records successful spend; failures
    live in Logfire's httpx/pydantic-ai spans, and this list says so rather than faking it."""
    v = vitals or await read_vitals(session)
    now = utcnow()
    day_ago = now - dt.timedelta(hours=24)
    found: list[dict[str, Any]] = []

    if not settings.routines.enabled:
        found.append(
            {
                "code": "routines-disabled",
                "severity": "warn",
                "count": 1,
                "detail": "PRISMOID_ROUTINES_ENABLED=false - no routine fires anywhere on this instance",
            }
        )
    auto_paused = await Routine.auto_paused_count(session)
    if auto_paused:
        found.append(
            {
                "code": "routines-auto-paused",
                "severity": "warn",
                "count": auto_paused,
                "detail": f"{auto_paused} routine(s) paused themselves (failures or budget) and wait for a human",
            }
        )

    budget_gates = await Gate.open_budget_count(session)
    if budget_gates:
        found.append(
            {
                "code": "budget-gates-open",
                "severity": "warn",
                "count": budget_gates,
                "detail": "tasks parked on an open budget gate, waiting for a human",
            }
        )

    failed, delivered = await TaskEvent.settled_counts_since(session, day_ago)
    if failed:
        found.append(
            {
                "code": "tasks-failed-24h",
                "severity": "warn" if failed <= delivered else "error",
                "count": failed,
                "detail": f"{failed} failed vs {delivered} delivered in the last 24 h",
            }
        )

    if v.stuck_tasks:
        found.append(
            {
                "code": "tasks-stuck",
                "severity": "error",
                "count": v.stuck_tasks,
                "detail": "active tasks with no motion past the stall thresholds - see the console's stuck filter",
            }
        )

    clogged = list(v.beat_info.get("clogged") or [])
    if clogged:
        found.append(
            {
                "code": "queues-clogged",
                "severity": "error",
                "count": len(clogged),
                "detail": "enqueued work, zero consumers: " + ", ".join(clogged),
            }
        )

    unpriced = await UsageEvent.zero_cost_count(session, day_ago)
    if unpriced:
        found.append(
            {
                "code": "unpriced-usage-24h",
                "severity": "warn",
                "count": unpriced,
                "detail": "ledger rows metered at $0 in the last 24 h - check the price book (models doctor)",
            }
        )

    if v.doctor:
        found.append(
            {
                "code": "catalog-doctor",
                "severity": "warn",
                "count": len(v.doctor),
                "detail": "; ".join(sorted({f["check"] for f in v.doctor})),
            }
        )
    return found
