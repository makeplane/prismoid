# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The sweeper - ghost-freedom for the gap between COMMIT and enqueue.

Operations enqueue **after** the commit, which is right: a job enqueued *inside* the
transaction can be picked up before that transaction commits, and the worker then reads a task
that does not exist yet. That trade leaves one small ghost of its own - a crash between the commit
and the enqueue leaves a task nobody started - and this is what kills it.

It is safe to be dumb about it precisely because job ids are deterministic (`task-{id}`): DBOS
dedupes on the id, so re-enqueueing a task that *is* already running is a no-op, not a second run.
Ghost-freedom by construction, not by locking.
"""

from __future__ import annotations

import datetime as dt

import logfire

from prismoid.db.base import utcnow
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.jobs import durable
from prismoid.jobs.queues import task_queue
from prismoid.jobs.run_task import run_task
from prismoid.operations.dispatch import task_job_key, task_turn_job_key

STALE_AFTER = dt.timedelta(seconds=60)
# A continuation's crash window (commit → enqueue) is rescued from `planning`. The horizon is
# wider than STALE_AFTER because a task legitimately sits in `planning` with no event writes for
# the whole planner model call - `updated_at` refreshes on every event append, so the quiet
# stretch is exactly the provider await. Sweeping a LIVE planning turn is a DBOS-dedupe no-op
# either way; the wider horizon only keeps the log honest.
PLANNING_STALE_AFTER = dt.timedelta(minutes=2)
# A `waiting` task whose steps are ALL settled and whose gates are ALL closed is stranded one
# illegal edge from delivery - the un-flip after a parked question was lost with a
# dead worker. Quiet-time horizon keeps the sweep off live un-park races; a legitimately
# waiting task always holds an open gate and is never touched.
WAITING_STALE_AFTER = dt.timedelta(minutes=2)


@durable.schedule("* * * * *")  # every minute
@durable.job("sweep_orphans")
async def sweep_orphans(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Re-enqueue `created` tasks older than 60 s, and `planning` turns nothing is moving.

    Returns None because DBOS's scheduled workflows must. The count goes to the log, where a human
    can see it - not to a caller that does not exist. Both sweeps are safe to be dumb about for
    the same reason: the job id is deterministic per turn, so re-enqueueing work that IS running
    dedupes to a no-op, and re-enqueueing work that lost its enqueue is exactly the rescue.

    **Rule 5, deliberate exception (session I/O in a workflow body, not an @DBOS.step effect).**
    A scheduled sweep must read the CURRENT world every minute and act on it; journaling those
    reads into steps would make a replay act on a stale snapshot - the opposite of what a sweep
    is for. It is idempotent by construction (deterministic ids + DBOS dedupe; `resolve`/`deliver`
    re-check terminal state), so the durability a step would add buys nothing here and would harm
    correctness. The reads stay inline, on purpose.
    """
    async with session_scope(platform=True) as session:  # cross-workspace, ids only
        orphans = [t.id for t in await Task.stale_created(session, utcnow() - STALE_AFTER)]
        stuck = [(t.id, t.turn) for t in await Task.stale_planning(session, utcnow() - PLANNING_STALE_AFTER)]

    for task_id in orphans:
        logfire.warn("sweeper re-enqueuing orphaned task {task_id}", task_id=str(task_id))
        await durable.start(task_queue, task_job_key(task_id), run_task, str(task_id))

    for task_id, turn in stuck:
        logfire.info(
            "sweeper re-enqueuing quiet planning turn {turn} of {task_id} (no-op if live)",
            turn=turn,
            task_id=str(task_id),
        )
        await durable.start(task_queue, task_turn_job_key(task_id, turn), run_task, str(task_id), turn)

    if orphans or stuck:
        logfire.info(
            "sweeper re-enqueued {count} orphaned task(s), {stuck} quiet turn(s)",
            count=len(orphans),
            stuck=len(stuck),
        )

    await _deliver_stranded()


async def _deliver_stranded() -> None:
    """The delivery belt: a `waiting` task with every step settled and every gate
    closed has finished - it just lost the un-flip that lets it say so. Deliver it here,
    through the same operation the workflow would have used, with the summary rebuilt from the
    turn's own durable narrations. A finished, paid-for 47-page document
    stranded behind `waiting → delivered` after a worker restart on a parked question."""
    from prismoid.db.models import tenant
    from prismoid.db.models.event import TaskEvent
    from prismoid.db.models.gate import Gate
    from prismoid.db.models.plan import PlanStep
    from prismoid.operations import execution
    from prismoid.schemas.plan import StepState

    async with session_scope(platform=True) as session:
        candidates = await Task.stale_waiting(session, utcnow() - WAITING_STALE_AFTER)

        for task in candidates:
            # Bind FIRST: every read below is a per-task read, and a platform unit may
            # enumerate ids but never run a fenced fetcher - the task's workspace governs all of it.
            tenant.bind(session, task.workspace_id)
            steps = await PlanStep.for_task(session, task.id)
            if not steps or any(s.step_state not in (StepState.DONE, StepState.SKIPPED) for s in steps):
                continue  # work genuinely outstanding - not ours
            if await Gate.open_for_task(session, task.id):
                continue  # a live question - legitimately waiting

            # **The double-deliver guard.** `candidates` was read at the top of this scan; a
            # recovered parent workflow may have delivered this task in the meantime. deliver()
            # guards on is_terminal(), but on THIS session's cached (stale) row - refresh it so
            # both that guard and ours see the committed truth, and skip an already-settled task.
            await session.refresh(task)
            if task.is_terminal():
                continue

            logfire.warn(
                "sweeper delivering stranded task {t} (all steps settled, no open gates, waiting)",
                t=str(task.id),
            )
            planned = await TaskEvent.latest_of_type(session, task.id, "task.planned")
            narrations = await TaskEvent.of_types(
                session, task.id, ["step.narration"], since=int(getattr(planned, "seq", 0) or 0)
            )
            summary = "\n\n".join(getattr(n, "markdown", "") for n in narrations).strip()
            await execution.deliver(
                session,
                task.id,
                summary or "Delivered - the step record carries the details.",
                turn=task.turn,
                reopen=False,  # no workflow would run a re-draft: a steer still waiting is rejected
            )
            await session.commit()
