# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker - the process that owns all machine work.

Boot order matters, and is not arbitrary:

1. **Logfire first**, so everything after it is traced - including the failures. Then load this
   worker's frame driver: a driver whose package is missing refuses the boot, naming what is
   missing, before any job could fail on it. In json log format (ADR-101) stdout carries
   log records only, the durable runtime's included.
2. **Import `jobs.all`**, which is what registers the workflows, queues and schedules with DBOS.
   An unimported job is an unregistered job, and an unregistered job is one DBOS will refuse to
   recover after a crash.
3. **Wire the Dispatcher** - in *this* process too, not only in the API, because a job
   can enqueue another job.
4. **Upsert one `kind=agent` Actor per registry entry**, so machine attribution is uniform
   with human attribution from day one. An agent that writes an event is an Actor, exactly like a
   person.
5. **Reconcile the frames**: destroy only what Postgres disowns - never every frame,
   because a live peer's Machines are not ours to reap.
6. `durable.launch()` - recover whatever was in flight when we last died, then start consuming.
"""

from __future__ import annotations

import asyncio

import logfire

from prismoid import stdout_log
from prismoid.ai.gateway.providers import warm_sdks
from prismoid.conf import settings
from prismoid.db.engine import dispose_engine, reset_engine, set_application_name
from prismoid.db.models.actor import Actor
from prismoid.db.session import session_scope
from prismoid.observability import SCRUB_PATTERNS, UrlCredentialSpanProcessor


def configure_observability() -> None:
    json_logs = stdout_log.install(f"{settings.observability.service_name}-worker")
    logfire.configure(
        service_name=f"{settings.observability.service_name}-worker",
        environment=settings.environment,
        token=settings.observability.logfire_token.get_secret_value() or None,
        send_to_logfire="if-token-present",
        scrubbing=logfire.ScrubbingOptions(
            # The SAME list api/app.py scrubs (`prismoid/observability.py`) - both processes
            # must agree on what a secret is, or the invariant holds in only one of the two
            # places it must.
            extra_patterns=SCRUB_PATTERNS,
        ),
        # The scrubber never touches URL attributes, and the worker's outbound calls carry a
        # Telegram bot token in the URL path: rewritten as each span starts.
        additional_span_processors=[UrlCredentialSpanProcessor(), *json_logs],
        console=False if json_logs else None,  # json logs (ADR-101): records only, no span on stdout
    )

    # Same process-wide instrumentations as the API (both halves must agree on what is observed),
    # plus the agent loop itself: pydantic-ai spans are the worker's half of the performance
    # dashboard - model calls, tool calls, retries - joining DBOS's spans (enable_otlp below in main()).
    logfire.instrument_system_metrics()
    logfire.instrument_asyncpg()
    logfire.instrument_httpx()
    logfire.instrument_pydantic_ai(include_content=False, include_binary_content=False)  # no prompts on spans


async def _sync_agent_actors() -> int:
    from prismoid.ai.agents.all import roster

    async with session_scope() as session:
        added = await Actor.sync_agents(session, [str(a.name) for a in roster()])
        await session.commit()

    # This loop is about to be closed by `asyncio.run`. An asyncpg pool belongs to the loop that
    # made it, so leaving one cached here hands DBOS's workflow loop a pool bound to a corpse -
    # and the symptom is `RuntimeError: Event loop is closed` three layers down, in a stack trace
    # that mentions neither loops nor boot. Dispose here; reset the cache in the caller.
    await dispose_engine()
    return added


def sync_agent_actors() -> int:
    """One Actor per agent in the registry. Idempotent - it runs on every boot."""
    added = asyncio.run(_sync_agent_actors())
    reset_engine()  # DBOS's loop will build its own pool, on its own loop
    return added


async def _boot_reconcile() -> int:
    from prismoid.ai.frames import close_clients
    from prismoid.operations import machines

    try:
        return len((await machines.reconcile_once()).destroy)
    finally:
        try:
            await close_clients()  # a driver's client channel belongs to this loop too
        except Exception:
            logfire.exception("closing a frame driver client failed; disposing the pool anyway")
        await dispose_engine()  # the pool belongs to this loop (see `_sync_agent_actors`)


def boot_reconcile() -> int:
    """The boot's reconcile, best-effort - the old reap's law: a failed pass costs the
    pass, never the boot (`machine_reconcile` runs again within the minute). Frames destroyed."""
    try:
        return asyncio.run(_boot_reconcile())
    except Exception:
        logfire.exception("boot reconcile failed; booting anyway (the minute's pass retries)")
        return 0
    finally:
        reset_engine()  # DBOS's loop will build its own pool, on its own loop


def main() -> None:
    configure_observability()

    # The driver this worker makes frames on must load: an opt-in driver whose package is missing
    # refuses the boot here, naming the fix, instead of failing every step as infra.
    from prismoid.ai.frames import require_driver

    require_driver()

    # Name our connections before the first one opens (the boot dance below builds and resets the
    # pool a few times; each rebuild reads this) - `prismoid-worker` in pg_stat_activity, so a step's
    # legitimately-long transaction is never mistaken for a leak.
    set_application_name("prismoid-worker")

    from prismoid.jobs import all as jobs
    from prismoid.jobs import durable
    from prismoid.operations.dispatch import set_dispatcher

    durable.init(
        name="prismoid",
        database_url=settings.worker.database_url,
        # Unique per instance when scaling workers past one; empty keeps DBOS's
        # single-instance default. Queue concurrency caps are global either way.
        executor_id=settings.worker.executor_id or None,
        # Pinned: recovery must ADOPT in-flight work across dev restarts - the
        # code-hash default orphaned every restart's workflows into queue-clogging ghosts.
        # Empty conf value = DBOS's hash (deliberate opt-out). Prod sets the release sha.
        version=settings.worker.app_version or None,
    )
    set_dispatcher(jobs.WorkerDispatcher())

    # The chat SDKs load NOW, not inside the first step: their lazy import cost the first talk
    # turn after a restart ~1.2 s before its model request.
    warm_sdks()

    added = sync_agent_actors()

    # Reconcile, never reap (ADR-063). The old reap-all assumed ONE worker and would
    # kill a live peer's frames under a rolling deploy; the reconciler destroys only frames whose
    # (machine, epoch) no row owns, throwaway frames whose step is gone, and pre-1b frames after an
    # hour's grace. It also runs every minute (`machine_reconcile`). Before `DBOS.launch()` nobody
    # can list the queued sleeps, so this pass judges no `sleeping` row stuck - a sleep DBOS is about
    # to recover is never preempted; the minute's pass can tell.
    destroyed = boot_reconcile()

    # Boot pulse before the first scheduled minute: without it, /healthz reads "never" for up to
    # 60 s and the UI honestly-but-wrongly says the machine is offline after a cold start. It runs
    # before `DBOS.launch()`, so it carries no queue vitals.
    from prismoid.jobs.heartbeat import pulse_now

    asyncio.run(pulse_now(vitals=False))
    reset_engine()  # same loop-isolation story as sync_agent_actors

    plain = stdout_log.log_format() != "json"  # in json the logfire lines below are the stdout lines
    if plain:
        print(
            f"worker booting: {len(jobs.JOBS)} jobs, {added} new agent actors, "
            f"{destroyed} frames destroyed by the reconciler",
            flush=True,
        )
    logfire.info(
        "worker booting: {jobs} jobs, {agents} new agent actors, {destroyed} frames destroyed by the reconciler",
        jobs=len(jobs.JOBS),
        agents=added,
        destroyed=destroyed,
    )

    if not settings.push.enabled():
        # Said once, here: push serves the cloud deployment, which sets the key; anywhere else it is off.
        print(
            "worker: push notifications are off: PRISMOID_PUSH_CREDENTIALS_JSON is unset (ADR-100)",
            flush=True,
        )
        logfire.info("worker: push notifications are off: PRISMOID_PUSH_CREDENTIALS_JSON is unset (ADR-100)")

    durable.launch()

    # **The ghost sweep, gated on the single-worker assumption.** Queued/pending
    # workflows whose application_version is not OURS are unrecoverable by construction - no
    # executor of that version will ever exist again - yet DBOS counts them against GLOBAL queue
    # concurrency: eight of them once froze the entire steps queue, and the freeze
    # was silent. Reaping them at boot is safe ONLY when no other-version work is a live peer -
    # true for a single worker, false during a ROLLING DEPLOY (the old version is still draining).
    # There is no peer-liveness check, so this honours `reap_version_ghosts`: rolling deploys set
    # it false and drain the old version before retiring its workers (deploy/DEPLOY.md).
    if settings.worker.reap_version_ghosts:
        ghosts = _sweep_version_ghosts()
        if ghosts:
            if plain:
                print(f"worker boot: swept {ghosts} version-orphaned workflow ghost(s)", flush=True)
            logfire.warn("boot swept {n} version-orphaned workflow ghost(s)", n=ghosts)

    # The batched NOTIFY hub: the worker is where Lane-2 deltas are emitted, at the
    # frequency that would otherwise convoy on Postgres's commit-time NOTIFY lock. One flusher
    # thread coalesces them; started here, stopped in the finally so a clean shutdown loses nothing
    # already queued.
    from prismoid.operations.notifier import notifier

    notifier.start()

    if plain:
        print(f"worker ready - jobs: {sorted(jobs.JOBS)}", flush=True)
    logfire.info("worker ready")

    # DBOS runs its consumers on background threads; this process exists to hold them open.
    stop = asyncio.Event()
    try:
        asyncio.run(stop.wait())
    except KeyboardInterrupt:
        if plain:
            print("worker stopping", flush=True)
        else:
            logfire.info("worker stopping")
    finally:
        notifier.stop()
        durable.shutdown()


def _sweep_version_ghosts() -> int:
    """Clear ENQUEUED/DELAYED/PENDING workflows from OTHER application versions.

    A PENDING ghost started on code that is gone: it is cancelled. An ENQUEUED one never started -
    the old API's turn, chat reply or bot draft that no worker of its version is left to take - so
    it MOVES here, and so does a DELAYED one (an inbox alert still waiting out its delay), which
    starts at once: deleted (a cancelled row would squat its deterministic id forever, and the
    sweeper's `task-{id}` re-enqueue would dedupe onto the tombstone) and enqueued again on THIS
    version under the same id with the same inputs (`dispatcher.requeue`). Only turns have a
    sweeper behind them; a chat reply deleted and never re-enqueued was a line that got no answer.

    Best-effort by the boot reconcile's law: a failed sweep costs the sweep, never the boot -
    but a queue frozen by ghosts costs every task, so the trade only runs one way.
    """
    from prismoid.jobs import durable
    from prismoid.jobs.dispatcher import requeue

    try:
        ours = durable.version()
        swept = 0
        for run in durable.runs_sync(states=("queued", "delayed", "running"), limit=500):
            if run.version and run.version != ours:
                if run.state != "running":
                    durable.forget_sync(run.key)
                    requeue(run)
                else:
                    durable.cancel_sync(run.key)
                swept += 1
        return swept
    except Exception:
        logfire.exception("version-ghost sweep failed; booting anyway (queues may be clogged)")
        return 0
