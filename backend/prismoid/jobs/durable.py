# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The durable runtime - the ONE file that imports `dbos` (ADR-069).

Every job, effect, schedule, queue, park and listing in the product goes through the verbs below,
and nothing else in `prismoid/` names the engine (`tools/audit.sh` holds the line). The point is the
planned port: DBOS stays the engine today, DART replaces it later, and the
swap is this file plus its tests - never forty files and every job body.

The verbs are the engine-neutral shape of what Prismoid needs, each one a DBOS call today:

    job(name)              a durable process with a deterministic id    @DBOS.workflow
    effect(...)            a journaled unit of I/O inside a job         @DBOS.step
    schedule(cron)         a job a clock starts                         @DBOS.scheduled
    queue(name, ...)       a lane with a global concurrency cap         dbos.Queue
    start / join / cancel  a child job: enqueue it, await it, stop it   SetWorkflowID + enqueue
    start_later            start, but no worker takes it for a while    SetEnqueueOptions(delay)
    wait(topic, timeout)   park until signalled (or the clock)          DBOS.recv
    sleep(seconds)         a durable timer                              DBOS.sleep
    signal(key, topic, x)  wake a parked job                            DBOS.send
    runs(...)              the queued / running / delayed jobs          DBOS.list_workflows
    Client                 the API's handle: enqueue + signal, never run DBOSClient
    init/launch/shutdown   the worker's runtime                         DBOS(...), launch, destroy

**Thin on purpose.** Each verb calls the engine exactly as the job bodies did before this file
existed, so a workflow journalled before the seam recovers after it: no step, workflow or recorded
call changed its name. Behaviour belongs to the jobs; this file only translates.

Where DBOS semantics leak through the names (a `wait` that is also a timer, a `job` that never
re-runs a finished id, `effect` retries), the docstrings say so - those are the contract a DART
implementation must keep, or the place its ADR must say it does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
from collections.abc import Callable, Collection, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from dbos import (
    DBOS,
    DBOSClient,
    DBOSConfig,
    EnqueueOptions,
    Queue,
    SetEnqueueOptions,
    SetWorkflowID,
    WorkflowHandleAsync,
    WorkflowStatus,
)
from dbos import error as _error

# Private paths, knowingly (drift → ADR-006): the running version and the schedule iterator are
# not exported, and both are contracts a test pins.
from dbos._croniter import croniter  # pyright: ignore[reportMissingTypeStubs]
from dbos._utils import GlobalParams

Running = WorkflowHandleAsync  # a started child job, awaited with `join`

# A join's poll interval: each poll is a thread-pool call, and the 0.05 s queue cadence churned threads.
JOIN_POLL_S = 0.1

# A signal sent to a key no job was ever started under (`Dispatcher.send(missing_ok=True)`).
NoSuchJob = _error.DBOSNonExistentWorkflowError
# A replay that met a different call than its journal recorded (the pause-park tests' model of it).
ReplayMismatch = _error.DBOSUnexpectedStepError

RunState = Literal["queued", "running", "delayed"]
_STATUS: dict[RunState, str] = {"queued": "ENQUEUED", "running": "PENDING", "delayed": "DELAYED"}
_STATE: dict[str, RunState] = {status: state for state, status in _STATUS.items()}
ACTIVE: tuple[RunState, ...] = ("queued", "running", "delayed")


# ── registration ─────────────────────────────────────────────────────────────────────────────


