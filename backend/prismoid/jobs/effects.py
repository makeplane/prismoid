# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Effects - the ONLY I/O a job may do.

Job bodies have **zero I/O**. Every read and write is a `@durable.effect`, which the engine journals: on a
crash-and-restart the workflow replays, and a completed step returns its **recorded result**
instead of running again. That is the whole durability contract, and it holds only if no I/O leaks
into the workflow body - **a single un-journalled write is one that runs twice on recovery.**

Each effect is a thin call into `operations`. Business logic lives there; this file is a
*sheet*, not a layer.

**Big payloads travel by id.** A step's arguments and return value are serialised into the DBOS
system database on every call. Passing a plan, a document, or an event list through a step argument
turns the journal into a blob store and the replay into a download.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

import logfire

from prismoid.db.models.conversation import Conversation
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import NotFound
from prismoid.jobs import durable
from prismoid.operations import push
from prismoid.operations.execution import creator
from prismoid.schemas.guard import BUDGET_STOP
from prismoid.schemas.plan import GateAction, TaskEnding, TaskStatus

if TYPE_CHECKING:
    from prismoid.operations.live import PhaseFact

# How long a step will wait on an ask_user answer before the sweep's WAIT policy leaves it parked.
# The gate's on_timeout is WAIT (a mid-run question has no safe default), so this is really just the
# recv's own ceiling; a human takes as long as a human takes.
ASK_TIMEOUT_S = 24 * 60 * 60


# The namers intake started and has not yet handed to `title_task`, by task id: this process's
# only, so a recovered workflow (intake replayed from its journal) finds none and names itself.
_NAMING: dict[str, asyncio.Task[str | None]] = {}


@durable.effect()
async def intake_task(task_id: str) -> str | None:
    """Turn 1's intake on a recipe turn: starts the namer beside the plan and asks nothing. Returns None or
    the gate id an older journal recorded, so a replayed workflow still parks on it."""
    from prismoid.operations import planning

    say = await _phase_voice(task_id)

    async def _title() -> str | None:
        try:
            await say("start", "Naming the task", agent="namer")
            async with row_scope(Task, UUID(task_id)) as session:
                title = await planning.generate_title(session, UUID(task_id))
                await session.commit()
            await say("end", "Named the task", agent="namer")
            return title
        except Exception:  # the label is never worth a failure: `title_task` names it again
            logfire.exception("background namer failed for {task_id}", task_id=task_id)
            return None

    naming = asyncio.create_task(_title())
    _NAMING[task_id] = naming
    naming.add_done_callback(
        lambda done: _NAMING.pop(task_id, None) if _NAMING.get(task_id) is done else None
    )
    return None


@durable.effect()
async def mark_dequeued(task_id: str, turn: int = 1) -> bool | str | None:
    """One durable `task.dequeued` so queue hop 1 is measurable from the record (a resume may append a
    second; measurement takes the first).

    **It also refuses first** a turn whose person (`turn_standing`) is suspended or out of the
    workspace: the task is cancelled, attributed to nobody, before anything is spent, and the
    verdict is True. **And it holds** a turn whose workspace has spent its allowance: the "allowance
    spent" gate it opened, as its id, before intake spends (`gates.hold_unstarted`). Both ride this effect's
    return so `run_task`'s journal keeps its shape: a journal that recorded None takes the old path."""
    from prismoid.operations import execution, gates

    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is not None and await creator.refuse_if_gone(session, task):
            return True
        await execution.record_dequeued(session, UUID(task_id), turn)
        await session.commit()
        if (gate_id := await gates.hold_unstarted(session, UUID(task_id), turn)) is not None:
            return str(gate_id)
    return None


@durable.effect()
async def mark_step_dequeued(task_id: str, step_id: str) -> None:
    """The step's child workflow started: `task.status(running) → step.dequeued`
    is queue hop 2 on the record, per step."""
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        await execution.record_step_dequeued(session, UUID(task_id), UUID(step_id))
        await session.commit()


@durable.effect()
async def turn_is_express(task_id: str) -> bool:
    """Whether the Lead decides this turn, as one journalled read: run_task then skips the intake round
    (the Lead asks for itself) and titles the turn after delivery. Planning reads the same rows to route."""
    from prismoid.operations.turn import route

    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is None:
            return False
        return await route.lead_decides(session, task)


