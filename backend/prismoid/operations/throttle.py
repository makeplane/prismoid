# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The burst doors' limiters, two kinds.

`FixedWindowLimiter` is in-process, for the doors whose caller is already known (the actor
throttle, the routine fire door, workspace founding): per-replica, a speed bump against loops,
its limit divided across replicas so the aggregate cap stays the advertised number
behind an evenly-distributing LB. A map whose keys callers choose must EVICT, or it is a
restart-only memory leak: past the floor, every key whose window went stale is dropped -
amortised, so the sweep never rides a hot small-map path.

`take` / `failed` / `succeeded` are durable, for the auth doors (login, signup, the reset pair)
whose callers are strangers: a Postgres row per bucket (`AuthThrottle`), so every replica and a
restart count the same window, and a run of failures earns a growing backoff. Each call is its own
short committed transaction on its own session: the door's own unit of work usually ends in a
refusal and a rollback, and a counted attempt must survive that.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.auth_throttle import AuthThrottle, BucketState
from prismoid.db.session import session_scope
from prismoid.errors import RateLimited

_WINDOW = dt.timedelta(minutes=1)
_SWEEP_THRESHOLD = 4096


class FixedWindowLimiter:
    """One instance per door; the key is the door's own bucket grammar (an actor id, an
    `email|ip` pair, a routine id). `hit` raises `RateLimited` past the per-replica share."""

    def __init__(self, what: str) -> None:
        self._hits: defaultdict[Hashable, list[dt.datetime]] = defaultdict(list)
        self._what = what

    def hit(self, key: Hashable, *, per_minute: int, message: str | None = None) -> None:
        now = utcnow()
        window = now - _WINDOW
        recent = [t for t in self._hits[key] if t > window]
        self._hits[key] = recent
        limit = math.ceil(per_minute / settings.api.replica_count)
        if len(recent) >= limit:
            retry_after = max(1, int((min(recent) + _WINDOW - now).total_seconds()))
            raise RateLimited(message or f"too many {self._what}", retry_after=retry_after)
        recent.append(now)
        if len(self._hits) > _SWEEP_THRESHOLD:  # evict every key whose whole window went stale
            for stale in [k for k, hits in self._hits.items() if not any(t > window for t in hits)]:
                del self._hits[stale]


# ── the durable auth buckets ──────────────────────────────────────────────────────────────────

# A run of failures this long without another one is forgotten; a bucket idle this long is swept.
_FORGET = dt.timedelta(hours=1)
_IDLE = dt.timedelta(days=1)


@dataclass(frozen=True)
class Bucket:
    """One durable bucket: its name (the door's grammar), its per-minute line, and how many failures
    in a row start its backoff (0: failures never block it)."""

    key: str
    per_minute: int
    backoff_after: int = 0


async def take(*buckets: Bucket, message: str) -> None:
    """Spend one attempt in every bucket and commit, then raise `RateLimited` if any is over its
    line or inside a backoff, with the longest wait among them as `retry_after`. Commits its own
    transaction on its own session; leaves no lock held."""
    async with session_scope(platform=True) as session:
        states = [(bucket, await AuthThrottle.hit(session, bucket.key, _WINDOW)) for bucket in buckets]
        await session.commit()
    waits = [_wait_s(bucket, state) for bucket, state in states]
    if longest := max(waits, default=0):
        raise RateLimited(message, retry_after=longest)


def _wait_s(bucket: Bucket, state: BucketState) -> int:
    """Seconds until this bucket admits again: 0 when it admits now."""
    wait = 0.0
    if state.blocked_until is not None and state.blocked_until > state.now:
        wait = (state.blocked_until - state.now).total_seconds()
    if state.hits > bucket.per_minute:
        wait = max(wait, (state.window_start + _WINDOW - state.now).total_seconds())
    return math.ceil(wait) if wait > 0 else 0


async def failed(*buckets: Bucket) -> None:
    """Count a failed attempt in every bucket that backs off. Commits its own transaction."""
    auth = settings.auth
    async with session_scope(platform=True) as session:
        for bucket in buckets:
            if bucket.backoff_after > 0:
                await AuthThrottle.fail(
                    session,
                    bucket.key,
                    after=bucket.backoff_after,
                    base_s=auth.login_backoff_base_seconds,
                    max_s=auth.login_backoff_max_seconds,
                    forget=_FORGET,
                )
        await session.commit()


async def succeeded(*buckets: Bucket) -> None:
    """End the buckets' failure runs and any backoff. Commits its own transaction."""
    async with session_scope(platform=True) as session:
        for bucket in buckets:
            await AuthThrottle.clear_failures(session, bucket.key)
        await session.commit()


async def forget(prefix: str) -> None:
    """Delete every bucket whose name starts with `prefix`: their windows, failure runs and
    backoffs. Commits its own transaction."""
    async with session_scope(platform=True) as session:
        await AuthThrottle.delete_prefixed(session, prefix)
        await session.commit()


async def purge_stale(session: AsyncSession) -> int:
    """Delete buckets idle for a day (no hit, failure or live backoff). Commits; returns the count.
    Callers choose bucket names (an address, an email), so without this the table only grows."""
    purged = await AuthThrottle.purge_stale(session, _IDLE)
    await session.commit()
    return purged
