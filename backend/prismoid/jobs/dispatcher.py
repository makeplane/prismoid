# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The `Dispatcher` implementations (seam 1).

`operations` depends on the Protocol in `operations/dispatch.py`; these implement it; `runner` wires
the right one at boot. That inversion is what lets `jobs` import `operations` (job bodies compose
it) without `operations` importing `jobs` back.

**Two implementations, because the two processes have different jobs - and that is Rule 5.**

* **`ClientDispatcher` (the API)** uses `durable.Client`, which **enqueues and nothing else**. The API
  must be able to *start* work; it must not be able to *do* work.
* **`WorkerDispatcher` (the worker)** has the full DBOS runtime: it enqueues, and it consumes.

Getting this wrong was not a style problem, and it did not look like a bug. The API originally
called `DBOS.launch()` - which quietly starts the queue consumers - so **the API process was
executing agent workflows**: spawning Docker sandboxes, calling models, and spending money inside
the process that serves HTTP. Every request would have been competing with a language model for the
event loop, and restarting the API would have killed tasks mid-flight.

Rule 5 says *"the worker owns all machine work."* That turns out not to be a guideline about where
to put code. It is a claim about **which process may launch a runtime** - and the only way to keep
it true is to hand the API a client that *cannot*.

**Deterministic ids are the other half.** `workflow_id = "task-{id}"` makes DBOS dedupe: a
double-enqueue is a no-op rather than a second run. That is what lets the sweeper be careless - it
re-enqueues anything that looks stalled without ever risking running it twice.
"""

from __future__ import annotations

from typing import Any

import logfire

from prismoid.conf import settings
from prismoid.jobs import durable
from prismoid.jobs.bot_studio import draft_bot, test_bot
from prismoid.jobs.chat import reply_chat
from prismoid.jobs.figures import pregenerate_figures
from prismoid.jobs.machines import (
    machine_boot,
    machine_browser_stop,
    machine_egress,
    machine_expire,
    machine_forget,
    machine_lease_end,
    machine_release,
    machine_reset,
    machine_seed,
    machine_sleep,
)
from prismoid.jobs.push import push_inbox, push_read
from prismoid.jobs.queues import (
    BOT_QUEUE,
    CHAT_QUEUE,
    MACHINE_QUEUE,
    PUSH_QUEUE,
    TASK_QUEUE,
    VERIFY_QUEUE,
    bot_queue,
    chat_queue,
    machine_queue,
    push_queue,
    task_queue,
    verify_queue,
)
from prismoid.jobs.run_task import run_task
from prismoid.jobs.verify import verify_artifact

# name → (function, queue, queue name) - every job `operations` may start, and the lane it
# runs in. The queue rides the registry because the two dispatchers must agree on it: the
# worker enqueues onto the Queue object, the API enqueues by NAME through `durable.Client`. Typed
# loosely on purpose: the registry spans jobs with different signatures, and the kwargs are
# checked where they matter - at the workflow function itself.
_JOBS: dict[str, tuple[Any, Any, str]] = {
    "run_task": (run_task, task_queue, TASK_QUEUE),
    "verify_artifact": (verify_artifact, verify_queue, VERIFY_QUEUE),
    "pregenerate_figures": (pregenerate_figures, verify_queue, VERIFY_QUEUE),
    "draft_bot": (draft_bot, bot_queue, BOT_QUEUE),
    "test_bot": (test_bot, bot_queue, BOT_QUEUE),
    "reply_chat": (reply_chat, chat_queue, CHAT_QUEUE),
    "machine_sleep": (machine_sleep, machine_queue, MACHINE_QUEUE),
    "machine_reset": (machine_reset, machine_queue, MACHINE_QUEUE),
    "machine_expire": (machine_expire, machine_queue, MACHINE_QUEUE),
    "machine_release": (machine_release, machine_queue, MACHINE_QUEUE),
    "machine_forget": (machine_forget, machine_queue, MACHINE_QUEUE),
    "machine_seed": (machine_seed, machine_queue, MACHINE_QUEUE),
    "machine_boot": (machine_boot, machine_queue, MACHINE_QUEUE),
    "machine_lease_end": (machine_lease_end, machine_queue, MACHINE_QUEUE),
    "machine_browser_stop": (machine_browser_stop, machine_queue, MACHINE_QUEUE),
    "machine_egress": (machine_egress, machine_queue, MACHINE_QUEUE),
    "push_inbox": (push_inbox, push_queue, PUSH_QUEUE),
    "push_read": (push_read, push_queue, PUSH_QUEUE),
}


def requeue(workflow: durable.Run) -> bool:
    """Enqueue a never-started workflow again on THIS worker's version, under its own id, with its
    own inputs - the boot sweep's way of moving another version's ENQUEUED ghost here
    once it has deleted the row. Nothing of it ran, so nothing runs twice. Both dispatchers
    enqueue a registered job by keyword, so that is the only shape moved; a job this worker does
    not know (retired, renamed) stays deleted. Sync: the boot runs it before the worker's loop."""
    entry = _JOBS.get(workflow.name)
    inputs = workflow.kwargs
    if entry is None or inputs is None or workflow.args:
        return False
    fn, queue, _ = entry
    durable.start_sync(queue, workflow.key, fn, **inputs)
    return True


