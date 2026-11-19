# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Beat - a process's pulse. One row per kind ("worker"), overwritten every beat.

The API cannot see the worker (separate process, maybe a separate box); the queue cannot say
"nobody is listening" - an empty consumer set looks exactly like a slow one. A heartbeat row is
the cheapest honest signal: the worker touches it on a schedule, and anyone with a session can
ask how long ago that was. Absence of a fresh beat is the ONLY way the system can say "the
machine is off" instead of letting a queued task impersonate a running one.

**One global name `WORKER_BEAT`** - "any worker is alive", not per-executor presence. Fine for a
single worker; multi-executor would key by `executor_id` later.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, utcnow

# Kind name for the worker process. Beats every minute; /healthz tolerates one miss.
WORKER_BEAT = "worker"
STALE_AFTER_S = 150


class Beat(Base):
    __tablename__ = "system_beats"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    beat_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    # The pulse's payload: the worker's application version and the queues' vital
    # signs as it last saw them - {"version": "<sha>", "queues": {"steps": {"enqueued": n,
    # "running": n, "oldest_wait_s": n}}, "clogged": bool, "ghosts": n}. Written every beat;
    # /healthz translates it. A frozen queue was invisible before this - the only witness was
    # SQL against DBOS's own tables.
    info: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict, server_default="{}")

    @classmethod
    async def pulse(cls, session: AsyncSession, name: str, info: dict[str, Any] | None = None) -> None:
        """Touch the row - insert or overwrite, no read first."""
        stmt = insert(cls).values(name=name, beat_at=utcnow(), info=info or {})
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[cls.name],
                set_={"beat_at": stmt.excluded.beat_at, "info": stmt.excluded.info},
            )
        )

    @classmethod
    async def age_seconds(cls, session: AsyncSession, name: str) -> float | None:
        """Seconds since the last beat, or None if this process has never beaten."""
        at = await session.scalar(select(cls.beat_at).where(cls.name == name))
        if at is None:
            return None
        return max(0.0, (utcnow() - at).total_seconds())

    @classmethod
    async def read(cls, session: AsyncSession, name: str) -> tuple[float | None, dict[str, Any]]:
        """(age_seconds, info) in one query - /healthz's read."""
        row = await session.scalar(select(cls).where(cls.name == name))
        if row is None:
            return None, {}
        return max(0.0, (utcnow() - row.beat_at).total_seconds()), dict(row.info or {})