@durable.effect()
async def turn_escalated(task_id: str) -> bool:
    """Replay only: kept so workflows journalled before the Lead replay in order; nothing reads its
    answer. One read of the turn's latest `task.planned`, true when it wears the retired `route_source=hatch`."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import TaskPlanned

    async with row_scope(Task, UUID(task_id)) as session:
        planned = await TaskEvent.latest_of_type(session, UUID(task_id), "task.planned")
        return isinstance(planned, TaskPlanned) and planned.route_source == "hatch"


@durable.effect()
async def clarify_task(task_id: str) -> str | None:
    """The intake round of workflows journalled before `intake_task` merged it with the namer. Nothing asks
    any more: a replay past a recorded gate id still parks on it, and a fresh call asks nothing."""
    logfire.info("clarify_task {task_id}: no intake question", task_id=task_id)
    return None


@durable.effect()
async def apply_clarification(task_id: str, resolution: dict[str, Any] | None) -> str:
    """What the human said -> the task; returns `planning` or `cancelled`. A None resolution is a timeout,
    and the gate proceeds with the asker's defaults."""
    from prismoid.operations import execution, gates

    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is None:
            raise NotFound("no such task")

        # Header cancel already wrote the row and may have closed the gate - never reanimate.
        if task.state is TaskStatus.CANCELLED:
            return "cancelled"

        action = (resolution or {}).get("action")
        if action == GateAction.CANCEL.value or (resolution or {}).get("cancelled"):
            await execution.advance(session, UUID(task_id), TaskStatus.CANCELLED)
            await session.commit()
            return "cancelled"
        if await creator.refuse_if_gone(session, task):  # an intake park resumed after they left
            return "cancelled"

        task.answers = await gates.answers_for(session, UUID(task_id), resolution)
        if resolution is None:
            await gates.expire_open(session, task)  # nobody answered; close the gate, on the record
        await session.flush()

        status = await execution.advance(session, UUID(task_id), TaskStatus.PLANNING)
        await session.commit()
    return status


@durable.effect()
async def title_task(task_id: str) -> str | None:
    """Mint the display title (namer agent) - fills silence only; a user rename wins. The namer
    intake started is awaited, never asked twice; once it finished, `generate_title` finds the
    title set and returns without a model call."""
    from prismoid.operations import planning

    if (naming := _NAMING.pop(task_id, None)) is not None and (title := await naming) is not None:
        return title  # else: a failed namer names again below; a user's rename makes that a no-op
    async with row_scope(Task, UUID(task_id)) as session:
        title = await planning.generate_title(session, UUID(task_id))
        await session.commit()
    return title


@durable.effect()
async def mark_planning(task_id: str, turn: int = 1) -> str:
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        status = await execution.advance(session, UUID(task_id), TaskStatus.PLANNING, turn=turn)
        await session.commit()
    return status


@durable.effect()
async def plan_task(task_id: str) -> list[str]:
    """Draft the plan. Returns **step ids**, not a plan - big payloads travel by id.

    The workflow no longer *walks* this list - it asks for the frontier instead - but the
    return value stays, because DBOS journals it and it is the cheapest possible record of what the
    plan was at the moment it was made.
    """
    from prismoid.operations import early_boot, planning

    say = await _phase_voice(task_id)
    await say("start", "Drafting the plan", agent="planner")
    async with row_scope(Task, UUID(task_id)) as session:
        view = await planning.draft_plan(session, UUID(task_id))
        await session.commit()
        await early_boot.for_plan(session, UUID(task_id))  # after the commit, best-effort
    await say(
        "end",
        f"Drafted the plan - {len(view.steps)} step{'' if len(view.steps) == 1 else 's'}",
        agent="planner",
    )
    return [str(s.id) for s in view.steps]


