# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The gate sweeper - a gate's TTL, enforced.

A gate carries an `expires_at` and an `on_timeout`. Without something that watches the clock, both
are decoration: a task parks on a question, the human never comes back, and it waits forever on a
`recv` that its own 24-hour timeout will eventually release - but *only* the intake gate's workflow
timeout does that, and only for the intake gate. A mid-run gate, a gate whose worker was replaced, a
gate whose task everyone forgot: those need a sweeper.

Every minute, like the orphan sweeper, and for the same reason - cheap, idempotent, and dumb on
purpose. The timeout write takes the task's row lock and re-reads the gate under it (the same guard
`resolve` uses), so a gate a human answered between the scan and the write is left alone - no second
`gate.resolved`, no second wake - and a sweep that races a human loses the race harmlessly.
"""

from __future__ import annotations

import datetime as dt

import logfire

from prismoid.db.session import session_scope
from prismoid.jobs import durable
from prismoid.operations import gates


@durable.schedule("* * * * *")  # every minute
@durable.job("sweep_gates")
async def sweep_gates(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Resolve every expired gate by its own `on_timeout` policy. Returns None (scheduled must).

    **Rule 5, deliberate exception:** the session I/O stays in this workflow body rather than an
    @DBOS.step effect. A scheduled sweep reads the CURRENT expired set each minute; journaling it
    would replay a stale snapshot. It is idempotent (the timeout write re-reads each gate under
    its task's row lock and skips one a human already closed), so a step's durability adds
    nothing and a replayed step would act on old data.
    """
    async with session_scope(
        platform=True
    ) as session:  # cross-workspace; each gate binds its task's workspace
        swept = await gates.sweep(session)
    if swept:
        logfire.info("gate sweep resolved {n} expired gate(s)", n=len(swept))
