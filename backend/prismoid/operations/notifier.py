# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The batched NOTIFY hub - one lock acquisition per FLUSH, not per event.

Postgres delivers `NOTIFY` on commit, and to keep notifications in commit order it takes an
`AccessExclusiveLock` on the notify queue at every commit that has one pending. So a system that
does one tiny `pg_notify`-only transaction *per event* serialises all of them on that one lock -
a convoy that collapses throughput at a few thousand writes/second while CPU and disk sit idle
(the documented failure mode; Recall.ai's outages, DBOS's measurements). The fix is not a new
broker: **buffer the payloads and flush them in one transaction.** N `pg_notify` calls inside a
single transaction queue N notifications and take the lock **once**, at that transaction's commit
- lifting the ceiling from ~2.9K/s to ~60K/s at 15–100 ms of added latency.

This batches **Lane 2** (the ephemeral deltas - `operations/live.notify`), which is the
high-frequency path: up to 10 frames/s per step, across every concurrent step of every concurrent
task. Losing a Lane-2 frame is already survivable by design (every stream terminates in
Lane-1 durability), so a dropped batch on a crash costs nothing a reconnect does not heal - which
is why Lane 2 needs no fallback polling. **Lane 1** (durable `TaskEvent.append`) keeps its
in-transaction notify: it is delivered on commit so a subscriber can never see an event a rollback
erased, and it is orders of magnitude lower-frequency, so it never forms the convoy.

Threading: the buffer is a `queue.SimpleQueue` (thread-safe, lock-free), so `enqueue` is callable
from any step on any DBOS worker thread with no await. One daemon thread owns the flush loop and
its own asyncpg connection - isolated from the main and DBOS event loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from typing import Any, Final

import asyncpg
import logfire

from prismoid.conf import settings
from prismoid.db.models.event import NOTIFY_CHANNEL

# Tuning. The interval is the added latency floor for a delta; 20 ms is invisible against the stream budget's
# "first delta < 2 s / no dead air ≤ 10 s". The batch cap bounds one flush's work; the buffer cap
# bounds memory - past it, frames are dropped (Lane-2 loss is survivable, a per-event tx is not).
FLUSH_INTERVAL_S: Final = 0.02
MAX_BATCH: Final = 512
BUFFER_CAPACITY: Final = 8192


class BatchNotifier:
    """Process-global. `start()` in the worker; `enqueue()` from any step; `stop()` at shutdown."""

    def __init__(self, dsn: str, channel: str) -> None:
        self._dsn = dsn
        self._channel = channel
        self._buf: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped = 0
        # Public counters - the batching gauge (frames ÷ flushes = the coalescing ratio, i.e. how
        # many lock acquisitions were saved) and what a test asserts on without reaching inside.
        self.flushed_batches = 0
        self.flushed_frames = 0

    @property
    def running(self) -> bool:
        return self._running

    def pending(self) -> int:
        """Approximate buffered-frame count - the flush latency gauge, and what a test waits on."""
        return self._buf.qsize()

    def enqueue(self, payload: str) -> bool:
        """Buffer one notification. Returns False when the notifier is not running, so the caller
        can fall back to a direct notify (the API process and tests never start it). A full buffer
        drops the frame but still returns True - under the load that fills it, falling back to a
        per-event transaction is the very convoy this exists to avoid."""
        if not self._running:
            return False
        if self._buf.qsize() >= BUFFER_CAPACITY:
            self._dropped += 1
            return True
        self._buf.put(payload)
        return True

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="batch-notifier", daemon=True)
        self._thread.start()
        logfire.info("batch notifier started on {channel}", channel=self._channel)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False  # the loop drains what remains, then exits
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        if self._dropped:
            logfire.warn("batch notifier dropped {n} live frame(s) under load", n=self._dropped)

    # ── the flusher thread ──────────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        conn: Any = None  # an asyncpg.Connection; Any to sidestep its generic typing (as the hub does)
        # Keep running while alive OR while frames remain to drain, so stop() loses nothing queued.
        while self._running or self.pending() > 0:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            batch = self._drain()
            if not batch:
                continue
            try:
                if conn is None or conn.is_closed():
                    conn = await asyncpg.connect(self._dsn)
                await self._flush(conn, batch)
            except Exception:
                # A dropped batch is survivable (Lane-2). Log, drop the connection so the next tick
                # reconnects, and keep the loop alive - a dead flusher is worse than a lost frame.
                logfire.exception("batch notify flush failed; dropping {n} frame(s)", n=len(batch))
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
                conn = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _flush(self, conn: Any, batch: list[str]) -> None:
        # THE batching: one transaction, N pg_notify → N notifications queued, **one** lock at the
        # single commit. Without the explicit transaction asyncpg autocommits each statement and
        # the lock is taken N times - the exact thing this avoids.
        async with conn.transaction():
            for payload in batch:
                await conn.execute("select pg_notify($1, $2)", self._channel, payload)
        self.flushed_batches += 1  # one flush == one transaction == one lock acquisition
        self.flushed_frames += len(batch)

    def _drain(self) -> list[str]:
        out: list[str] = []
        for _ in range(MAX_BATCH):
            try:
                out.append(self._buf.get_nowait())
            except queue.Empty:
                break
        return out


def _worker_dsn() -> str:
    """asyncpg wants a libpq DSN. The worker URL is already `postgresql://…` (DBOS speaks sync
    libpq), so unlike the hub's API URL there is no `+asyncpg` tag to strip."""
    return settings.worker.database_url


notifier = BatchNotifier(_worker_dsn(), NOTIFY_CHANNEL)
