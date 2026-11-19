# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The auth sweepers - revocable rows accumulate, and someone has to take out the trash.

Opaque Postgres sessions are why we rejected JWT (a kill switch a stateless token cannot have),
but nothing reaped the dead ones - expired and revoked rows sat forever. Every hour, hard-delete
the ones dead past their grace window. Cheap, idempotent, and dumb on purpose: the set to delete
is defined by the clock, so a re-run only ever finds what has newly aged out. Password-reset
rows get the same treatment on the half hour, and so do idle auth throttle buckets.
"""

from __future__ import annotations

import datetime as dt

import logfire

from prismoid.db.session import session_scope
from prismoid.jobs import durable
from prismoid.operations import auth, throttle


@durable.schedule("0 * * * *")  # hourly - session GC is housekeeping, not a live-work rescue
@durable.job("sweep_sessions")
async def sweep_sessions(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Hard-delete auth sessions dead past their grace window. Returns None (scheduled must).

    **Rule 5, deliberate exception:** the session I/O runs in this workflow body, not an
    @DBOS.step effect - the same shape every sweeper uses. The delete targets a clock-defined
    set, so it is idempotent; a replay is harmless and journaling the read buys no durability.
    """
    async with session_scope(platform=True) as session:  # person-level rows, every workspace
        purged = await auth.purge_expired_sessions(session)
    if purged:
        logfire.info("session sweep hard-deleted {n} dead auth session(s)", n=purged)


@durable.schedule("30 * * * *")  # hourly on the half - same housekeeping, offset from the above
@durable.job("sweep_password_resets")
async def sweep_password_resets(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Hard-delete password-reset rows long past expiry. The same deliberate Rule-5
    exception as `sweep_sessions`: a clock-defined delete set is idempotent, so journaling the
    I/O as an effect would buy no durability worth its ceremony."""
    async with session_scope(platform=True) as session:
        purged = await auth.purge_dead_password_resets(session)
        unverified = await auth.purge_dead_email_verifications(session)
        buckets = await throttle.purge_stale(session)
    if purged:
        logfire.info("reset sweep hard-deleted {n} dead password reset(s)", n=purged)
    if unverified:
        logfire.info("reset sweep hard-deleted {n} dead email verification(s)", n=unverified)
    if buckets:
        logfire.info("reset sweep hard-deleted {n} idle auth throttle bucket(s)", n=buckets)
