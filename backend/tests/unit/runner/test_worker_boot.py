# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker's boot reconcile (ADR-063) is best-effort, the old reap's law: a failed pass costs
the pass - `machine_reconcile` runs again within the minute - never the boot."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from prismoid.ai import frames
from prismoid.db.base import utcnow
from prismoid.operations import machines
from prismoid.runner import worker
from prismoid.schemas.machines import FrameInfo, ReconcilePlan


@pytest.fixture(autouse=True)
def no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing() -> None:
        return None

    monkeypatch.setattr(worker, "dispose_engine", nothing)
    monkeypatch.setattr(worker, "reset_engine", lambda: None)


def test_a_failed_boot_reconcile_reports_zero_and_the_boot_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down() -> ReconcilePlan:
        raise ConnectionError("postgres is restarting")

    monkeypatch.setattr(machines, "reconcile_once", down)
    assert worker.boot_reconcile() == 0


def test_the_boot_reports_what_the_reconciler_destroyed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def two() -> ReconcilePlan:
        return ReconcilePlan(
            disowned=(
                FrameInfo(ref="machine-a-1", labels={}, created_at=utcnow(), driver="docker"),
                FrameInfo(ref="frame-b", labels={}, created_at=utcnow(), driver="docker"),
            ),
            fixes=(),
        )

    monkeypatch.setattr(machines, "reconcile_once", two)
    assert worker.boot_reconcile() == 2


def test_a_driver_client_that_will_not_close_never_skips_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def pool() -> None:
        seen.append("pool disposed")

    async def stuck() -> None:
        seen.append("close tried")
        raise RuntimeError("channel already torn down")

    async def none() -> ReconcilePlan:
        return ReconcilePlan(disowned=(), fixes=())

    monkeypatch.setattr(worker, "dispose_engine", pool)
    driver = ModuleType("prismoid.ai.frames.remote_driver")  # a loop that made a remote driver's client
    driver.__dict__["close_client"] = stuck
    monkeypatch.setitem(sys.modules, driver.__name__, driver)
    monkeypatch.setattr(frames, "CLIENT_MODULES", (driver.__name__,))
    monkeypatch.setattr(machines, "reconcile_once", none)
    assert worker.boot_reconcile() == 0
    assert seen == ["close tried", "pool disposed"]