class WorkerDispatcher:
    """The worker's. It holds the whole runtime, so it enqueues by calling the function."""

    async def enqueue(self, job: str, key: str, *, delay_s: float = 0.0, **kwargs: Any) -> None:
        entry = _JOBS.get(job)
        if entry is None:
            raise KeyError(f"no job named {job!r}; known: {sorted(_JOBS)}")
        fn, queue, _ = entry
        # `task-{id}` / `verify-{artifact}` - deterministic, deduped. Async: the sync
        # enqueue is a blocking system-DB write, and every caller of this method is on the loop.
        await durable.start_later(queue, key, delay_s, fn, **kwargs)
        logfire.info("enqueued {job} as {key}", job=job, key=key)

    async def send(self, key: str, payload: Any, topic: str, *, missing_ok: bool = False) -> None:
        """Wake a job parked on `durable.wait` - a gate resolution.

        **The async send, not the sync one, and this is not interchangeable.** The worker's callers run
        inside an event loop (the API's request handler, the sweep's async workflow), and DBOS's
        sync `send` refuses that outright - *"send() was called while an event loop is running"*. The
        gate would resolve, the message would raise, and the task would sit parked on a `recv` that
        nobody ever posted to. The gate TTL sweep once failed in exactly this way, silently: the
        gate went `decline`, and the workflow never woke.

        **A wake with no addressee is a no-op only where the caller SAYS so** (`missing_ok` -
        cancel's pokes at a HELD task whose workflow was never enqueued). Every other failure
        PROPAGATES: the previous broad except swallowed real
        transport errors too, so a resolved gate's wake could vanish while the request
        reported success and the task re-parked for 24 h on an answer already given. The gate-row
        re-reads on recv timeout are the durable net; a loud failure
        here is what tells a human the poke lane is sick.
        """
        try:
            await durable.signal(key, payload, topic)
        except durable.NoSuchJob as exc:
            if not missing_ok:
                raise
            logfire.warn(
                "wake for {key} topic {topic} had no addressee: {err}",
                key=key,
                topic=topic,
                err=str(exc),
            )


class ClientDispatcher:
    """The API's. `durable.Client` enqueues and sends. It **cannot consume**, and that is the point.

    It addresses the workflow by *name*, not by reference - the honest shape of the thing: this
    process is not the one that will run it, and does not need to be able to.
    """

    def __init__(self) -> None:
        self._client = durable.Client(settings.worker.database_url)

    async def enqueue(self, job: str, key: str, *, delay_s: float = 0.0, **kwargs: Any) -> None:
        entry = _JOBS.get(job)
        if entry is None:
            raise KeyError(f"no job named {job!r}; known: {sorted(_JOBS)}")
        _, _, queue_name = entry
        # A turn runs only on a worker of the version that enqueued it: DBOS hands
        # an UNSTAMPED row to a worker of any release, and one that predates the memory fence
        # runs a Project turn unfenced. Worker-side enqueues are stamped
        # by the runtime. Empty is the worker's opt-out to DBOS's code hash, which this process
        # cannot know - so it stamps nothing rather than a value no worker would ever match.
        # Async: the sync client call was a blocking SQLAlchemy round-trip ON the
        # API's event loop - every task submit stalled every other in-flight request for its write.
        # The key is deterministic - the engine dedupes on it.
        version = settings.worker.app_version or None
        await self._client.enqueue(queue_name, job, key, version, delay_s, **kwargs)
        logfire.info("enqueued {job} as {key} (client)", job=job, key=key)

    async def send(self, key: str, payload: Any, topic: str, *, missing_ok: bool = False) -> None:
        # The client is a standalone connection, not bound to a running workflow, so its send is
        # safe from an event loop - but the signature is `async` to match `WorkerDispatcher`, so a
        # caller `await`s the same shape regardless of which dispatcher is installed.
        # Same missing_ok-only tolerance as the worker's send (its docstring carries both
        # found-live stories): client and worker sends share `_send_bulk_txn`, which raises the
        # same typed no-addressee error on the notifications FK miss.
        try:
            self._client.signal(key, payload, topic)
        except durable.NoSuchJob as exc:
            if not missing_ok:
                raise
            logfire.warn(
                "wake for {key} topic {topic} had no addressee: {err}",
                key=key,
                topic=topic,
                err=str(exc),
            )
