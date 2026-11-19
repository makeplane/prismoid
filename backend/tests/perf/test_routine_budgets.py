# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Performance budgets - the routines rows (ADR-048).

Two floors, asserted where they can regress:

- **due-scan @1k** - the minute sweep's ONE query (`Routine.due`) must stay index-served as the
  standing population grows: 1 000 active schedule routines are seeded around `now()`, and the
  warm p95 of the scan must sit under 50 ms. This is the partial index `ix_routines_due` doing
  its job; a regression here means every worker minute got slower for every tenant at once.
- **fire → task.created** - the mint path (gate + task + receipt, one transaction) is the part
  of the "fire→task.created < 5 s" promise that lives in-process; the rest is queue latency
  the smoke drill owns. Measured through `ops.fire` (the play button: manual, policy-exempt).

Direct-DB, unlike the HTTP budgets in test_budgets.py, because both floors are below the API:
the sweep has no endpoint, and the mint budget must not blur into uvicorn's overhead. Skipped
(not failed) when the database is unreachable, so the default unit suite stays hermetic. The
conftest time-window cleanup sweeps everything seeded here.
"""

from __future__ import annotations

import datetime as dt
import statistics
import subprocess
import time
import uuid
from decimal import Decimal

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.routine import Routine
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import routines as ops
from prismoid.schemas.routines import RunDecision
from tests.support.cast import member

# BOTH markers, deliberately: `perf` so the default suite's `-m "not perf"` skips this file,
# and `integration` so conftest's time-window `scratch` sweeps what it seeds - the first run
# without it left 1 000 routines + 400 minted tasks in the dev DB, and the suite's template
# clones inherited them until the sweeper test's 100-row page silently overflowed.
pytestmark = [pytest.mark.perf, pytest.mark.integration]

SEEDED = 1_000
DUE_SCAN_BUDGET_MS = 50.0
MINT_BUDGET_MS = 5_000.0


@pytest.fixture(autouse=True, scope="module")
def burn_ban() -> None:
    """Not hygiene - a burn ban: this file once seeded 500 past-due routines
    while a dev worker ran, its minute sweep fired ALL of them, and ~410 real haiku runs cost
    $16 in five minutes. The due seeds are live bait and the fire test's minted tasks are
    runnable; neither may share a DB with a live sweeper. (`just perf` needs the API for the
    HTTP budgets - it never needs the worker.)"""
    if subprocess.run(["pgrep", "-f", "prismoid run worker"], capture_output=True).returncode == 0:
        pytest.skip("a dev worker is running - its sweep would FIRE this file's seeds; stop it first")


async def _db_or_skip() -> None:
    try:
        async with session_scope() as session:
            await session.connection()
    except Exception:
        pytest.skip("database unreachable - run `just perf` with the stack up")


def _p95(samples: list[float]) -> float:
    samples = sorted(samples)
    return samples[int(0.95 * len(samples)) - 1]


async def test_due_scan_p95_under_50ms_at_1k() -> None:
    await _db_or_skip()
    now = utcnow()
    async with session_scope() as session:
        owner, _ = await member(session, "Perf Seeder")  # routines are tenant rows: bound to their workspace
        # Half due, half future - the scan must pick the due half through the partial index,
        # not walk the population. Seeded through the model (no per-row cron math, no
        # max_active ceiling - this is a data shape, not a thousand user actions).
        session.add_all(
            Routine(
                name=f"perf-{uuid.uuid4().hex[:12]}",
                instruction="perf probe",
                owner_actor_id=owner.id,
                trigger={"cron": "0 9 * * *", "every_s": None, "at": None, "timezone": "UTC"},
                budget_usd_per_run=Decimal("2.0"),
                next_fire_at=now + dt.timedelta(seconds=-30 if i % 2 else 3_600),
            )
            for i in range(SEEDED)
        )
        await session.commit()

        rows = await Routine.due(session, limit=500)
        assert len(rows) == 500  # the due half saturates the sweep's page
        for _ in range(2):  # warm
            await Routine.due(session, limit=500)
        samples: list[float] = []
        for _ in range(30):
            start = time.perf_counter()
            rows = await Routine.due(session, limit=500)
            samples.append((time.perf_counter() - start) * 1000)

    p95 = _p95(samples)
    print(
        f"\n  due-scan @{SEEDED}: mean {statistics.mean(samples):.1f}ms  p95 {p95:.1f}ms  budget {DUE_SCAN_BUDGET_MS:.0f}ms"
    )
    assert p95 < DUE_SCAN_BUDGET_MS, (
        f"due-scan p95 {p95:.1f}ms exceeds {DUE_SCAN_BUDGET_MS:.0f}ms at {SEEDED} routines"
    )


async def test_fire_to_task_created_under_budget() -> None:
    await _db_or_skip()
    async with session_scope() as session:
        me, _ = await member(session, "Perf Firer")
        routine = await ops.create_routine(
            session,
            me,
            name=f"perf-fire-{uuid.uuid4().hex[:8]}",
            instruction="perf probe - never runs (null dispatcher)",
            schedule={"cron": "0 9 * * *", "every_s": None, "at": None, "timezone": "UTC"},
        )
        await session.commit()

        samples: list[float] = []
        for _ in range(12):
            start = time.perf_counter()
            result = await ops.fire(session, me, routine.id)  # bare = the play button
            assert result.decision == RunDecision.MINTED and result.task_id is not None
            task = await session.get(Task, result.task_id)
            assert task is not None  # created, visible - the promise being timed
            samples.append((time.perf_counter() - start) * 1000)

    p95 = _p95(samples)
    print(
        f"\n  fire→task.created: mean {statistics.mean(samples):.1f}ms  p95 {p95:.1f}ms  budget {MINT_BUDGET_MS:.0f}ms"
    )
    assert p95 < MINT_BUDGET_MS, f"fire→task.created p95 {p95:.1f}ms exceeds {MINT_BUDGET_MS:.0f}ms"