async def _phase_voice(task_id: str) -> PhaseFact:
    """Bind this task's phase-fact voice (`operations.live.phase_narrator` - the envelope and
    channel grammar live there, not in this sheet): one PK read for the project + anchor every
    other Lane-2 emitter stamps. A step's Wire does that binding; nothing exists pre-step to
    do it, so the effect reads and binds. Best-effort like the narration itself: a failed
    read binds an anchorless voice rather than costing intake or planning."""
    import contextlib

    from prismoid.operations import live
    from prismoid.schemas.ids import ProjectId, TaskId

    tid = UUID(task_id)
    project_id: ProjectId | None = None
    anchor = 0
    with contextlib.suppress(Exception):
        async with row_scope(Task, UUID(task_id)) as session:
            task = await session.get(Task, tid)
            if task is not None:
                project_id = ProjectId(task.project_id) if task.project_id else None
                anchor = task.event_seq
    return live.phase_narrator(TaskId(tid), project_id=project_id, after_seq=anchor)


@durable.effect()
async def frontier(task_id: str) -> list[str]:
    """Every step that can run **now** - dependencies all satisfied, nothing else in its way.

    A `@durable.effect`, and it must be: it reads the database, and the workflow body may not.
    Its recorded result is what a replay sees, which is also what makes the wave loop deterministic
    under recovery.
    """
    from prismoid.db.models.plan import PlanStep

    async with row_scope(Task, UUID(task_id)) as session:
        return [str(s.id) for s in await PlanStep.frontier(session, UUID(task_id))]


@durable.effect()
async def check_budget(task_id: str) -> str | None:
    """The Bursar at the frontier boundary. Returns a gate id to park on, or None."""
    from prismoid.operations import gates

    async with row_scope(Task, UUID(task_id)) as session:
        gate_id = await gates.check_budget(session, UUID(task_id))
    return str(gate_id) if gate_id else None


@durable.effect()
async def gate_resolution(gate_id: str) -> dict[str, Any] | None:
    """The gate ROW's recorded resolution, or None while it is open - the budget hold's
    timeout re-read: the row is the durable truth, and a resolution whose wake was lost in
    transit must not re-park a decided task for another 24 h. A by-id read
    of the gate's own row that acts for no workspace - the one effect left unbound."""
    from prismoid.db.models.gate import Gate

    async with session_scope() as session:
        gate = await session.get(Gate, UUID(gate_id))
        if gate is None or gate.is_open() or gate.resolution is None:
            return None
        return dict(gate.resolution)


@durable.effect()
async def resume_from_budget(task_id: str, turn: int = 1) -> str:
    """The owner resolved a budget gate (or the allowance's wake answered it); move the task back to running."""
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is None:
            raise NotFound("no such task")
        # Header cancel may have already terminalised the task while we were parked.
        if task.is_terminal():
            return task.status
        # A turn held before it started (`created`, or `planning` on a follow-up) never ran: its
        # intake or planning moves the status, not this resume.
        if task.state in (TaskStatus.CREATED, TaskStatus.PLANNING):
            return task.status
        status = await execution.advance(session, UUID(task_id), TaskStatus.RUNNING, turn=turn)
        await session.commit()
    return status


@durable.effect()
async def mark_cancelled(task_id: str, turn: int = 1) -> str:
    """The task is over because a human said stop at its budget gate (`run_task._hold_for_budget`, the
    one caller). The workflow returning "cancelled" is not a status - the ROW is the record, and a budget-gate cancel that only ended the workflow left
    the task at `waiting` forever. Every cancel exit writes the row."""
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        status = await execution.advance(
            session, UUID(task_id), TaskStatus.CANCELLED, turn=turn, reason=BUDGET_STOP
        )
        await session.commit()
    return status


@durable.effect()
async def is_cancelled(task_id: str) -> bool:
    """Did a cancel already write the row? Park loops re-check so a timed-out recv cannot
    resume or re-park a task the human already stopped."""
    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is None:
            raise NotFound("no such task")
        return task.state is TaskStatus.CANCELLED


@durable.effect()
async def paused(task_id: str) -> bool:
    """Is the task asking to stop at this boundary?. A plain status read, journalled."""
    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is None:
            raise NotFound("no such task")
        return task.state is TaskStatus.PAUSED


