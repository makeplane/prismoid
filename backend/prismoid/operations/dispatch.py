# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Dispatcher port (seam 1).

`jobs` sits **above** `operations` - job bodies compose operations through `jobs/effects.py` -
and Rule 4 has operations enqueue after commit. Taken together, `operations → jobs` would be an
upward import and a layering violation.

So the dependency is inverted at exactly one seam: `operations` depends on this Protocol, `jobs`
implements it over DBOS queues, and `runner` (the top layer, which legitimately imports both)
wires the real one at process boot - in **both** processes, because the API enqueues and the
worker executes.

The payoff is not just a green `just layers`. It is that `operations` stays testable with a fake
dispatcher, and that "enqueue after commit" cannot quietly become "enqueue inside the
transaction" - because the only thing an operation can reach is `enqueue`, and it has to call it
somewhere.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
from typing import Any, Protocol
from uuid import UUID

import logfire
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction


class Dispatcher(Protocol):
    """What `operations` is allowed to know about background work: that it can start it."""

    async def enqueue(self, job: str, key: str, *, delay_s: float = 0.0, **kwargs: Any) -> None:
        """Start a durable job with a **deterministic id** (`task-{id}`).

        Deterministic ids are what make a double-enqueue harmless: DBOS dedupes on the id, so the
        sweeper can re-enqueue a task it *thinks* was dropped without any risk of running
        it twice. Ghost-freedom by construction, not by locking.

        `delay_s` holds the job back that long before any worker may take it (0 starts it now). The
        caller decides it, never the dispatcher, and no job takes an argument by that name.

        Async for the same reason `send` is: both implementations write to the DBOS
        system database, and the sync client call was a blocking round-trip ON the event loop -
        every enqueue stalled every other in-flight request for its duration.
        """
        ...

    async def send(self, key: str, payload: Any, topic: str, *, missing_ok: bool = False) -> None:
        """Wake a parked job - a gate resolution, in practice.

        Async because the worker-side implementation must be: DBOS's `send` refuses to run inside
        an event loop, and both callers (the API handler, the sweep workflow) are in one.

        `missing_ok` names the ONE tolerated failure - a wake whose addressee workflow was
        never enqueued (cancel poking a HELD task). Everything else raises: a swallowed send
        is a human's answer silently discarded.
        """
        ...


class NullDispatcher:
    """The default, and an honest one: it records the intent and does nothing.

    It exists so that the API can boot and serve *before* the worker exists, and so that
    a test that does not care about jobs does not have to stand one up. It logs, loudly, so that
    a production process running without a real dispatcher is visible rather than mysterious.
    """

    async def enqueue(self, job: str, key: str, **kwargs: Any) -> None:
        logfire.warn("no dispatcher wired; {job} {key} not enqueued", job=job, key=key)

    async def send(self, key: str, payload: Any, topic: str, *, missing_ok: bool = False) -> None:
        logfire.warn("no dispatcher wired; send to {key} dropped", key=key)


_dispatcher: Dispatcher = NullDispatcher()


def set_dispatcher(dispatcher: Dispatcher) -> None:
    """Called once, at process boot, by `runner/`."""
    global _dispatcher
    _dispatcher = dispatcher


def get_dispatcher() -> Dispatcher:
    return _dispatcher


def task_job_key(task_id: UUID) -> str:
    """`task-{id}` - the deterministic job id. One task, one TURN-1 job, forever."""
    return f"task-{task_id}"


def task_turn_job_key(task_id: UUID, turn: int) -> str:
    """The per-turn workflow id: `task-{id}` for turn 1 - unchanged, so every
    pre-turns workflow, sweep and dedupe keeps working - and `task-{id}-t{n}` for turn ≥ 2.

    It exists because a completed DBOS workflow id never re-executes: a continued task must run
    its next turn as its own durable job. And it is why every wake (gate resolve, resume,
    cancel) must be addressed per turn - a message sent to `task-{id}` while turn 3 is parked
    lands in a workflow that already returned, and the park sleeps its full timeout.
    """
    return task_job_key(task_id) if turn <= 1 else f"task-{task_id}-t{turn}"


def machine_boot_job_key(task_id: UUID, turn: int = 1) -> str:
    """`machine-boot-{task}` - the early boot's job id, per turn as `task_turn_job_key` is: a
    completed boot id never runs again, so a later turn whose task has no Machine needs its own."""
    return f"machine-boot-{task_id}" if turn <= 1 else f"machine-boot-{task_id}-t{turn}"


