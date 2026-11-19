# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`run_task` - the durable orchestrator. One job per TURN, id `task-{id}` / `task-{id}-t{n}`.

A **Task** is what the user asked for; a **job** is the machine process that runs it. A task
is now a conversation: each follow-up bumps `tasks.turn` and runs as its own workflow, because a
completed DBOS workflow id never re-executes. Turn 1 keeps the id `task-{id}` unchanged.

The loop: **plan → dispatch the frontier → deliver.** A continuation (turn ≥ 2) skips the intake
round and the namer - the thread is the clarification and the title exists - and goes straight to
planning, which drafts the NEW message with the conversation in hand.

**Zero I/O in this body**. Every line that touches the world is an effect. Every
status-writing effect carries `turn` so a crash-recovered old-turn workflow can never write over
the turn that superseded it (the guard lives in `operations.execution`).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from enum import StrEnum
from uuid import UUID

import logfire

from prismoid.errors import CouldNotDo, PrismoidError
from prismoid.jobs import durable, effects
from prismoid.jobs.queues import step_queue
from prismoid.operations.dispatch import step_job_key
from prismoid.operations.gates import GATE_TOPIC
from prismoid.operations.steering import STEER_TOPIC
from prismoid.schemas.plan import TaskEnding

# How long a task will wait for its one clarifying question before proceeding on the defaults.
#
# **The wait costs nothing.** `durable.wait` parks the workflow in the system database - no
# thread, no poll, no container - so this number is a *product* decision about how long a human is
# allowed to be at lunch, not a resource one. 24 hours is the QuestionSet's TTL, and the two
# must agree: a workflow that gives up before the gate expires leaves a question hanging in the
# user's queue that nothing will ever consume.
CLARIFY_TIMEOUT_S = 24 * 60 * 60


class _Next(StrEnum):
    """What a stage of the turn tells the loop to do next. A plain return value, never journaled:
    the workflow's own result ("cancelled", "failed", "delivered") and the effects' results are the
    frozen words, and these are neither."""

    GO_ON = "go_on"  # carry on: dispatch the wave, or move to the next stage
    AGAIN = "again"  # read the frontier again before dispatching anything
    CANCELLED = "cancelled"  # the task was cancelled: leave through `_cancelled`


async def _hold_while_paused(task_id: str) -> _Next:
    """Park at the boundary while the row says paused. Returns CANCELLED to exit the
    workflow, AGAIN when it parked and was resumed, GO_ON when there was no pause. `while`,
    not `if`: the recv's own timeout must re-park - a paused task that auto-resumed after
    24 h of silence would dispatch waves while its row still said paused (the human said stop;
    only the human un-says it). The record says the pause took hold once, before the first park
    ("Pausing…" becomes "Paused by")."""
    if not await effects.pause_held(task_id):
        return _Next.GO_ON
    while await effects.paused(task_id):
        logfire.info("task {task_id} paused at boundary", task_id=task_id)
        signal = await durable.wait(STEER_TOPIC, timeout_s=CLARIFY_TIMEOUT_S)
        if (signal or {}).get("cancelled") or await effects.is_cancelled(task_id):
            return _Next.CANCELLED  # the cancel endpoint already wrote the row
    return _Next.AGAIN


def summary_of(narrations: Sequence[tuple[str, str]]) -> str:
    """What the turn delivers in words: each step's narration, in the order the steps finished."""
    return "\n\n".join(text for _, text in narrations if text and text.strip())


def kept_narrations(
    narrations: Sequence[tuple[str, str]], replaced: Collection[str]
) -> list[tuple[str, str]]:
    """Fence 2 re-drafted these sinks: a steer superseded their answer, so their
    words leave the summary, the chat card and the inbox - the re-drafts' replace them."""
    gone = set(replaced)
    return [(step_id, text) for step_id, text in narrations if step_id not in gone]


