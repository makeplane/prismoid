# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Health probes - the checks whose absence makes the process a liar (`/healthz`).

Lifted out of the view so the endpoint stays a thin call-and-serialize (no raw SQL, no
branching in `api/`). This module owns the probes; the endpoint owns the shape. It is deliberately
transaction-free and tolerant - every probe degrades to a word ("unknown", "error: …") rather than
raising, because a health check that 500s tells a load balancer nothing it can act on.
"""

from __future__ import annotations

from dataclasses import dataclass

import logfire
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.beat import STALE_AFTER_S, WORKER_BEAT, Beat


@dataclass(frozen=True)
class HealthReport:
    """The probe result as a value (schemas-style), never an ORM row and never an API shape - the
    endpoint maps it onto HealthOut. `ok` drives the top-level status; the rest are the details."""

    ok: bool
    database: str
    migration: str
    worker: str
    queues: str


async def probe_bare(session: AsyncSession) -> bool:
    """The load-balancer probe: the database answers and the migration table has a row. Nothing else.

    This is the whole truth `/healthz` tells a stranger. The full report - migration revision,
    worker pulse, queue vitals - is reconnaissance on a public box (the split this module's
    endpoint always promised), so it lives behind an Actor at `/healthz/full` via `probe`.
    """
    try:
        await session.execute(text("select 1"))
        return bool(await session.scalar(text("select version_num from alembic_version")))
    except Exception as exc:
        logfire.warn("healthz: the database did not answer ({kind})", kind=type(exc).__name__)
        return False


async def probe(session: AsyncSession) -> HealthReport:
    """Ping the database, read the migration head, and take the worker's pulse.

    A process that reports healthy while its schema is one revision behind is worse than one that
    reports unhealthy, because a load balancer will send it traffic - so a bad DB or a missing
    migration flips `ok`. The **worker line never does**: this probe gates the API container, and a
    stopped worker must read as "the machine is off", not "the API is sick".
    """
    database = "ok"
    migration = "unknown"
    worker = "unknown"
    queues = "unknown"
    ok = True

    try:
        await session.execute(text("select 1"))
    except Exception as exc:
        database = f"error: {type(exc).__name__}"
        ok = False

    try:
        rev = await session.scalar(text("select version_num from alembic_version"))
        migration = rev or "none"
    except Exception as exc:
        logfire.warn("health: the migration head is unreadable ({kind})", kind=type(exc).__name__)
        migration = "unknown"
        ok = False

    # The worker's pulse now carries the queues' vital signs: a frozen queue used to be
    # indistinguishable from a busy one from every surface the product had. Informational only.
    try:
        age, info = await Beat.read(session, WORKER_BEAT)
        worker = "never" if age is None else "ok" if age < STALE_AFTER_S else f"stale {int(age)}s"
        clogged = list(info.get("clogged") or [])
        ghosts = int(info.get("ghosts") or 0)
        if clogged:
            queues = "CLOGGED: " + ", ".join(clogged)
        elif ghosts:
            queues = f"ok ({ghosts} version ghost(s) pending sweep)"
        elif info.get("queues") is not None:  # the beat also carries the version: not just non-empty
            queues = "ok"
    except Exception as exc:
        logfire.warn("health: the worker beat is unreadable ({kind})", kind=type(exc).__name__)
        worker = "unknown"

    return HealthReport(ok=ok, database=database, migration=migration, worker=worker, queues=queues)


async def version_skew(session: AsyncSession) -> str | None:
    """The one value the API and its workers must read alike, compared: the API stamps
    its application version on every enqueue and a worker dequeues only its own, so while a LIVE
    worker beats another version, every turn the API starts waits for a worker that matches. The
    sentence to say so, or None when they agree, no worker is live, the beat names no version, or
    the API is unpinned (it then stamps nothing, and any worker takes its turns).

    Loud, never fatal: a deploy that rolls the API first runs beside the old worker on purpose until
    the new one beats (deploy/DEPLOY.md), and the skew is exactly what the operator must see then."""
    age, info = await Beat.read(session, WORKER_BEAT)
    worker = info.get("version")
    api = settings.worker.app_version
    if not api or age is None or age >= STALE_AFTER_S or worker is None or worker == api:
        return None
    return f"the live worker runs version {worker!r} but this API stamps {api!r}: its turns wait for a {api!r} worker"