def step_job_key(step_id: UUID) -> str:
    """`step-{id}` - the deterministic id of a step's `run_step` child workflow.

    Deterministic for the same reason `task-{id}` is: a gate resolution has to wake the *exact*
    workflow that parked on the question, and a mid-run `ask_user` parks `run_step`, not the parent
    `run_task`. Without a known id there is no address to send the answer to. It also keeps DBOS's
    dedupe honest - the same step never runs as two workflows.
    """
    return f"step-{step_id}"


def push_inbox_job_key(item_id: UUID) -> str:
    """`push-inbox-{item}` - one inbox item's push. One item, one push, whoever enqueues it twice."""
    return f"push-inbox-{item_id}"


def push_read_job_key(item_id: UUID) -> str:
    """`push-read-{item}` - the silent push that says this item was read. An item is read once."""
    return f"push-read-{item_id}"


def push_read_all_job_key(actor_id: UUID, workspace_id: UUID, at: dt.datetime) -> str:
    """`push-read-all-{actor}-{workspace}-{at}` - one read-all's silent push, keyed by the moment the
    read stamped, so a second read-all later is its own push."""
    return f"push-read-all-{actor_id}-{workspace_id}-{at.strftime('%Y%m%dT%H%M%S%f')}"


# ── enqueue after commit ─────────────────────────────────────────────────────────────────────

_AFTER_COMMIT = "dispatch.after_commit"  # the session's `info` key: jobs its open transaction asked for
_enqueuing: set[asyncio.Task[None]] = set()  # held strongly: a bare task can be collected mid-flight


def enqueue_after_commit(
    session: AsyncSession, job: str, key: str, *, delay_s: float = 0.0, **kwargs: Any
) -> None:
    """Enqueue `job` once the caller's transaction commits, and never if it rolls back - for a writer
    that runs inside other operations' transactions and cannot see their commit (the inbox).
    `delay_s` rides along to `Dispatcher.enqueue`.

    The enqueue runs off the commit path and is best-effort: a process that dies between the commit
    and the enqueue loses the job, so only work that may be lost rides here (a push is a courtesy;
    the row it announces stands). A job keyed on a row it then re-reads also shrugs off a savepoint
    that rolled the row back under a committed outer transaction."""
    sync = session.sync_session
    sync.info.setdefault(_AFTER_COMMIT, []).append((job, key, delay_s, kwargs))
    if not event.contains(sync, "after_commit", _enqueue_committed):
        event.listen(sync, "after_commit", _enqueue_committed)
        event.listen(sync, "after_transaction_end", _drop_uncommitted)


async def flush_enqueues() -> None:
    """Await every after-commit enqueue in flight (the tests' determinism seam)."""
    while _enqueuing:
        await asyncio.gather(*tuple(_enqueuing), return_exceptions=True)


def _enqueue_committed(session: Session) -> None:
    """On the outermost commit, start every enqueue the transaction asked for, each in an empty
    context: a commit inside a job's body (a routine's fire writing its inbox item) would otherwise
    hand the job around it to the enqueue, which would start the push as that job's child, journaled
    out of order and unreplayable on recovery. Detached, it is a job of its own - `durable.start`'s
    move inside an effect."""
    if session.in_nested_transaction():
        return
    for job, key, delay_s, kwargs in session.info.pop(_AFTER_COMMIT, []):
        task = asyncio.get_running_loop().create_task(
            _enqueue(job, key, delay_s, kwargs), context=contextvars.Context()
        )
        _enqueuing.add(task)
        task.add_done_callback(_enqueuing.discard)


def _drop_uncommitted(session: Session, transaction: SessionTransaction) -> None:
    """When the outermost transaction ends without a commit, its jobs go with it."""
    if transaction.parent is None:
        session.info.pop(_AFTER_COMMIT, None)


async def _enqueue(job: str, key: str, delay_s: float, kwargs: dict[str, Any]) -> None:
    try:
        await get_dispatcher().enqueue(job, key, delay_s=delay_s, **kwargs)
    except Exception:
        logfire.exception("after-commit enqueue of {job} {key} failed; the job is lost", job=job, key=key)
