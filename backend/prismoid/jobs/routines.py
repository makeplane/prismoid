# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The routine clock (ADR-048): a deliberately dumb minute sweep, and the per-fire workflow
every trigger source converges on.

The sweep is READ-ONLY - it advances nothing, decides nothing, and can race itself, a replica,
or a replay harmlessly: correctness lives entirely in the fire path's idempotent layers (the
run ledger's unique occurrence re-checked under the row lock, `create_task`'s client_token, and
the tick-scoped workflow id `routine-{id}-{occurrence}~{tick}`). Due-ness is judged by
**Postgres `now()`** inside `Routine.due` - the database is the time authority (the pg-boss
lesson) - and so is the gate's own clock (`_db_now`), so worker skew shifts nothing. A fire
that crashes RESUMES under its own id; one that ERRORS terminally is re-offered next tick
under a fresh id instead of wedging its occurrence (a terminally-errored fire once wedged its
occurrence); a poison fire dead-letters alone on the `routines` queue and can never starve
tasks or steps.

The sweep has TWO arms: DUE (next_fire_at has arrived) and DRAIN (a parked overlap=queue fire,
re-offered each tick until the live run settles or the buffer expires). Drain rides the sweep
rather than an in-workflow sleep loop deliberately - a sleeping fire held one of the lane's
eight queue slots for up to a day (eight parks froze every routine instance-wide, the ADR-027
ghost shape) and a deploy's version sweep could cancel it and strand the buffer forever.
The park's only state is the routine row's `buffered_occurrence`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import logfire

from prismoid.conf import settings
from prismoid.db.models.routine import Routine
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import durable
from prismoid.jobs.queues import routine_queue
from prismoid.schemas.routines import RunSource, fire_workflow_id, occurrence_for_clock


