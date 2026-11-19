# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Rate limits - the in-process fixed-window limiter and the per-actor backstop on it.

The tests drive the throttle functions directly with a unique key per test (the windows are
module-level state), so they are deterministic and need no clock control: N calls pass, the N+1th
raises `RateLimited`, and the error carries a positive `retry_after` so a client is told when to come
back rather than left to hammer. The auth doors' durable buckets are tested against Postgres in
`tests/integration/api/test_auth_throttle.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from prismoid.api import deps
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.errors import RateLimited


def test_per_actor_backstop_raises_after_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.api, "per_actor_rate_limit_per_minute", 3)
    actor_id = uuid.uuid4()
    for _ in range(3):
        deps.throttle_actor(actor_id)
    with pytest.raises(RateLimited) as caught:
        deps.throttle_actor(actor_id)
    assert caught.value.retry_after > 0


def test_per_actor_backstop_is_scoped_per_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.api, "per_actor_rate_limit_per_minute", 3)
    a, b = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        deps.throttle_actor(a)
    deps.throttle_actor(b)  # a's window does not throttle b


def test_the_backstop_divides_by_replica_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three replicas behind an even load balancer must not triple the fleet ceiling."""
    monkeypatch.setattr(settings.api, "per_actor_rate_limit_per_minute", 9)
    monkeypatch.setattr(settings.api, "replica_count", 3)
    actor_id = uuid.uuid4()
    for _ in range(3):  # ceil(9 / 3) = 3 per replica
        deps.throttle_actor(actor_id)
    with pytest.raises(RateLimited):
        deps.throttle_actor(actor_id)


def test_stale_buckets_are_evicted_past_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A door whose callers choose the keys mints one bucket per key, so the map must evict or it
    is a restart-only memory leak."""
    from prismoid.operations import throttle

    monkeypatch.setattr(throttle, "_SWEEP_THRESHOLD", 4)
    limiter = throttle.FixedWindowLimiter("attempts")
    stale_when = utcnow() - dt.timedelta(minutes=5)
    hits = limiter._hits  # pyright: ignore[reportPrivateUsage]
    for n in range(5):
        hits[f"stale-{n}"] = [stale_when]
    limiter.hit(f"fresh-{uuid.uuid4()}", per_minute=10)
    assert [k for k in hits if str(k).startswith("stale-")] == []