async def _hold_for_budget(task_id: str, budget_gate: str, turn: int) -> _Next:
    """Park on an open budget gate until a human decides. accept → proceed (GO_ON);
    cancel AND decline → row written, CANCELLED. Only the recv's 24 h timeout (silence)
    re-parks: a budget question has no safe default - silence must never spend past the cap."""
    while True:
        # Re-check before every park: header cancel already wrote the row and woke GATE;
        # a timed-out recv must not re-park a dead task forever.
        if await effects.is_cancelled(task_id):
            return _Next.CANCELLED
        signal = await durable.wait(GATE_TOPIC, timeout_s=CLARIFY_TIMEOUT_S)
        if signal is None:
            # The recv timed out. The gate ROW is the durable truth - if the resolution's wake
            # was lost in transit (a transient send failure), the answer is already sitting on
            # the row, and re-parking on it for another day would strand a decided task
            signal = await effects.gate_resolution(budget_gate)
        action = (signal or {}).get("action")
        if action in ("cancel", "decline") or (signal or {}).get("cancelled"):
            # **decline maps to cancel here (safe choice).** A budget gate has NO safe default:
            # declining is an explicit "do not raise the budget", so proceeding would spend past
            # the cap. The old code fell through and RE-PARKED forever on an answer already given
            # (the zombie-park bug); a decline now stops the task honestly. Only silence (a None
            # signal from a timed-out recv) re-parks - `action` is None then, and falls through.
            # The row, not just the workflow: without this the task sat at `waiting` forever while
            # the workflow exited "cancelled".
            await effects.mark_cancelled(task_id, turn)
            return _Next.CANCELLED
        if action == "accept":
            # Back to running and dispatch THIS wave - not a re-check, or an owner who
            # accepts without raising would be asked the identical question forever. The
            # next boundary checks again, so a raise covering one wave still stops there.
            status = await effects.resume_from_budget(task_id, turn)
            if status == "cancelled" or await effects.is_cancelled(task_id):
                return _Next.CANCELLED
            return _Next.GO_ON
        logfire.info("budget gate {g} unanswered; parking again", g=budget_gate)


async def _cancelled(task_id: str) -> str:
    """Every cancel exit reports home: a chat-born task's card settles as
    cancelled instead of spinning forever (found in review). The report dedupes on the record,
    so the cancel operation's own report and this one never stack."""
    await effects.conversation_report(task_id)
    return "cancelled"


@durable.job("run_task")
async def run_task(task_id: str, turn: int = 1) -> str:
    """`turn` defaults to 1 so a workflow journalled before turns existed recovers unchanged."""
    logfire.info("run_task {task_id} (turn {turn})", task_id=task_id, turn=turn)

    # The dequeue mark, first: `task.created → task.dequeued` IS queue hop 1 on
    # the record - without it the poll gap fuses into the first model call forever.
    if await _dequeue(task_id, turn) is _Next.CANCELLED:
        return await _cancelled(task_id)

    # The deterministic express door: a turn adjudged trivial skips the intake
    # round by construction (nothing material to ask) and, below, is titled after delivery. The
    # mint's own re-derivation of this pure verdict is what routes the plan.
    express = await effects.turn_is_express(task_id)

    if turn <= 1 and await _intake(task_id, turn, express=express) is _Next.CANCELLED:
        return await _cancelled(task_id)
    # Turn ≥ 2 skips all of that: `continue_task` already wrote `planning`, the task
    # is named, and the thread IS the clarification - a follow-up that got re-interviewed would
    # be a conversation that forgot it was one. Mid-run `ask_user` stays available.

    await effects.plan_task(task_id)
    await effects.mark_running(task_id, turn)
    parallel = await effects.fanout_enabled(task_id)

    narrations: list[tuple[str, str]] = []  # (step id, what it said)
    # **Fence 2 sends the turn round**: a delivery that finds an accepted steer
    # nothing took re-drafts the turn's sinks and answers "reopened" with the sinks it replaced -
    # their narrations leave, the frontier runs again, and delivery is asked again.
    while True:
        try:
            if (
                await _run_frontier(task_id, turn, parallel=parallel, narrations=narrations)
                is _Next.CANCELLED
            ):
                return await _cancelled(task_id)
        except Exception as exc:
            logfire.exception("task {task_id} failed", task_id=task_id)
            await effects.fail_task(task_id, _failure_said(exc), turn, _failure_ended(exc))
            await effects.conversation_report(task_id)
            return "failed"

        # A paused task never tries to deliver: delivery is a boundary too, and a pause that
        # landed during the last wave holds here until resume (or a cancel). The plan may have
        # been edited while it was paused (a replan is legal then): read the frontier again.
        held = await _hold_while_paused(task_id)
        if held is _Next.CANCELLED:
            return await _cancelled(task_id)
        if held is _Next.AGAIN:
            continue

        if await effects.is_cancelled(task_id):
            return await _cancelled(task_id)

        delivery = await effects.deliver_task(task_id, summary_of(narrations), turn)
        if delivery["verdict"] == "held":
            continue  # a pause landed after the hold above read the row: it holds there now
        if delivery["verdict"] != "reopened":
            break
        narrations = kept_narrations(narrations, delivery["replaced"])

    await effects.conversation_report(task_id)
    await _after_delivery(task_id, turn, express=express)
    return "delivered"


