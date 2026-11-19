# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A run that stopped without ending: one handler per park kind, picked by the `Deferral` it saved.

Each opens its gate and hands it to the workflow (the step's final word), or - its budget spent, or
the reason for the park already gone - answers the stopped call itself and sends the same run on in
place (`AGAIN`, with `ladder.resume` set). A new park kind is one row in `ARMS` plus its handler.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import logfire

from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.operations import gates, machines, questions
from prismoid.operations.execution import giveback, parks, wake
from prismoid.operations.execution.caps import (
    MAX_STEP_APPROVAL_GATES,
    MAX_STEP_CONNECT_GATES,
    MAX_STEP_HANDOFF_GATES,
    MAX_STEP_QUESTION_GATES,
    NOT_RUN_BESIDE_HANDOFF,
    OUT_OF_CONNECTS,
    OUT_OF_HANDOFFS,
    OUT_OF_QUESTIONS,
)
from prismoid.operations.execution.deferrals import (
    ApprovalDeferral,
    BudgetDeferral,
    ConnectDeferral,
    ControlDeferral,
    Deferral,
    PauseDeferral,
    budget_state,
    park_record,
)
from prismoid.operations.execution.handlers.state import AGAIN, Ladder, Ran, Round
from prismoid.operations.execution.record import ASKING, hand_back, with_record
from prismoid.operations.execution.taps import step_event
from prismoid.schemas.guard import outcome_narration
from prismoid.schemas.park import ParkKind
from prismoid.schemas.plan import GateKind, StepState, TaskStatus

Said = str | Round | Ran


async def handle(ladder: Ladder, outcome: object) -> Said | None:
    """The park kind's handler, or None when the run produced (no park to handle)."""
    for kind, arm in ARMS:
        if isinstance(outcome, kind):
            return await arm(ladder, outcome)
    return None


async def _continue(ladder: Ladder, resume: dict[str, object]) -> Round:
    """The stopped call answers itself and the same run goes on, in place. The reads above must not
    hold a transaction open: commits first."""
    await ladder.session.commit()
    ladder.resume = resume
    return AGAIN


async def control(ladder: Ladder, outcome: ControlDeferral) -> Said:
    """**A person is at the browser**: the step's browser call met a live control lease, or the
    agent asked for one. Its own budget - handoffs only: a person TAKING control is not the agent's
    spend. Past it the verb answers itself and the run continues in place, exactly like the ask
    budget."""
    session, task, step = ladder.session, ladder.task, ladder.step
    if outcome.source == "handoff":
        asked = await Gate.count_for_step(session, step.id, kind=GateKind.CONTROL.value, source="handoff")
        if asked >= MAX_STEP_HANDOFF_GATES:
            logfire.warn(
                "step {step_id} spent its handoff budget ({n} gates) - answering in place",
                step_id=str(step.id),
                n=MAX_STEP_HANDOFF_GATES,
            )
            return await _continue(
                ladder,
                {
                    **park_record(outcome, ParkKind.CONTROL, None),
                    "answer": OUT_OF_HANDOFFS,
                    "held_text": NOT_RUN_BESIDE_HANDOFF,
                },
            )
    if outcome.ended is not None:
        # A sign-in block turned hand-off: the row says "Blocked: sign-in needed" while the gate is
        # open; the resume's own running line clears it.
        await step_event(
            session,
            task,
            step,
            StepState.RUNNING,
            detail=outcome.ended.summary,
            outcome="blocked",
            blocked_by=outcome.ended.blocked_by,
        )
    takeover = outcome.source == "takeover"
    said = await parks.open_gate(
        session,
        task,
        step,
        ladder.spec,
        parks.park_on_control(session, task, step, outcome),
        hand_back=not takeover,
    )
    if takeover:
        return await _lease_race(ladder, outcome, said)
    return said


async def _lease_race(ladder: Ladder, outcome: ControlDeferral, said: str) -> Said:
    """The race: the lease the hook saw ended before this gate committed. The settle answers the
    gate WITHOUT a wake - this body runs inside the durable `run_step` workflow, where a send is
    journaled - so the same run continues here, in place, as if the recv had returned. That park
    was never handed to the workflow, so it is recorded (`HANDBACKS`) only below, when it is: a
    replay re-issues exactly the parks it recv'd."""
    session, task, step = ladder.session, ladder.task, ladder.step
    gate_id = uuid.UUID(said.removeprefix(ASKING))
    if await wake.settle_lease_race(task.id, holder=outcome.holder, since=outcome.since):
        settled = await wake.resume_settled(session, task, step, gate_id)
        if isinstance(settled, str):
            return settled  # the task ended meanwhile: the recv path's cancel exit
        if settled is not None:
            ladder.resume = settled
            return AGAIN
    said = hand_back(step, f"park:{gate_id}")
    await session.commit()
    return said


async def question(ladder: Ladder, outcome: Deferral) -> Said:
    """**The step stopped to ask.** Persist the run so far, open a gate the human can answer, and
    hand the gate id up to the workflow to park on. The frame is alive. Unless the ask budget is
    spent: then the tool answers ITSELF and the same run continues in place - no gate, no park, no
    human on the critical path. The frame is alive (the Deferral left it parked), so the resume
    binds it. The budget counts INPUT gates only: approvals keep their own.

    A question the person already answered - the SAME question, in `/task/answers.json`
    (`questions.known_asks`) - is never put to them again: it leaves the batch, and a batch with
    nothing left answers itself, in place, with the held answer. Only turn 1 holds those answers (a
    follow-up such as "make it Friday instead" outdates them). A question offering options is a
    choice the step found mid-run, always asked.
    """
    session, task, step = ladder.session, ladder.task, ladder.step
    answers = (task.answers or {}) if task.turn <= 1 else {}
    known = questions.known_asks([p for p, _ in outcome.questions], answers)
    known = [None if options else why for (_, options), why in zip(outcome.questions, known, strict=True)]
    if any(known):
        logfire.info(
            "step {step_id} asked {n} of {total} question(s) the person already answered",
            step_id=str(step.id),
            n=sum(1 for k in known if k),
            total=len(known),
        )
        kept = [q for q, why in zip(outcome.questions, known, strict=True) if why is None]
        if not kept:
            return await _answer_in_place(ladder, outcome, questions.already_answered(known))
        outcome = replace(outcome, questions=kept)
    asked = await Gate.count_for_step(session, step.id, kind=GateKind.INPUT.value)
    if asked >= MAX_STEP_QUESTION_GATES:
        logfire.warn(
            "step {step_id} spent its ask budget ({n} gates) - auto-answering",
            step_id=str(step.id),
            n=MAX_STEP_QUESTION_GATES,
        )
        return await _answer_in_place(ladder, outcome, OUT_OF_QUESTIONS)
    return await parks.open_gate(
        session, task, step, ladder.spec, parks.park_on_question(session, task, step, outcome)
    )


async def _answer_in_place(ladder: Ladder, outcome: Deferral, answer: str) -> Round:
    """The `ask_user` call answers itself with `answer` and the same run goes on, in place."""
    return await _continue(
        ladder,
        {**park_record(outcome, ParkKind.QUESTION, None), "answer": answer},
    )


async def connect(ladder: Ladder, outcome: ConnectDeferral) -> Said:
    """**The step stopped for a SERVICE it does not hold**: the in-conversation connect block. Its
    own small budget; past it the tool answers itself and the run continues without the service."""
    session, task, step = ladder.session, ladder.task, ladder.step
    raised = await Gate.count_for_step(session, step.id, kind=GateKind.CONNECT.value)
    if raised >= MAX_STEP_CONNECT_GATES:
        logfire.warn(
            "step {step_id} spent its connect budget ({n} gates) - continuing",
            step_id=str(step.id),
            n=MAX_STEP_CONNECT_GATES,
        )
        return await _continue(
            ladder,
            {**park_record(outcome, ParkKind.CONNECT, None), "answer": OUT_OF_CONNECTS},
        )
    return await parks.open_gate(
        session, task, step, ladder.spec, parks.park_on_connect(session, task, step, outcome)
    )


async def approval(ladder: Ladder, outcome: ApprovalDeferral) -> Said:
    """**The step stopped for consent**: side-effectful connector tools under an `ask` policy. Same
    park spine, its own budget - and past the budget the default is DENY, in place: a spent
    allowance must never silently run the side effect the policy said to ask about."""
    session, task, step = ladder.session, ladder.task, ladder.step
    opened = await Gate.count_for_step(session, step.id, kind=GateKind.APPROVE.value)
    if opened >= MAX_STEP_APPROVAL_GATES:
        logfire.warn(
            "step {step_id} spent its approval budget ({n} gates) - denying in place",
            step_id=str(step.id),
            n=MAX_STEP_APPROVAL_GATES,
        )
        return await _continue(
            ladder,
            {**park_record(outcome, ParkKind.APPROVAL, None), "approval_action": "deny_overflow"},
        )
    return await parks.open_gate(
        session, task, step, ladder.spec, parks.park_on_approval(session, task, step, outcome)
    )


async def pause(ladder: Ladder, outcome: PauseDeferral) -> Said:
    """**The task is paused**: the step parked at its next tool dispatch, on a gate with no
    questions and no clock that only the task's resume (or its cancel) lifts. Unless the pause
    already lifted while the run was parking - the resume found no gate to lift - or the
    deployment's pause slots filled since the safe point saw a free one: then the run continues
    in place, now, every deferred call told it did not run (at the cap the frontier holds)."""
    session, step = ladder.session, ladder.step
    ladder.task = task = await Task.held(session, ladder.task.id)  # the lock the pause doors write under
    if task.state is TaskStatus.PAUSED and await gates.pause_slot_free(session):
        return await parks.open_gate(
            session, task, step, ladder.spec, parks.park_on_pause(session, task, step, outcome)
        )
    if task.is_terminal():
        await session.commit()
        title = step.title
        await giveback.give_back_a_dead_park(session, task, step)
        return f"(the task ended while step {title!r} was in flight)"
    ladder.resume = await parks.pause_lifted(session, task, outcome)
    await session.commit()
    return AGAIN


async def budget(ladder: Ladder, outcome: BudgetDeferral) -> Said:
    """**The task reached its cap mid-run**: the Bursar's own gate, the run saved on the row in the
    same commit. No gate means nothing stops the spend any more (the cap was raised while the run
    parked): it continues in place, now."""
    session, task, step = ladder.session, ladder.task, ladder.step
    ids = (task.id, step.id, step.attempt)  # a rollback expires the rows
    profile = machines.seat_profile(task, ladder.spec)
    step.resume_state = with_record(step, budget_state(outcome, None))
    try:
        # The Bursar reads `task` from this session's map, loaded before the run: a cap raised (or
        # a move) while the run spent must be what it sees.
        await session.refresh(task, attribute_names=["budget", "project_id"])
        gate_id = await gates.check_budget(session, task.id, step_id=step.id)
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        await giveback.give_park_back(*ids, profile=profile)
        raise
    if gate_id is not None:
        step.resume_state = with_record(step, budget_state(outcome, gate_id))
        said = hand_back(step, f"park:{gate_id}")
        await session.commit()
        return said
    step.resume_state = with_record(step, None)
    await session.commit()
    if outcome.ended is None or outcome.ended.status == "partial":
        # A partial stopped short at a cap raised since: the work goes on.
        ladder.resume = {**budget_state(outcome, None), "budget_answer": "accept", "cancelled": False}
        return AGAIN
    # The model's own budget stop, and the Bursar sees nothing over (no cap, or one not reached): no
    # gate to park on, no raise to tell it about - and continuing would let it say so again,
    # forever. Its stop is the step's blocked ending.
    await giveback.give_park_back(*ids, profile=profile)
    return Ran(outcome_narration(outcome.ended), [], outcome.ended)


# The park kinds, in the order they are asked about.
ARMS: list[tuple[type, Callable[[Ladder, Any], Awaitable[Said]]]] = [
    (ControlDeferral, control),
    (Deferral, question),
    (ConnectDeferral, connect),
    (ApprovalDeferral, approval),
    (PauseDeferral, pause),
    (BudgetDeferral, budget),
]
