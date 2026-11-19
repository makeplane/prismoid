# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The boot pulse runs before `durable.launch()`, when DBOS cannot
list its queues - asking anyway logged a traceback for an expected condition."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.jobs import durable, heartbeat
from prismoid.runner import worker


@pytest.fixture
def beats(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def scope(**_: Any) -> AsyncGenerator[Any]:
        async def commit() -> None:
            return None

        yield SimpleNamespace(commit=commit)

    async def pulse(_session: Any, _name: str, *, info: dict[str, Any]) -> None:
        seen.append(info)

    monkeypatch.setattr(heartbeat, "session_scope", scope)
    monkeypatch.setattr(heartbeat.Beat, "pulse", pulse)
    return seen


async def test_the_boot_pulse_never_asks_dbos_for_its_queues(
    monkeypatch: pytest.MonkeyPatch, beats: list[dict[str, Any]]
) -> None:
    calls: list[Any] = []

    async def listing(*args: Any, **kw: Any) -> list[Any]:
        calls.append(kw)
        return []

    monkeypatch.setattr(durable, "runs", listing)
    await heartbeat.pulse_now(vitals=False)
    # recorded, not raised: _queue_vitals swallows exceptions, so a raise could never fail this
    assert calls == [] and beats == [{"version": settings.worker.app_version}]


async def test_a_scheduled_pulse_still_carries_the_queues(
    monkeypatch: pytest.MonkeyPatch, beats: list[dict[str, Any]]
) -> None:
    async def listing(*_: Any, **__: Any) -> list[Any]:
        return []

    monkeypatch.setattr(durable, "runs", listing)
    await heartbeat.pulse_now()
    assert beats == [{"version": settings.worker.app_version, "queues": {}, "clogged": [], "ghosts": 0}]


def test_the_worker_boots_with_the_vitals_off() -> None:
    assert "pulse_now(vitals=False)" in Path(worker.__file__).read_text()