@durable.schedule("* * * * *")  # every minute
@durable.job("sweep_routines")
async def sweep_routines(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Enqueue a fire for every due routine and re-offer every parked one. Returns None
    (scheduled must).

    **Rule 5, deliberate exception (the sweeper family's):** the reads run in this workflow
    body, not an @DBOS.step - a sweep must see the CURRENT world each minute, and a journaled
    read replayed would act on a stale one. Safe to be dumb about because every enqueue is a
    deterministic id DBOS dedupes within the tick (`scheduled` is a journaled argument, so a
    replayed sweep re-mints the same ids): re-offering work that is running, done, or already
    enqueued is a no-op, and re-offering work that lost its enqueue - or errored terminally on
    a previous tick - is exactly the rescue.
    """
    if not settings.routines.enabled:
        return  # the kill switch stops dispatch THIS tick - never "after existing schedules finish"
    async with session_scope(platform=True) as session:  # cross-workspace, read-only
        due = await Routine.due(session)
        parked = await Routine.parked(session)
    for routine_id, fire_at in due:
        occurrence = occurrence_for_clock(fire_at)
        key = fire_workflow_id(routine_id, occurrence, tick=scheduled)
        await durable.start(routine_queue, key, fire_routine, str(routine_id), occurrence)
    for routine_id, occurrence in parked:  # the DRAIN arm - the gate decides its fate
        key = fire_workflow_id(routine_id, occurrence, tick=scheduled)
        await durable.start(routine_queue, key, fire_routine, str(routine_id), occurrence)
    if due or parked:
        logfire.info("routine sweep offered {n} fire(s), {p} parked", n=len(due), p=len(parked))


@durable.schedule("* * * * *")  # every minute
@durable.job("sweep_routine_events")
async def sweep_routine_events(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """The event matcher: scan the record's lagged window against every live
    listener and offer each match as a fire. Returns None (scheduled must).

    **Rule 5, deliberate exception** (the sweeps' clause): current-world reads inline. The
    watermark trails `now()` by `event_lag_s` so commit-visibility skew can never hide an
    event; the window advances ONLY after the offers are enqueued (crash → re-scan → the run
    ledger's unique occurrence absorbs the overlap - the graphile pattern). The advance runs
    even on an empty roster, deliberately: the matcher has no per-routine birth filter, so a
    frozen watermark would replay pre-creation history into a listener created later. Latency
    is published honestly: ≤ two sweep intervals plus the lag (the IFTTT lesson).
    """
    import base64

    from prismoid.db.base import utcnow
    from prismoid.db.models.routine import RoutineEventCursor
    from prismoid.operations import routines as ops

    if not settings.routines.enabled:
        return
    until = utcnow() - dt.timedelta(seconds=settings.routines.event_lag_s)
    async with session_scope(platform=True) as session:  # the matcher reads every workspace's record
        since = await RoutineEventCursor.read_or_seed(session, seed_at=until)
        await session.commit()  # the cursor helpers are flush-only; the sweep owns the tx
        if until <= since:
            return
        matches = await ops.match_window(session, since=since, until=until)
    for m in matches:
        await durable.start(
            routine_queue,
            fire_workflow_id(m.routine_id, m.occurrence, tick=scheduled),
            fire_routine_event,
            str(m.routine_id),
            m.occurrence,
            base64.b64encode(m.payload).decode(),
            m.chain_depth,
        )
    async with session_scope(platform=True) as session:
        await RoutineEventCursor.advance(session, to=until)
        await session.commit()
    if matches:
        logfire.info("routine event sweep offered {n} fire(s)", n=len(matches))


@durable.job("fire_routine_event")
async def fire_routine_event(routine_id: str, occurrence: str, payload_b64: str, depth: int) -> str:
    """One receipt, one fire - the event lane's twin of `fire_routine`, carrying
    the fenced snapshot and the chain depth the loop guard reads. Same Rule-5 exception, same
    idempotent layers."""
    import base64

    from prismoid.operations import routines as ops
    from prismoid.schemas.routines import RunDecision

    async with row_scope(Routine, uuid.UUID(routine_id)) as session:  # the routine's workspace mints
        result = await ops.execute_fire(
            session,
            uuid.UUID(routine_id),
            occurrence=occurrence,
            source=RunSource.EVENT,
            payload=base64.b64decode(payload_b64),
            chain_depth=depth,
        )
    if result.decision not in (RunDecision.MINTED, RunDecision.HELD):
        logfire.info(
            "routine {rid} event fire {occ}: {decision} {detail}",
            rid=routine_id,
            occ=occurrence,
            decision=result.decision,
            detail=result.detail,
        )
    return result.decision


@durable.job("fire_routine")
async def fire_routine(routine_id: str, occurrence: str) -> str:
    """One occurrence, one fire - whoever's sweep offered it, however many times.

    **Rule 5, deliberate exception (the sweeps' clause, and one more reason of its own):** the
    gate runs in this workflow body, NOT inside an @DBOS.step - first because the gate must
    judge the CURRENT routine row (a journaled read replayed would act on a stale one), and
    second because the mint's last move is `create_task`, which ENQUEUES the task's own
    workflow - and DBOS forbids starting workflows from inside a step (the first
    fire minted its task, then died on that assertion with the receipt unwritten). A workflow body
    may enqueue children - the sweeper has always done exactly
    that. Crash-resume re-runs the whole gate, and the idempotent layers absorb it: that is
    what they are for.

    A BUFFERED result ends this workflow - the sweep's drain arm re-offers the parked
    occurrence next tick, so no workflow ever sleeps on a queue slot.
    """
    from prismoid.operations import routines as ops

    async with row_scope(Routine, uuid.UUID(routine_id)) as session:  # the routine's workspace mints
        result = await ops.execute_fire(
            session, uuid.UUID(routine_id), occurrence=occurrence, source=RunSource.CLOCK
        )
    if result.decision not in ("minted", "held"):
        logfire.info(
            "routine {rid} fire {occ}: {decision} {detail}",
            rid=routine_id,
            occ=occurrence,
            decision=result.decision,
            detail=result.detail,
        )
    return result.decision
