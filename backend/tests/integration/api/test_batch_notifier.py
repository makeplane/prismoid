# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The batched NOTIFY hub (ADR-012): many frames, ONE lock acquisition per flush.

The property that matters is not "the notification arrives" (a plain pg_notify does that) but
"a hundred frames cost one commit, not a hundred" - that is the whole point, and it is the thing
that lifts the lock-convoy ceiling. So the load-bearing test counts how many transactions the
flush actually opened, and asserts a batch is one.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import asyncpg
import pytest

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.event import NOTIFY_CHANNEL
from prismoid.operations import live
from prismoid.operations.notifier import BatchNotifier, notifier
from prismoid.schemas.events import Progress
from prismoid.schemas.ids import StepId, TaskId

pytestmark = pytest.mark.integration


def _drained(deadline_s: float) -> None:
    """Block until the buffer is empty (a flush has carried everything), so the test need not
    sleep a fixed guess. The flush loop runs on its own thread, so blocking here does not stall it."""
    end = time.monotonic() + deadline_s
    while notifier.pending() > 0 and time.monotonic() < end:
        time.sleep(0.005)


def _frame(task_id: uuid.UUID, n: int) -> Progress:
    step = uuid.uuid4()
    return Progress(
        task_id=TaskId(task_id),
        step_id=StepId(step),
        at=utcnow(),
        after_seq=0,
        channel=f"{step}:progress",
        phase="thinking",
        elapsed_s=float(n),
        detail=f"frame {n}",
    )


async def test_a_batch_of_frames_is_one_transaction() -> None:
    """Enqueue many; assert the flusher carried all of them in a HANDFUL of flushes - each flush
    is one transaction, and that transaction count is exactly what Postgres serialises on. 100
    frames in ≤3 flushes is the whole point: ~33× fewer lock acquisitions."""
    n = BatchNotifier(settings.worker.database_url, NOTIFY_CHANNEL)
    n.start()
    try:
        for i in range(100):
            assert n.enqueue(f'{{"i": {i}}}') is True
        for _ in range(100):  # let the ~20 ms tick fire
            if n.flushed_frames >= 100:
                break
            await asyncio.sleep(0.01)
    finally:
        n.stop()

    assert n.flushed_frames == 100, "every frame must be carried"
    assert n.flushed_batches <= 3, (
        f"100 frames should coalesce into a handful of flushes, got {n.flushed_batches}"
    )


async def test_live_notify_batches_and_reaches_a_listener() -> None:
    """The whole Lane-2 path: live.notify → the batch notifier → pg_notify → a real LISTEN. The
    notifier is running (as it is in the worker), so this exercises the batched path, not the
    fallback."""
    dsn = settings.worker.database_url
    conn = await asyncpg.connect(dsn)
    received: list[str] = []
    got = asyncio.Event()

    def on_notify(_c: object, _pid: int, _chan: str, payload: str) -> None:
        received.append(payload)
        got.set()

    await conn.add_listener(NOTIFY_CHANNEL, on_notify)
    task_id = uuid.uuid4()

    notifier.start()
    try:
        await live.notify(_frame(task_id, 1))
        assert notifier.running, "the batched path, not the fallback, is under test"
        _drained(2.0)
        await asyncio.wait_for(got.wait(), timeout=3)
    finally:
        await conn.remove_listener(NOTIFY_CHANNEL, on_notify)
        await conn.close()
        # leave the shared notifier running for other tests; stopping it is the worker's job

    assert any(str(task_id) in p for p in received), "the delta must arrive on the channel"


async def test_fallback_when_not_running() -> None:
    """With the notifier stopped, enqueue reports False so live.notify takes its own-transaction
    fallback - which is how the API process and unit tests keep working with no flusher thread."""
    n = BatchNotifier(settings.worker.database_url, NOTIFY_CHANNEL)
    assert n.running is False
    assert n.enqueue("x") is False