def job[**P, R](name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """A durable job registered under `name` - the name the API enqueues by and recovery resumes
    by, so it never changes. A job started under an id that already finished never runs again
    (that is what makes `task-{id}` dedupe, and why a follow-up turn gets its own id)."""
    return DBOS.workflow(name=name)


def effect[**P, R](
    *,
    attempts: int = 1,
    interval_s: float = 1.0,
    backoff: float = 2.0,
    retry_if: Callable[[BaseException], bool] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """A journaled effect: its output is recorded, and a replayed job gets the record instead of
    running it twice. One attempt by default - the product's retries are the targeted ladder, never
    the engine's. `attempts > 1` retries with `interval_s` growing by `backoff`, and `retry_if`
    narrows which errors retry."""
    if attempts <= 1:
        return DBOS.step()
    return DBOS.step(
        retries_allowed=True,
        max_attempts=attempts,
        interval_seconds=interval_s,
        backoff_rate=backoff,
        should_retry=retry_if,
    )


type _Tick = (
    Callable[[dt.datetime, dt.datetime], None]
    | Callable[[dt.datetime, dt.datetime], Coroutine[Any, Any, None]]
)


def schedule[J: _Tick](cron: str) -> Callable[[J], J]:
    """A job the clock starts, called with `(scheduled, actual)`. Five fields, or six with SECONDS
    FIRST. A fire missed while no worker ran is never backfilled: the next one is the next tick
    (`next_fire` is the iterator, so a test can pin a schedule's meaning)."""
    return DBOS.scheduled(cron)  # pyright: ignore[reportReturnType]


def next_fire(cron: str, after: dt.datetime) -> dt.datetime:
    """The first fire of `cron` after `after`, as the scheduler walks it."""
    fires = croniter(cron, after, second_at_beginning=True)  # pyright: ignore[reportUnknownVariableType]
    return fires.get_next(dt.datetime)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def queue(name: str, concurrency: int, *, poll_s: float | None = None) -> Queue:
    """A lane: at most `concurrency` of its jobs run at once ACROSS every worker. `poll_s` is how
    often a worker looks for new work (the engine's default, 1 s, when None)."""
    if poll_s is None:
        return Queue(name, concurrency=concurrency)
    return Queue(name, concurrency=concurrency, polling_interval_sec=poll_s)


# ── inside a job ─────────────────────────────────────────────────────────────────────────────


async def wait(topic: str, timeout_s: float) -> Any:
    """Park this job until a `signal` on `topic` arrives, or `timeout_s` passes (then None). Costs
    no thread while it waits - but on DBOS it HOLDS ITS QUEUE SLOT (the defect DART's sleep fixes).
    The mailbox is per job, not per topic-and-park: a signal sent before the park is waiting for it."""
    return await DBOS.recv_async(topic=topic, timeout_seconds=timeout_s)


async def sleep(seconds: float) -> None:
    """A durable timer: a restart resumes the wait, never skips it. No time journals nothing."""
    await DBOS.sleep_async(seconds)


async def start[**P, R](
    lane: Queue, key: str, fn: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
) -> Running[R]:
    """Enqueue `fn` on `lane` under the deterministic id `key`. A second start of a key is the
    first one (dedupe), which is what lets a sweeper be careless. The seam's own parameters are
    positional-only, so a job's argument can be called `key` or `lane` and still reach the job.

    From a job's body the new job is its child, journaled in the parent. From inside an effect it
    is started as its own root, in a context with no job around it: DBOS starts a workflow only from
    a workflow body or from outside any workflow, and asserts inside a step. The key keeps that start
    idempotent - an effect that runs again starts the same job, which is the first one."""
    return await start_later(lane, key, 0.0, fn, *args, **kwargs)


async def start_later[**P, R](
    lane: Queue,
    key: str,
    delay_s: float,
    fn: Callable[P, Coroutine[Any, Any, R]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> Running[R]:
    """`start`, but no worker takes the job for `delay_s` seconds (0 or less starts it now). It waits
    `delayed`, holding no slot of its lane, then joins the lane's queue like any other. The delay is
    a floor, not a schedule: the engine moves a due job on within about a second, and the lane's
    poll adds its own."""
    if DBOS.step_id is not None:
        return await asyncio.create_task(
            _enqueue(lane, key, delay_s, fn, *args, **kwargs), context=contextvars.Context()
        )
    return await _enqueue(lane, key, delay_s, fn, *args, **kwargs)


async def _enqueue[**P, R](
    lane: Queue,
    key: str,
    delay_s: float,
    fn: Callable[P, Coroutine[Any, Any, R]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> Running[R]:
    later = SetEnqueueOptions(delay_seconds=delay_s) if delay_s > 0 else contextlib.nullcontext()
    with SetWorkflowID(key), later:
        return await lane.enqueue_async(fn, *args, **kwargs)


def start_sync[**P, R](
    lane: Queue, key: str, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> None:
    """`start` for the worker's boot, which runs before its event loop exists."""
    with SetWorkflowID(key):
        lane.enqueue(fn, *args, **kwargs)


async def join[R](handle: Running[R]) -> R:
    """Await a started job's result; its failure re-raises here. Polls every `JOIN_POLL_S` - the
    engine's 1 s default sat between "the answer exists" and "the user is told"."""
    return await handle.get_result(polling_interval_sec=JOIN_POLL_S)


async def cancel(key: str) -> None:
    """Stop the job under `key` at its next effect boundary."""
    await DBOS.cancel_workflow_async(key)


# ── from outside a job ───────────────────────────────────────────────────────────────────────


async def signal(key: str, payload: Any, topic: str) -> None:
    """Wake the job under `key` parked on `topic` (or leave the payload for its next `wait`).
    Raises `NoSuchJob` when nothing was ever started under `key`. Async because the engine's sync
    send refuses to run inside an event loop, and every caller is in one."""
    await DBOS.send_async(key, payload, topic)


@dataclass(frozen=True, slots=True)
class Run:
    """One job the engine holds, as the reconciler, the heartbeat and the boot sweep read it.
    `kwargs` is None unless the listing asked for inputs."""

    key: str
    name: str
    state: RunState
    queue: str | None
    version: str | None
    created_at_ms: int | None
    args: tuple[Any, ...] | None = None
    kwargs: dict[str, Any] | None = None


def _run(status: WorkflowStatus) -> Run:
    # The engine hands back the raw string when it cannot read a row's input (an old release's
    # format): that row's input is unknown, never a reason to drop the whole listing.
    inputs: Any = status.input
    if not (isinstance(inputs, dict) and "args" in inputs and "kwargs" in inputs):
        inputs = None
    return Run(
        key=status.workflow_id,
        name=status.name,
        state=_STATE[str(status.status)],  # only the states asked for come back
        queue=status.queue_name,
        version=status.app_version,
        created_at_ms=status.created_at,
        args=tuple(inputs["args"]) if inputs is not None else None,
        kwargs=dict(inputs["kwargs"]) if inputs is not None else None,
    )


def _listing(
    name: str | None, states: Collection[RunState], load_input: bool, limit: int | None
) -> dict[str, Any]:
    return {
        "name": name,
        "status": [_STATUS[s] for s in states],
        "load_input": load_input,
        "load_output": False,
        "limit": limit,
    }


async def runs(
    name: str | None = None,
    *,
    states: Collection[RunState] = ACTIVE,
    load_input: bool = False,
    limit: int | None = None,
) -> list[Run]:
    """The jobs in `states` (every live one by default), of one name or all. Raises when the
    engine cannot say (before `launch`, or a failing system database) - the callers read that as
    "unknown", never "none". Inside a job the listing is journaled like an effect."""
    return [_run(s) for s in await DBOS.list_workflows_async(**_listing(name, states, load_input, limit))]


def runs_sync(
    *, states: Collection[RunState] = ACTIVE, load_input: bool = True, limit: int | None = None
) -> list[Run]:
    """`runs` for the boot sweep, before the worker's loop."""
    return [_run(s) for s in DBOS.list_workflows(**_listing(None, states, load_input, limit))]


def forget_sync(key: str) -> None:
    """Delete the job under `key` and its record, freeing its id for a fresh start."""
    DBOS.delete_workflow(key)


def cancel_sync(key: str) -> None:
    """`cancel` for the boot sweep."""
    DBOS.cancel_workflow(key)


def version() -> str:
    """The application version this worker runs and stamps on what it enqueues."""
    return GlobalParams.app_version


class Client:
    """The API's handle on the engine: it ENQUEUES BY NAME and signals, and it cannot run a job
    (the API process must never execute agent work; `jobs/dispatcher.py` tells the story)."""

    def __init__(self, database_url: str) -> None:
        self._client = DBOSClient(database_url=database_url)

    async def enqueue(
        self, lane: str, job: str, key: str, version: str | None, delay_s: float = 0.0, /, **kwargs: Any
    ) -> None:
        """Start `job` on `lane` under `key`. `version` pins which workers may take it; None lets
        any worker of any release take it. `delay_s` holds it back as `start_later` does.
        Positional-only: `kwargs` are the job's own arguments, and two jobs take one called
        `version` (`test_bot`, `machine_egress`)."""
        options: EnqueueOptions = {"queue_name": lane, "workflow_name": job, "workflow_id": key}
        if version:
            options["app_version"] = version
        if delay_s > 0:
            options["delay_seconds"] = delay_s
        await self._client.enqueue_async(options, **kwargs)

    def signal(self, key: str, payload: Any, topic: str) -> None:
        """`signal` from a standalone connection, safe on an event loop. Raises `NoSuchJob` as
        the worker's does."""
        self._client.send(key, payload, topic)


# ── the worker's runtime ─────────────────────────────────────────────────────────────────────


def init(*, name: str, database_url: str, executor_id: str | None, version: str | None) -> None:
    """Build the runtime. Every job module must be imported first: an unregistered job is one
    recovery refuses. `executor_id` must be unique per worker past one; `version` None
    falls back to the engine's code hash (prod pins the release sha, see ADR-027)."""
    DBOS(config=_config(name=name, database_url=database_url, executor_id=executor_id, version=version))


def _config(*, name: str, database_url: str, executor_id: str | None, version: str | None) -> DBOSConfig:
    config = DBOSConfig(
        name=name,
        database_url=database_url,
        executor_id=executor_id,
        application_version=version,
        enable_otlp=True,  # one instrumentation story: the engine's spans join Logfire's
        # The engine's admin server binds 0.0.0.0:3001 with no auth (list, cancel, resume, fork
        # workflows): a door onto the network on a Mac and to every pod and VPC peer in a cluster.
        # Nothing uses it: the worker's probes read system_beats, and no Service exposes the port.
        run_admin_server=False,
        max_executor_threads=64,  # top-level key: the engine's own pool is unbounded and churn exhausts it
    )
    return config


def launch() -> None:
    """Recover what this executor had in flight, then start consuming every queue and schedule."""
    DBOS.launch()


def shutdown() -> None:
    DBOS.destroy()
