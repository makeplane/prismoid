# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The reconciler's job asks the engine which Machines still have a sleep queued or in flight.
Unknown is never "none": a listing that fails is None, and the reconciler
then treats every sleeping row as having its job."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prismoid.jobs import durable
from prismoid.jobs import machines as machine_jobs
from prismoid.schemas.machines import sleep_job_id


def _run(key: str, **kwargs: Any) -> durable.Run:
    return durable.Run(key, "machine_seed", "queued", "machines", None, None, args=(), kwargs=kwargs)


async def test_the_sleep_jobs_are_read_from_the_queued_and_pending_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt

    a, b = uuid.uuid4(), uuid.uuid4()
    seen: dict[str, Any] = {}

    async def listing(name: str | None = None, **kw: Any) -> list[durable.Run]:
        seen.update(kw, name=name)
        now = dt.datetime.now(dt.UTC)
        ids = [sleep_job_id(a, 1, now), sleep_job_id(b, 4, now), "machine-sleep-garbage"]
        return [_run(w) for w in ids]

    monkeypatch.setattr(durable, "runs", listing)
    assert await machine_jobs.sleeping_jobs() == frozenset({a, b})
    # Delayed counts too: DBOS 2.26 holds an enqueue with a delay there, and it is still active.
    assert set(seen["states"]) == {"queued", "running", "delayed"} and seen["name"] == "machine_sleep"


async def test_the_seeding_forks_are_read_from_the_queued_and_pending_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed's hold names its child Machine (`seed:<child>`), and so does the job's own input."""
    child, other = uuid.uuid4(), uuid.uuid4()
    seen: dict[str, Any] = {}

    def seed(**kwargs: Any) -> durable.Run:
        return _run(f"machine-seed-{uuid.uuid4()}", **kwargs)

    listed = [
        seed(parent_machine_id=str(uuid.uuid4()), child_machine_id=str(child)),
        seed(parent_machine_id=str(uuid.uuid4()), child_machine_id=str(other)),
    ]

    async def listing(name: str | None = None, **kw: Any) -> list[durable.Run]:
        seen.update(kw, name=name)
        return listed

    monkeypatch.setattr(durable, "runs", listing)
    assert await machine_jobs.seeding_jobs() == frozenset({child, other})
    assert set(seen["states"]) == {"queued", "running", "delayed"} and seen["name"] == "machine_seed"
    assert seen["load_input"] is True
    # An active seed whose input names no child could be any fork's: unknown, never "not seeding".
    unloaded = durable.Run("x", "machine_seed", "queued", "machines", None, None)
    for unreadable in (seed(child_machine_id="garbage"), unloaded):
        listed[1:] = [unreadable]
        assert await machine_jobs.seeding_jobs() is None


async def test_a_listing_that_fails_is_unknown_never_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(*args: Any, **kw: Any) -> list[Any]:
        raise RuntimeError("Executor accessed before DBOS was launched")

    monkeypatch.setattr(durable, "runs", down)
    assert await machine_jobs.sleeping_jobs() is None
    assert await machine_jobs.seeding_jobs() is None


def test_a_recovered_pass_never_judges_on_its_recorded_listings() -> None:
    import datetime as dt

    from prismoid.conf import settings

    tick = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.UTC)
    interval = dt.timedelta(seconds=settings.machines.reconcile_interval_s)
    assert not machine_jobs.replayed(tick, tick + dt.timedelta(seconds=1)), "a pass on its own tick"
    assert machine_jobs.replayed(tick, tick + 3 * interval), "recovered: a newer tick has run"