@durable.effect()
async def pause_held(task_id: str) -> bool:
    """Does the frontier hold for a pause? If so the record says it took hold, once,
    before the hold parks. Read under the task row lock, so a resume that lands first writes no
    "Paused by" for a pause that is already over."""
    from prismoid.operations import steering
    from prismoid.schemas.steering import SafePoint

    tid = UUID(task_id)
    async with row_scope(Task, tid) as session:
        task = await Task.held(session, tid)
        if task.state is not TaskStatus.PAUSED:
            return False
        await steering.pause_applied(session, task, None, SafePoint.BOUNDARY)
        await session.commit()
    return True


@durable.effect()
async def steer_boundary(task_id: str) -> bool:
    """Re-plan the unstarted steps a steer overtook, in one transaction. True when the frontier moved and
    the workflow must read it again; the Lead's phase ends only after the commit."""
    from prismoid.operations import steering
    from prismoid.operations.execution.lead_rung import LEAD

    async with row_scope(Task, UUID(task_id)) as session:
        outcome = await steering.boundary(session, UUID(task_id))
        await session.commit()
    if outcome.said:
        say = await _phase_voice(task_id)
        await say("end", outcome.said, agent=LEAD)
    return outcome.moved


@durable.effect()
async def fanout_enabled(task_id: str) -> bool:
    """Parallel siblings run at once - except on the device, where they run one by one:
    one folder, one agent at a time."""
    from prismoid.operations import features, machines
    from prismoid.schemas.device import DEVICE_DRIVER

    async with row_scope(Task, UUID(task_id)) as session:
        task = await session.get(Task, UUID(task_id))
        if task is None:
            raise NotFound("no such task")
        if machines.driver_of(task) == DEVICE_DRIVER:
            return False
        return await features.enabled(session, features.PARALLEL_FANOUT, task.project_id)


@durable.effect()
async def mark_running(task_id: str, turn: int = 1) -> str:
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        status = await execution.advance(session, UUID(task_id), TaskStatus.RUNNING, turn=turn)
        await session.commit()
    return status


