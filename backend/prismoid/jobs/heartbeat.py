# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker's pulse (a status must be honest about the worker itself).

A queued task with no worker looks exactly like a queued task with a busy one - the queue
cannot report an empty consumer set. So the worker states its own liveness: touch one row a
minute, and let /healthz translate its age. This exists because a task once sat at
`created` behind a stopped worker while the UI shimmered "working…" - a hang the product
could neither see nor say.

A fresh beat proves the scheduler ran and the DB accepted a write - not that every queue
consumer is unwedged. Constants live on `Beat` so the API can read them without importing jobs.
"""

from __future__ import annotations

import datetime as dt
import time

import logfire

from prismoid.conf import settings
from prismoid.db.models.beat import WORKER_BEAT, Beat
from prismoid.db.session import session_scope
from prismoid.jobs import durable

# A queue is CLOGGED when work is enqueued, nothing is running, and the oldest enqueued row has
# waited past this - the signature of ghosts eating the global cap or a dead consumer, and the
# exact silent state that froze the steps queue.
CLOG_AFTER_S = 120


async def pulse_now(*, vitals: bool = True) -> None:
    """Touch the beat row immediately. The boot pulse (so a cold start never reads as offline)
    runs BEFORE `durable.launch()`, when the engine cannot list its queues: it passes `vitals=False` and the
    beat carries none until the first scheduled pulse a minute later (B4 - asking anyway logged a
    traceback for an expected condition). Every beat names the version this worker dequeues, so
    the API can tell when the two disagree (the API stamps every turn with its own, and
    a worker of another takes none of them)."""
    info = {"version": settings.worker.app_version, **(await _queue_vitals() if vitals else {})}
    async with session_scope(platform=True) as session:  # the worker's own row, no workspace's
        await Beat.pulse(session, WORKER_BEAT, info=info)
        await session.commit()


async def _queue_vitals() -> dict[str, object]:
    """The queues as DBOS sees them, per beat. Best-effort: a failed read costs the
    payload, never the pulse - but a clog found IS logged loudly right here, because the beat
    may be the only process that ever looks."""
    try:
        ours = durable.version()
        queues: dict[str, dict[str, int]] = {}
        ghosts = 0
        now = time.time()
        for run in await durable.runs(states=("queued", "running"), limit=500):
            if run.version and run.version != ours:
                ghosts += 1
            if not run.queue:
                continue
            q = queues.setdefault(run.queue, {"enqueued": 0, "running": 0, "oldest_wait_s": 0})
            if run.state == "queued":
                q["enqueued"] += 1
                waited = int(now - (run.created_at_ms or now * 1000) / 1000)
                q["oldest_wait_s"] = max(q["oldest_wait_s"], max(0, waited))
            else:
                q["running"] += 1

        clogged = [
            name
            for name, q in queues.items()
            if q["enqueued"] > 0 and q["running"] == 0 and q["oldest_wait_s"] > CLOG_AFTER_S
        ]
        if clogged or ghosts:
            logfire.warn(
                "queue vitals: clogged={clogged} ghosts={ghosts} ({queues})",
                clogged=clogged,
                ghosts=ghosts,
                queues=queues,
            )
        return {"queues": queues, "clogged": clogged, "ghosts": ghosts}
    except Exception:
        logfire.exception("queue vitals read failed; the pulse carries none")
        return {}


@durable.schedule("* * * * *")  # every minute - /healthz uses Beat.STALE_AFTER_S
@durable.job("worker_heartbeat")
async def worker_heartbeat(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Touch the beat row, carrying the queues' vital signs. Returns None (scheduled must).

    **Rule 5, deliberate exception:** the pulse's session I/O runs in this workflow body, not an
    `@durable.effect`. The whole point of a heartbeat is to prove a LIVE write this minute;
    journaling it would let a replay re-report an old beat as fresh. It is idempotent (one row,
    upsert), so inlining costs no durability and keeps the liveness signal honest.
    """
    await pulse_now()
