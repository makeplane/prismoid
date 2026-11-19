# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The produce ladder: an attempt that published nothing is told exactly why ("write the file to
/work and call publish") and goes round, while the person reads why in product words; three strikes
escalate with a diagnosis.
"""

from __future__ import annotations

import datetime as dt
import time

import logfire

from prismoid.conf import settings
from prismoid.db.models.plan import MAX_ATTEMPTS, PlanStep
from prismoid.errors import StepFailed
from prismoid.operations import call_log
from prismoid.operations import execution as engine
from prismoid.operations import swarm as swarm_ops
from prismoid.operations.execution import finish, infra, parks
from prismoid.operations.execution.handlers.state import AGAIN, Ladder, Ran, Round
from prismoid.operations.execution.record import hand_back
from prismoid.operations.execution.taps import step_event
from prismoid.operations.stopped import ended_said
from prismoid.schemas.failures import ADVICE, FailureKind
from prismoid.schemas.plan import StepSpec, StepState


async def nothing_published(ladder: Ladder, ran: Ran) -> str | Round:
    """The step's final word (skipped, or a pace to wait out), or `AGAIN`; raises once the ladder
    is spent on a step whose loss costs the whole. Commits."""
    session, task, step, spec = ladder.session, ladder.task, ladder.step, ladder.spec
    await session.refresh(task, attribute_names=["status"])
    if task.is_terminal():
        return await _stopped(ladder)
    gotcha = f"- **nothing was published.** {ran.undelivered}"
    ladder.gotchas.append(gotcha)
    worker = settings.worker
    spent = step.model_attempts() >= MAX_ATTEMPTS or infra.spent(
        step.infra_failures, worker.max_infra_attempts
    )
    if spent or ladder.final:
        # **An escalation without a diagnosis is a shrug with a stack trace attached.**
        failure = ran.failure or FailureKind.MODEL_FAILED
        await finish.escalate(session, task, step, spec, ladder.gotchas, failure=failure)
        if (lost := _lost_part(spec)) is not None:
            return await _skip(ladder, lost)
        await session.commit()
        output = ladder.contract.output
        raise StepFailed(
            f"{spec.agent} produced no {output.type.value if output else 'result'} "
            f"in {step.attempt} attempts{_infra_said(step)} - see the diagnosis on the stream"
        )
    logfire.warn(
        "step {step_id} produced nothing (attempt {n}) - retrying with the reason",
        step_id=str(step.id),
        n=step.attempt,
    )
    step.transition(StepState.RETRYING)
    await step_event(session, task, step, StepState.RETRYING, detail=_retry_said(ladder, ran))
    step.transition(StepState.READY)  # a retry is a fresh attempt, not a resumed one
    # **A provider that just failed is not asked again at once.** The transport already retried
    # the same model; past that, the next attempt waits on the workflow's durable clock, doubling
    # per infra death, with what it is owed kept on the row (`parks.back_off`).
    wait = infra.backoff_s(
        step.infra_failures, base=worker.provider_backoff_s, ceiling=worker.provider_backoff_max_s
    )
    if ladder.backoff and wait > 0:
        outcome = parks.back_off(step, seconds=wait, gotchas=ladder.gotchas, failures=ladder.failures)
        await session.commit()
        return outcome
    # **A step that cannot get its Machine retries every `wait_s`.** Acquire waits out a busy or
    # capped Machine itself; one that FAILS at once (a frame that will not start, a driver that
    # errors) would otherwise retry at once, uncapped, for ever - live, ~35 rungs a second against
    # the shared Postgres. Its next rung waits out the rest of `wait_s` on the workflow, durably,
    # with the step `ready` and holding nothing.
    if ladder.no_machine and (left := settings.machines.wait_s - (time.monotonic() - ladder.rung_at)) > 0:
        outcome = hand_back(step, f"pace:{(engine.utcnow() + dt.timedelta(seconds=left)).isoformat()}")
        await session.commit()
        return outcome
    step.transition(StepState.RUNNING)
    await session.commit()
    return AGAIN


async def _stopped(ladder: Ladder) -> str:
    """The task ended under the run (failed, cancelled) and its safe point stopped it: the step
    settles `skipped`, saying why - never `retrying` on a task nothing will run again. Commits."""
    session, task, step = ladder.session, ladder.task, ladder.step
    step.transition(StepState.SKIPPED)
    await call_log.forget(session, task.id)  # rows the run logged after the task ended
    await step_event(session, task, step, StepState.SKIPPED, detail=f"stopped: {ended_said(task.status)}")
    await session.commit()
    return f"(the task ended while step {step.title!r} was in flight)"


def _retry_said(ladder: Ladder, ran: Ran) -> str:
    """Why the step goes round, for the person: the death's kind, else what was not handed over."""
    if ran.failure is not None and ran.failure is not FailureKind.MODEL_FAILED:
        return ADVICE[ran.failure].said
    output = ladder.contract.output
    made = output.type.value.replace("_", " ") if output is not None else "result"
    return f"The attempt ended without handing over the {made}"


def _infra_said(step: PlanStep) -> str:
    """How many of the attempts the weather took, when any did: the reader of a stopped step
    needs to know an outage stopped it, not the model."""
    n = step.infra_failures
    return f" ({n} of them stopped by the provider or the sandbox, not the model)" if n else ""


def _lost_part(spec: StepSpec) -> str | None:
    """What losing this step costs when it is only a part (a fleet seat, a long-form part, a sweep's
    territory): the survivors still assemble. None: the loss costs the whole."""
    if spec.model_ref is not None:
        return "seat lost - the fleet continues without it"
    if spec.lane_role == "gather":
        return "seat lost - the sweep continues without its territory (ADR-050)"
    if spec.lane_role == "section":
        return "part lost - the next part runs, and assembly marks the gap"
    return None


async def _skip(ladder: Ladder, lost: str) -> str:
    """The step parks as `skipped`, which settles the consumer's join and passes delivery's roll
    call. Commits."""
    session, task, step, spec = ladder.session, ladder.task, ladder.step, ladder.spec
    step.transition(StepState.SKIPPED)
    await step_event(session, task, step, StepState.SKIPPED, detail=lost)
    if spec.lane_role == "gather":
        # The wave may settle on this SKIP: run the tally BEFORE promotion, so a re-scatter round
        # can land ahead of synthesis.
        await swarm_ops.after_gather(session, task, step)
    await finish.promote_dependents(session, task, step)
    await session.commit()
    what = spec.model_ref or spec.title
    return f"({what} produced nothing in {step.attempt} attempts - skipped; the diagnosis is on the stream)"
