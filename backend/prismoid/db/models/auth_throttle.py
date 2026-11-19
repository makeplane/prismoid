# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""AuthThrottle - one burst bucket of an auth door (login, signup, the reset pair): its hits in the
current one-minute window, its run of consecutive failures, and the backoff those earned.

A row in Postgres, not a map in a process: every replica counts the same window, a restart forgets
nothing, and the clock is the database's (`now()`), so two API hosts with drifting clocks cannot
disagree about a window. The bucket name is the door's grammar (`login:acct:<sha256 of the
address>:ip:<sha256 of the client address>`, `login:ip:<client address>`); an address is hashed so the table holds no one's email.
Rows are swept once idle (`purge_stale`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import String, case, delete, func, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, rows_affected


@dataclass(frozen=True)
class BucketState:
    """A bucket right after a hit, read in the same statement that counted it."""

    hits: int
    window_start: dt.datetime
    blocked_until: dt.datetime | None
    now: dt.datetime


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"

    bucket: Mapped[str] = mapped_column(String(160), primary_key=True)
    window_start: Mapped[dt.datetime] = mapped_column(nullable=False)
    hits: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    failures: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    failed_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    blocked_until: Mapped[dt.datetime | None] = mapped_column(default=None)

    @classmethod
    async def hit(cls, session: AsyncSession, bucket: str, window: dt.timedelta) -> BucketState:
        """Count one attempt: a window older than `window` restarts at this hit. One upsert, so
        concurrent attempts on one bucket serialize on its row and none is lost."""
        stale = cls.window_start <= func.now() - window
        stmt = insert(cls).values(bucket=bucket, window_start=func.now(), hits=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[cls.bucket],
            set_={
                "hits": case((stale, 1), else_=cls.hits + 1),
                "window_start": case((stale, func.now()), else_=cls.window_start),
            },
        ).returning(cls.hits, cls.window_start, cls.blocked_until, func.now())
        hits, window_start, blocked_until, now = (await session.execute(stmt)).one()
        return BucketState(hits=hits, window_start=window_start, blocked_until=blocked_until, now=now)

    @classmethod
    async def fail(
        cls,
        session: AsyncSession,
        bucket: str,
        *,
        after: int,
        base_s: int,
        max_s: int,
        forget: dt.timedelta,
    ) -> None:
        """Count one failure. From the `after`-th in a row the bucket blocks for
        base_s * 2^(failures - after) seconds, capped at `max_s`; a run quiet for `forget` starts
        over at one."""
        failures = case(
            (or_(cls.failed_at.is_(None), cls.failed_at < func.now() - forget), 1), else_=cls.failures + 1
        )
        # The exponent is capped before `power`: Postgres evaluates every argument of LEAST, so an
        # uncapped 2^n overflows float8 after about a thousand failures and the UPDATE errors,
        # however small `max_s` is. 2^30 seconds is decades, far past any cap.
        delay_s = func.least(max_s, base_s * func.power(2, func.least(failures - after, 30)))
        blocked = case(
            (failures >= after, func.now() + func.make_interval(0, 0, 0, 0, 0, 0, delay_s)),
            else_=cls.blocked_until,
        )
        await session.execute(
            update(cls)
            .where(cls.bucket == bucket)
            .values(failures=failures, failed_at=func.now(), blocked_until=blocked)
        )

    @classmethod
    async def clear_failures(cls, session: AsyncSession, bucket: str) -> None:
        await session.execute(
            update(cls).where(cls.bucket == bucket).values(failures=0, failed_at=None, blocked_until=None)
        )

    @classmethod
    async def delete_prefixed(cls, session: AsyncSession, prefix: str) -> None:
        await session.execute(delete(cls).where(cls.bucket.startswith(prefix, autoescape=True)))

    @classmethod
    async def purge_stale(cls, session: AsyncSession, idle: dt.timedelta) -> int:
        """Delete buckets idle for `idle`: no hit, no failure and no block within it. Returns the
        count. The set is defined by the clock, so a second sweep finds only what aged out since."""
        cutoff = func.now() - idle
        result = await session.execute(
            delete(cls).where(
                cls.window_start < cutoff,
                or_(cls.failed_at.is_(None), cls.failed_at < cutoff),
                or_(cls.blocked_until.is_(None), cls.blocked_until < func.now()),
            )
        )
        return rows_affected(result)
