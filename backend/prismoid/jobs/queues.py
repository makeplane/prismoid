# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The queues. One per kind of work, so one kind cannot starve another.

`step_queue`'s concurrency is `PRISMOID_WORKER_STEP_CONCURRENCY` - the frontier fans out
onto it, and it is the one number that decides how many sandboxes run at once.
"""

from __future__ import annotations

from prismoid.conf import settings
from prismoid.jobs import durable

# The names are constants because the API enqueues **by name** - it holds a `durable.Client`, not the
# runtime, so it cannot reference the `Queue` object the worker consumes from (see dispatcher.py).
TASK_QUEUE = "tasks"
STEP_QUEUE = "steps"
VERIFY_QUEUE = "verification"

# Tasks: the long-lived orchestrator. Cheap - it spends most of its life parked on a gate or
# joining a frontier, so its concurrency is generous. The poll cadence is conf:
# DBOS's worker sleeps BEFORE it polls, so at the 1.0 s default a fresh enqueue waits ~0.5 s
# mean / ~1.05 s worst on this hop alone - the audit's pure-granularity bucket.
task_queue = durable.queue(TASK_QUEUE, 64, poll_s=settings.worker.queue_poll_s)

# Steps: the expensive lane. Each one may hold a frame and a model. Same cadence: this is
# queue hop 2 of every answer.
step_queue = durable.queue(STEP_QUEUE, settings.worker.step_concurrency, poll_s=settings.worker.queue_poll_s)

# Verification: the receipt lane - post-delivery verify frames + judge calls. Its own
# queue, deliberately modest, so signing receipts can never starve the steps producing answers.
verify_queue = durable.queue(VERIFY_QUEUE, 4)

# Routine fires: cheap DB work that ends in `create_task` - but a flood of due
# routines must never starve tasks or steps, and a poison fire must dead-letter alone. Its own
# modest lane is that isolation.
ROUTINE_QUEUE = "routines"
routine_queue = durable.queue(ROUTINE_QUEUE, 8)

# Bot authoring: `bot_lesson` drafts and the selection eval, moved off the CLI so the web
# can author end to end. A couple at a time is plenty - these are an author waiting on one
# result, and they must never crowd the step lane doing real work.
BOT_QUEUE = "bots"
bot_queue = durable.queue(BOT_QUEUE, 2)

# Conversation replies: one frameless model call each, a person waiting on the
# other end. Its own lane so a step flood never delays a chat, and a chat flood never
# holds a sandbox slot; per-conversation order is the row claim, not the queue.
CHAT_QUEUE = "chat"
chat_queue = durable.queue(CHAT_QUEUE, 8, poll_s=settings.worker.queue_poll_s)

# The Machine: sleeps, resets, seeds and expiries - each one row's slow work (an archive,
# an upload, a destroy). Its own lane so a sleep storm never holds a step slot, and a step flood
# never leaves warm Machines un-slept past the workspace's cap.
MACHINE_QUEUE = "machines"
machine_queue = durable.queue(MACHINE_QUEUE, 8)  # sleeps, resets, seeds, expiries

# Push notifications: one FCM round trip per job, a person's phone on the other end. Its own lane so a
# notification storm never waits behind a task or a sleep, and never holds their slots.
PUSH_QUEUE = "push"
push_queue = durable.queue(PUSH_QUEUE, 16)