@durable.job("run_step")
async def execute_step(task_id: str, step_id: str, seat: bool = False) -> str:
    """Run one step. **A child workflow, not a step - and that is what makes the fan-out parallel.**

    It was a `@DBOS.step`, and two of them under `asyncio.gather` ran **strictly serially**: DBOS
    journals a workflow's steps in sequence, so `gather` bought nothing at all. The live run showed
    it exactly - writer #2 started at 21:15:29, the second writer #1 finished at 21:15:29 - and it
    would have shipped as "parallel fan-out" on the strength of an `asyncio.gather` that reads like
    concurrency and is not.

    A **child workflow enqueued on `step_queue`** is the real thing: DBOS runs it on the queue's own
    worker pool, bounded by `PRISMOID_WORKER_STEP_CONCURRENCY`, each with its own durable
    journal. The parent awaits the handles. That is what `queues.py` defined `step_queue` *for* - it
    had been sitting there unused, which was the tell.

    Retries stay off. DBOS's automatic retry would re-run the whole agent - a second sandbox, a
    second set of model calls, a second bill - for a step that may have failed for a reason no retry
    can fix. Retries in this product are the **targeted ladder**: ≤3 attempts, each one *told
    what went wrong*, with the gotcha recorded.

    **And this is where a step parks on `ask_user`**. The ladder runs until the step is
    done, failed, or **asking** - and when it asks, the ladder returns the gate id, the workflow
    parks here on `recv` at zero cost, and on the human's answer it stores it and runs the ladder
    again, which binds the parked frame again (the task's Machine, or a pre-1b frame by its old
    name) and finishes the same run. The park is on *this* workflow (`step-{id}`), which is why a
    mid-run gate routes its wake here and not to `run_task`.

    `seat`: `dispatch_frontier` runs this step beside siblings, so it keeps a throwaway
    frame instead of the task's Machine. A workflow recorded before the flag recovers with the
    default, which is tolerable, not right: a serial step is right to take the Machine, but a
    parallel sibling recorded before the deploy recovers as `seat=False` and competes for the
    Machine - one sibling runs in it while the others wait "the Machine is busy" (an infra rung,
    never the task) instead of running beside it. Only in-flight waves at the deploy see it.
    """
    from prismoid.errors import InvalidTransition, ModelIncapable, NotFound, PolicyDenied, StepFailed
    from prismoid.operations import execution
    from prismoid.operations.gates import GATE_TOPIC

    await mark_step_dequeued(task_id, step_id)

    # The hand-backs this run of the body has taken. A worker death replays the body from here:
    # while the count is behind the row's record the ladder re-issues them in order, so each
    # recorded recv and sleep below hands back its own result (`execution.HANDBACKS`).
    handed_back = 0
    try:
        while True:
            async with row_scope(Task, UUID(task_id)) as session:
                outcome = await execution.execute_step(
                    session, UUID(task_id), UUID(step_id), seat=seat, handed_back=handed_back
                )

            if outcome.startswith(execution.PACING):
                # The step could not get its Machine at once: wait out the rest of
                # `wait_s` on DBOS's clock - a restart resumes the wait, never skips it - then the
                # next rung, whose door ejects it if the task was cancelled meanwhile.
                handed_back += 1
                await durable.sleep(float(outcome.removeprefix(execution.PACING)))
                continue
            if not outcome.startswith(execution.ASKING):
                return outcome

            handed_back += 1
            gate_id = outcome.removeprefix(execution.ASKING)
            logfire.info("step {step} parked on gate {gate}", step=step_id, gate=gate_id)
            answer = await durable.wait(GATE_TOPIC, timeout_s=ASK_TIMEOUT_S)
            # **The wake must name THIS gate**. The mailbox is per-step, not
            # per-gate: a stale unconsumed wake from an earlier gate (worker death between a
            # resolve's send and this recv) would otherwise be applied to a LATER park - an
            # old question's accept read as a tool approval. A stray is dropped; the ladder
            # re-parks and the right wake (or the sweep's) is still coming. Wakes without an
            # id (pre-stamp rows in flight) pass, as before.
            stray = str((answer or {}).get("gate_id") or "")
            if stray and stray != gate_id:
                logfire.warn(
                    "stale wake for gate {got} while parked on {expected} - ignored",
                    got=stray,
                    expected=gate_id,
                )
                continue
            async with row_scope(Task, UUID(task_id)) as session:
                await execution.store_answer(session, UUID(step_id), answer, handback=handed_back)
                await session.commit()
            # Header cancel closes the gate with action=cancel and wakes this step workflow.
            # Exit cleanly - do not re-enter the ladder on a task already terminal.
            if (answer or {}).get("action") == "cancel" or (answer or {}).get("cancelled"):
                # A parked BROWSER step publishes its recording and frees its frame on the way
                # out (the recording covers a cancel too). Never raises.
                async with row_scope(Task, UUID(task_id)) as session:
                    await execution.close_cancelled_park(session, UUID(task_id), UUID(step_id))
                return "(cancelled)"
    except (PolicyDenied, StepFailed, ModelIncapable, InvalidTransition, NotFound):
        raise  # our domain errors carry message-only constructors; the journal replays them whole
    except Exception as raw:
        # **Exception hygiene at the journal boundary.** A workflow's
        # error is JOURNALED and re-raised on recovery by reconstructing the exception from its
        # args - and a third-party type with required keyword-only arguments (httpx's
        # HTTPStatusError) blows up the RECOVERY with a TypeError instead of
        # replaying the failure, killing a task the original error never would have. Nothing
        # crosses this boundary unless it reconstructs from a message. CancelledError is
        # BaseException and passes untouched - sibling cancellation depends on it.
        raise RuntimeError(f"{type(raw).__name__}: {raw}") from raw