async def _dequeue(task_id: str, turn: int) -> _Next:
    """The dequeue mark and what it says before anything spends: CANCELLED for a turn whose person
    is suspended or gone (ADR-087 section 2), a hold on the "allowance spent" gate it opened when the
    workspace has spent its allowance (ADR-103: the budget park's own effects, so the allowance coming
    back resumes it), else GO_ON.
    Both ride the mark's return value, so the journal's shape is unchanged: an old journal recorded
    None there, and None means carry on."""
    verdict = await effects.mark_dequeued(task_id, turn)
    if verdict is True:
        return _Next.CANCELLED
    if verdict:
        logfire.info("task {task_id} held at its start on gate {g}", task_id=task_id, g=verdict)
        return await _hold_for_budget(task_id, verdict, turn)
    return _Next.GO_ON


async def _intake(task_id: str, turn: int, *, express: bool) -> _Next:
    """Turn 1's way into planning: straight there on express, else the namer-starting intake effect.
    CANCELLED when a clarification cancelled the task."""
    if express:
        await effects.mark_planning(task_id, turn)
        return _Next.GO_ON
    # The namer starts beside the plan and is never awaited here (the turn's last effect, `title_task`,
    # takes its title). A gate id replayed from an older journal parks here, durably, at zero cost,
    # until a human answers or the clock runs out.
    gate_id = await effects.intake_task(task_id)
    if not gate_id:
        await effects.mark_planning(task_id, turn)
        return _Next.GO_ON
    logfire.info("parked on gate {gate_id}", gate_id=gate_id)
    resolution = await durable.wait(GATE_TOPIC, timeout_s=CLARIFY_TIMEOUT_S)
    if resolution is None:
        # Timed out - consult the gate ROW before defaulting: a resolution whose wake
        # was lost must not be replayed as "nobody answered".
        resolution = await effects.gate_resolution(gate_id)
    if await effects.apply_clarification(task_id, resolution) == "cancelled":
        return _Next.CANCELLED
    return _Next.GO_ON


async def _run_frontier(
    task_id: str, turn: int, *, parallel: bool, narrations: list[tuple[str, str]]
) -> _Next:
    """Run waves of ready steps (all dependencies done) until none is ready, appending each narration.
    CANCELLED when the task was cancelled at a wave's boundary."""
    while frontier := await effects.frontier(task_id):
        boundary = await _wave_boundary(task_id, turn)
        if boundary is _Next.CANCELLED:
            return _Next.CANCELLED
        if boundary is _Next.AGAIN:
            continue
        await _dispatch_wave(task_id, frontier, parallel=parallel, narrations=narrations)
    return _Next.GO_ON


