# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""SCREEN 3 - every durable job in the product. Read it top to bottom and you know the machine.

A **job** is the machine process; a **Task** is what the user asked for. Importing this
module is what registers the workflows and the schedules with DBOS, so both processes import it
exactly once at boot - the API because it *enqueues*, the worker because it *executes*.
"""

from __future__ import annotations

from prismoid.jobs.bot_studio import draft_bot, test_bot
from prismoid.jobs.channels import sweep_channels
from prismoid.jobs.chat import reply_chat
from prismoid.jobs.dispatcher import ClientDispatcher, WorkerDispatcher
from prismoid.jobs.effects import execute_step as run_step
from prismoid.jobs.figures import pregenerate_figures
from prismoid.jobs.gate_sweeper import sweep_gates
from prismoid.jobs.heartbeat import worker_heartbeat
from prismoid.jobs.machines import (
    machine_boot,
    machine_browser_stop,
    machine_egress,
    machine_expire,
    machine_forget,
    machine_lease_end,
    machine_orphans,
    machine_reconcile,
    machine_release,
    machine_reset,
    machine_seed,
    machine_sleep,
    machine_sweep,
)
from prismoid.jobs.push import push_inbox, push_read
from prismoid.jobs.queues import (
    machine_queue,
    push_queue,
    routine_queue,
    step_queue,
    task_queue,
    verify_queue,
)
from prismoid.jobs.routines import fire_routine, fire_routine_event, sweep_routine_events, sweep_routines
from prismoid.jobs.run_task import run_task
from prismoid.jobs.session_sweeper import sweep_password_resets, sweep_sessions
from prismoid.jobs.sweeper import sweep_orphans
from prismoid.jobs.verify import verify_artifact

# The roster. One line per job.
JOBS = {
    "run_task": run_task,  # the orchestrator: one per Task, id `task-{id}` / `task-{id}-t{n}`
    "run_step": run_step,  # one per plan step, on `step_queue` - this is what fans out
    "verify_artifact": verify_artifact,  # the receipt, after the answer: `verify-{artifact_id}`
    "pregenerate_figures": pregenerate_figures,  # the outline's images, in parallel: `figures-{task}-t{n}`
    "sweep_routines": sweep_routines,  # every minute: offer due routine fires (read-only, dumb)
    "sweep_routine_events": sweep_routine_events,  # every minute: the record's lagged window vs live listeners (W4)
    "fire_routine": fire_routine,  # one occurrence → the policy gate → create_task: `routine-{id}-{occurrence}`
    "fire_routine_event": fire_routine_event,  # one receipt → the same gate, + fenced snapshot + chain depth
    "sweep_orphans": sweep_orphans,  # every minute: re-enqueue tasks nobody started
    "sweep_channels": sweep_channels,  # every minute: telegram poll channels drain into conversations
    "reply_chat": reply_chat,  # one bot reply in one conversation: `chat-{conversation}-{seq}`
    "sweep_gates": sweep_gates,  # every minute: resolve expired gates by their on_timeout
    "draft_bot": draft_bot,  # `bot_lesson` from an idea: one author sentence → a first DRAFT: `botdraft-{row}`
    "test_bot": test_bot,  # the selection eval, from the web: `bottest-{row}`
    "sweep_sessions": sweep_sessions,  # hourly: hard-delete auth sessions dead past their grace
    "sweep_password_resets": sweep_password_resets,  # hourly: hard-delete reset codes long past expiry
    "worker_heartbeat": worker_heartbeat,  # every minute: the worker's pulse - /healthz reads its age
    "machine_sleep": machine_sleep,  # a Machine's checkpoint is stored and its frame destroyed: `machine-sleep-{id}-{epoch}~{claim|tick}`
    "machine_reset": machine_reset,  # a reset Machine's old frame and objects are cleaned up: `machine-reset-{id}-{epoch}`
    "machine_expire": machine_expire,  # retention: a sleeping Machine's objects are deleted: `machine-expire-{id}-{epoch}~{tick}`
    "machine_release": machine_release,  # past the asleep cap: `machine-release-{id}-{epoch}~{tick}`
    "machine_forget": machine_forget,  # a deleted task's Machine: its frame and its objects: `machine-forget-{id}`
    "machine_boot": machine_boot,  # a task's first Machine, prewarmed on a committed plan: `machine-boot-{task}`
    "machine_seed": machine_seed,  # a fork's Machine is filled from its parent's /work: `machine-seed-{child_task}`
    "machine_lease_end": machine_lease_end,  # a control lease past its clock - or any, computer use off - ends: `machine-lease-{id}-{since}~{tick}`
    "machine_browser_stop": machine_browser_stop,  # computer use off: a warm browser Machine's Chromium stops: `machine-browser-stop-{id}-{epoch}`
    "machine_egress": machine_egress,  # a control lease moved an idle Machine's egress: its frame is told: `machine-egress-{id}-{version}`
    "machine_sweep": machine_sweep,  # every 30 s: dispatch due sleeps, expiries, lease endings and kill-switch browser stops (read-only, dumb)
    "machine_reconcile": machine_reconcile,  # boot + every 60 s: destroy what Postgres disowns, fix rows
    "machine_orphans": machine_orphans,  # daily: delete objects under machines/ that no row references
    "push_inbox": push_inbox,  # an inbox item's alert to its recipient's phones: `push-inbox-{item}`
    "push_read": push_read,  # a read's silent badge update: `push-read-{item}` / `push-read-all-{actor}-{ws}-{at}`
}

__all__ = [
    "JOBS",
    "ClientDispatcher",
    "WorkerDispatcher",
    "draft_bot",
    "fire_routine",
    "fire_routine_event",
    "machine_boot",
    "machine_browser_stop",
    "machine_egress",
    "machine_expire",
    "machine_forget",
    "machine_lease_end",
    "machine_orphans",
    "machine_queue",
    "machine_reconcile",
    "machine_release",
    "machine_reset",
    "machine_seed",
    "machine_sleep",
    "machine_sweep",
    "pregenerate_figures",
    "push_inbox",
    "push_queue",
    "push_read",
    "reply_chat",
    "routine_queue",
    "run_step",
    "run_task",
    "step_queue",
    "sweep_channels",
    "sweep_gates",
    "sweep_orphans",
    "sweep_password_resets",
    "sweep_routine_events",
    "sweep_routines",
    "sweep_sessions",
    "task_queue",
    "test_bot",
    "verify_artifact",
    "verify_queue",
    "worker_heartbeat",
]