async def dispatch_frontier(task_id: str, step_ids: list[str]) -> list[str]:
    """Every step in the wave, onto `step_queue`, at once. Awaits them all.

    **Not an effect** - it enqueues child workflows and awaits their handles, which is workflow
    orchestration, not I/O (Rule 5 is about un-journalled writes, and every one of these children is
    journalled by DBOS in its own right).

    **A failed sibling stops the wave, and the wave stops its siblings.** The first
    version awaited handles one by one: the first child that raised aborted the parent into
    `fail_task` while every LATER child kept executing - billing model calls onto a task already
    marked failed, then writing step events over its grave. Now the wave waits FIRST_EXCEPTION;
    on a failure the outstanding children are cancelled (DBOS interrupts them at their next
    step boundary; the executor's terminal check ejects them at the door) before the failure
    propagates. Orphaned spend on a dead task was the single worst money leak in the plane.
    """
    import asyncio

    from prismoid.jobs.queues import step_queue
    from prismoid.operations.dispatch import step_job_key

    # **A deterministic id per step (`step-{id}`)**, because a mid-run `ask_user` parks *this* child
    # workflow and a gate resolution has to be able to address it. It also keeps DBOS's
    # dedupe honest: the same step never runs as two workflows. Every sibling is a SEAT:
    # steps running at once cannot share the task's one Machine, so each keeps a throwaway frame.
    handles = [
        await durable.start(step_queue, step_job_key(UUID(s)), execute_step, task_id, s, True)
        for s in step_ids
    ]

    # The joins ride the queue cadence too (`durable.join`) - the default 1.0 s result
    # poll added ~0.5 s mean between every finished wave and the loop noticing.
    pending = {asyncio.ensure_future(durable.join(h)): s for h, s in zip(handles, step_ids, strict=True)}
    results: dict[str, str] = {}
    failure: BaseException | None = None
    while pending and failure is None:
        done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_EXCEPTION)
        for finished in done:
            sid = pending.pop(finished)
            exc = finished.exception()
            if exc is not None:
                failure = exc
            else:
                results[sid] = finished.result()

    if failure is not None:
        for future, sid in pending.items():
            future.cancel()
            try:
                await durable.cancel(step_job_key(UUID(sid)))
            except Exception:  # a child that finished in the race window is fine; the rest are logged
                logfire.warn("could not cancel sibling step {sid} after a wave failure", sid=sid)
        if pending:
            logfire.warn(
                "wave failed - cancelled {n} in-flight sibling step(s) before failing the task",
                n=len(pending),
            )
        raise failure

    return [results[s] for s in step_ids]


class Delivered(TypedDict):
    """`deliver_task`'s journaled answer: the verdict, and the sinks a reopen replaced (step ids)."""

    verdict: str
    replaced: list[str]


@durable.effect()
async def deliver_task(task_id: str, summary: str, turn: int = 1) -> Delivered:
    """Deliver the turn: `delivered`, `skipped` (a stale turn, a settled task), `held` (paused since
    the workflow's boundary read; it holds and asks again) or `reopened` - fence 2 re-drafted the
    turn's sinks for a steer nothing took; the workflow drops those sinks'
    narrations (their answer is superseded) and runs the frontier again."""
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        delivery = await execution.deliver(session, UUID(task_id), summary, turn=turn)
        await session.commit()
    return Delivered(verdict=delivery.verdict, replaced=[str(s) for s in delivery.replaced])


@durable.effect()
async def conversation_report(task_id: str) -> int:
    """A chat-born task's result comes home, after the settle is durable: a
    SYSTEM row in its conversation (delivered or failed, with the artifacts), then the
    channel transport if the conversation rides one. REPLACES the old `channel_reply`
    (the channel ledger's out row + queued drain): the conversation holds the row now, and
    nothing queues - talk never waits on work. Its own session, its own commits, the
    remember_task contract: **must never fail the task**. Returns 1 when a row landed."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.operations import chat as chat_ops

    try:
        async with row_scope(Task, UUID(task_id)) as session:
            task = await session.get(Task, UUID(task_id))
            if task is None or task.origin_conversation_id is None:
                return 0
            status = TaskStatus(task.status)
            if status is TaskStatus.DELIVERED:
                outcome = "delivered"
                event = await TaskEvent.latest_of_type(session, task.id, "task.delivered")
                summary = str(getattr(event, "summary", "") or "") if event else ""
            else:
                # A failure or a cancel is a `task.status` frame wearing its reason - there is
                # no `task.failed` type (found in review: the row always said a bare
                # "The task failed.").
                outcome = "cancelled" if status is TaskStatus.CANCELLED else "failed"
                event = await TaskEvent.latest_of_type(session, task.id, "task.status")
                # A stop carries its account of what got done; a failure its reason.
                summary = (
                    str(getattr(event, "summary", "") or getattr(event, "reason", "") or "")
                    if event is not None and getattr(event, "status", None) == status
                    else ""
                )
            await chat_ops.on_task_settled(session, task, outcome=outcome, summary=summary)
            return 1
    except Exception:
        logfire.exception("conversation_report failed for {task_id}; delivery stands", task_id=task_id)
        return 0


@durable.effect()
async def reply_chat_once(conversation_id: str, seq: int) -> str:
    """One attempt at answering a conversation: `done` / `answered` / `busy` /
    `gone` - the workflow sleeps and retries on `busy`. The model call lives inside this ONE
    step so a replay returns the journaled outcome instead of billing a second reply."""
    from prismoid.operations import chat as chat_ops

    async with row_scope(Conversation, UUID(conversation_id)) as session:
        return await chat_ops.reply(session, UUID(conversation_id), seq)


@durable.effect()
async def reply_chat_stalled(conversation_id: str, seq: int) -> None:
    """The lane stayed claimed past its lease: say so in the thread (a row wearing its reason)
    - a wedged conversation must never stay silent. The claim itself is NOT forced open
    (see ADR-059): a holder still alive this long is a live reply, and the lease frees a dead
    one on its own - the next message's job takes the lane."""
    from prismoid.operations import chat as chat_ops

    async with row_scope(Conversation, UUID(conversation_id)) as session:
        conv = await session.get(Conversation, UUID(conversation_id))
        if conv is None:
            return
        if not await chat_ops.answered_after(session, conv, seq):
            await chat_ops.note_stall(session, conv, seq)
        await session.commit()