async def _wave_boundary(task_id: str, turn: int) -> _Next:
    """The checks before a wave starts: CANCELLED, AGAIN (read the frontier again first) or GO_ON
    (dispatch it)."""
    # **The frontier boundary** is where a pause holds and where
    # unstarted steps re-plan for a steer; a running step takes steers at its own safe
    # points. Before dispatching a wave, honour a pause (park until resume, or notice a
    # cancel) - the current wave, if any, has already finished, so nothing running is
    # interrupted. `cancel` is the only mid-step interrupt and it does not come through
    # here; it lands as a failed durable write inside the step. A held turn reads its
    # frontier again: an editor's replan is legal while it holds, and a step it deleted
    # must never be dispatched.
    held = await _hold_while_paused(task_id)
    if held is not _Next.GO_ON:
        return held

    # Header cancel can flip the row while we were paused (or between checks). The
    # status, not the signal, is authoritative - never dispatch a cancelled task.
    if await effects.is_cancelled(task_id):
        return _Next.CANCELLED

    # Unstarted steps a steer overtook are re-drafted under its epoch. When the frontier
    # moved (re-planned, or changed under the draft) after a model call's time: go round
    # again, so this wave is the current plan's and a pause or cancel that landed meanwhile
    # is honoured before it starts.
    if await effects.steer_boundary(task_id):
        return _Next.AGAIN

    # **The Bursar**. Stop *before* the overrun, not after: if the task or its
    # project has reached its ceiling, it parks on a `budget` gate here - the owner raises the
    # budget and resolves, or cancels - rather than running one more expensive wave and
    # billing past the limit. A budget checked only after the spend is a receipt.
    budget_gate = await effects.check_budget(task_id)
    if budget_gate:
        logfire.info("task {task_id} parked on budget gate {g}", task_id=task_id, g=budget_gate)
        return await _hold_for_budget(task_id, budget_gate, turn)
    return _Next.GO_ON


async def _dispatch_wave(
    task_id: str, frontier: list[str], *, parallel: bool, narrations: list[tuple[str, str]]
) -> None:
    """Run one wave and append what each step said, in the frontier's order."""
    if parallel and len(frontier) > 1:
        # **Child workflows on `step_queue`, not `asyncio.gather`.** Two `@DBOS.step`s under
        # a gather run *strictly serially* - DBOS journals a workflow's steps in sequence -
        # and it looks exactly like concurrency in the source. A queued child workflow is the
        # real thing, and each one keeps its own durable journal.
        logfire.info("frontier: {n} steps in parallel", n=len(frontier))
        # `dispatch_frontier` answers in the frontier's order.
        narrations += zip(frontier, await effects.dispatch_frontier(task_id, frontier), strict=True)
        return
    # **The serial path rides `step_queue` too, one at a time** (bounded, not inline).
    # Called inline, `execute_step` ran inside THIS run_task workflow - so its sandbox
    # was bounded by task_queue (64), not step_concurrency (12), and N serial/one-step
    # tasks could open N sandboxes past the intended cap. Enqueue on step_queue like the
    # parallel wave, but await each before the next so serial ORDER holds. A deterministic
    # child id (`step-{id}`) is preserved so a mid-run `ask_user` gate can address this
    # exact workflow to wake it; `join` re-raises a child failure to
    # the loop's try/except, exactly as the inline call did.
    for step_id in frontier:
        key = step_job_key(UUID(step_id))
        handle = await durable.start(step_queue, key, effects.execute_step, task_id, step_id)
        narrations.append((step_id, await durable.join(handle)))


def _failure_said(exc: Exception) -> str:
    """The task's failure reason: a domain error's own message (written for people), else the type."""
    return exc.message if isinstance(exc, PrismoidError) else f"{type(exc).__name__}: {exc}"


def _failure_ended(exc: Exception) -> str | None:
    """The failure's typed ending: `could_not` when an agent answered it could not do the work."""
    return TaskEnding.COULD_NOT.value if isinstance(exc, CouldNotDo) else None


async def _after_delivery(task_id: str, turn: int, *, express: bool) -> None:
    """The title and the memories a delivered turn may still earn; neither can fail it."""
    if express and turn <= 1:
        # The shelf still deserves a label, but the namer must never gate the
        # answer - on express, turn 1's title is minted AFTER delivery (fills silence only).
        # A Lead turn that ended in a plan entered as express too, so its namer never ran at
        # intake: it is titled here like any express turn.
        await effects.title_task(task_id)
    # After delivery, never instead of it: memory_extract files what this task taught us about
    # the user (≤3 private memories, usually none). It swallows every error - a broken
    # extraction must never turn "delivered" into "failed". Express turns extract too ("remember
    # I prefer tables" is a quick turn). `turn_escalated` is replay only: its answer is unused, and
    # the call stays so a workflow journalled before the Lead replays in order.
    if express:
        await effects.turn_escalated(task_id)
    await effects.remember_task(task_id)
    if not express and turn <= 1:
        # The namer intake started beside the plan, handed over: awaited if still naming,
        # named again if a crash lost it. LAST on purpose - appended after every effect an
        # in-flight turn already journalled, so a recovered workflow replays unchanged.
        await effects.title_task(task_id)