@durable.effect()
async def remember_task(task_id: str) -> int:
    """memory_extract after delivery (a Project task records `skipped` and mints nothing; a direct turn also
    extracts the keeper bot's lane). Never fails the task: errors are swallowed; the records guard replay."""
    from prismoid.operations import memory as memory_ops

    try:
        async with row_scope(Task, UUID(task_id)) as session:
            task = await session.get(Task, UUID(task_id))
            if task is None:
                return 0
            count = await memory_ops.remember(session, task)
            await session.commit()
            if task.direct_bot:
                count += await memory_ops.remember_for_bot(session, task)
                await session.commit()
            return count
    except Exception:
        logfire.exception("remember_task failed for {task_id}; delivery stands", task_id=task_id)
        return 0


@durable.effect()
async def pregenerate_figures(task_id: str, turn: int) -> int:
    """Every outline-promised image (ADR-030), generated once, in parallel, WHILE the section
    wave writes - never inside assembly, never re-bought by a retry. Failure costs
    placeholders, by contract, so this step swallows everything with a log."""
    from prismoid.operations import figures

    try:
        async with row_scope(Task, UUID(task_id)) as session:
            return await figures.pregenerate(session, UUID(task_id), turn)
    except Exception:
        logfire.exception("figure pre-generation failed for task {t}; placeholders stand", t=task_id)
        return 0


@durable.effect()
async def fail_task(task_id: str, reason: str, turn: int = 1, ended: str | None = None) -> None:
    """Fail the turn; `ended` is the failure's typed ending (`could_not`), None for a system fault."""
    from prismoid.operations import execution

    async with row_scope(Task, UUID(task_id)) as session:
        ending = TaskEnding(ended) if ended else None
        await execution.fail(session, UUID(task_id), reason, turn=turn, ended=ending)
        await session.commit()


@durable.effect()
async def send_inbox_push(item_id: str) -> int:
    """One inbox item's alert to its recipient's phones (ADR-100); the count delivered. Never fails
    anything: the item stands whatever FCM says."""
    try:
        async with row_scope(InboxItem, UUID(item_id)) as session:
            return await push.send_inbox(session, UUID(item_id))
    except Exception:
        logfire.exception("push for inbox item {item} failed; the item stands", item=item_id)
        return 0


@durable.effect()
async def send_read_push(actor_id: str, workspace_id: str, inbox_ids: str) -> int:
    """The silent update after a read (ADR-100): the phones' badge and list follow; the count delivered."""
    try:
        async with session_scope(UUID(workspace_id)) as session:
            return await push.send_read(session, UUID(actor_id), inbox_ids)
    except Exception:
        logfire.exception("read push for {actor} failed", actor=actor_id)
        return 0
