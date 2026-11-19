# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The top of a fresh rung: the task read again, the doors that can stop it before it buys a run
(an ended task, a command left running on the device, the budget), then the attempt counted (a run
recovered mid-flight re-enters its own, ADR-089) and the steers accepted since the step last took
one folded in - except into a run re-entered, which takes them at its first live safe point.
"""

from __future__ import annotations

from prismoid.db.models.machine import Machine
from prismoid.db.models.steer import Steer
from prismoid.operations import gates, machines, steering
from prismoid.operations.execution import mounts, parks, steers
from prismoid.operations.execution.handlers.state import Ladder
from prismoid.operations.execution.record import hand_back, owe_run, owed_run, with_record
from prismoid.operations.execution.taps import step_event
from prismoid.schemas.plan import StepSpec, StepState
from prismoid.schemas.steering import SafePoint

# Each fresh rung reads the task again (`task` was loaded when execute_step began,
# expire_on_commit=False). Its status: cancel is the only mid-step interrupt, and an infra rung
# nets out of every cap, so a Machine that fails at once would retry forever without the door's
# eject at every rung. Its epoch: a steer may have moved it since, and the step takes it below. Its
# budget and Project: the Bursar below reads them (a cap raised, or a move between Projects, while
# the last attempt ran).
FRESH_TASK = [
    "status",
    "plan_epoch",
    "turn_epoch",
    "epoch_actor_id",
    "turn_actor_id",
    "budget",
    "project_id",
    "machine_driver",
]


async def open_fresh(ladder: Ladder) -> str | None:
    """The step's final word if a door stops the rung, else None with the attempt begun. Commits
    the attempt's start; leaves no row lock held."""
    session, task, step = ladder.session, ladder.task, ladder.step
    await session.refresh(task, attribute_names=FRESH_TASK)
    if task.is_terminal():
        return f"(the task ended while step {step.title!r} was in flight)"
    beside = await parks.park_beside_a_command(
        session, task, step, ladder.spec, seat=ladder.seat, gotchas=ladder.gotchas
    )
    if beside is not None:
        return beside
    if (
        step.attempt > 0
        and not ladder.budget_consented
        and not ladder.reentered
        and (parked := await _budget_gate(ladder)) is not None
    ):
        return parked
    ladder.budget_consented = False
    if not ladder.reentered:  # a recovered run re-enters its own attempt: its call log replays what it bought
        step.attempt += 1
    owed = owed_run(step)
    if not ladder.reentered and (owed is None or owed.pickup != "reenter"):
        await _take_steers(ladder)  # a run re-entered keeps its epoch: it reads a later steer live
    await step_event(session, task, step, StepState.RUNNING)
    await session.commit()
    return None


async def _budget_gate(ladder: Ladder) -> str | None:
    """**The Bursar before every further attempt**: a retry or the next seat buys a whole new run,
    and the task loop only checks between steps - live, one navigator step's second attempt took a
    $3 task to $13.61. The first attempt was checked at the frontier; an answered gate buys exactly
    the one attempt it asked about. The state rides the gate's own commit, so a crash between them
    re-parks. The park is a hand-back like any other (`HANDBACKS`): recorded, so a replay re-issues
    it in order, and the record survives its state. Commits only when it parks."""
    session, task, step = ladder.session, ladder.task, ladder.step
    step.resume_state = with_record(
        step, {"budget_gate": True, "gotchas": ladder.gotchas, "failures": ladder.failures}
    )
    if (budget_gate := await gates.check_budget(session, task.id, step_id=step.id)) is not None:
        outcome = hand_back(step, f"park:{budget_gate}")
        await session.commit()
        return outcome
    step.resume_state = with_record(step, None)
    return None


async def _take_steers(ladder: Ladder) -> None:
    """Fold every steer accepted since the step last took one into the contract (a continued attempt is
    told them). The caller commits."""
    session, task, step = ladder.session, ladder.task, ladder.step
    # What the step had taken before this attempt - the dead run's own takes included: its inbox
    # moved the epoch on its own session, and a continued run already heard them.
    await steers.reread_epoch(session, step)
    prior_epoch = step.epoch
    steers.stamp_attempt(task, step)
    fresh = await Steer.newer_than(session, task.id, turn=task.turn, epoch=prior_epoch)
    taken = await steering.notes(session, fresh)
    if (owed := owed_run(step)) is not None:
        owe_run(step, owed.owe(taken))
    await steering.apply(session, task, step, taken, SafePoint.BEFORE_MODEL)


async def on_paper(ladder: Ladder, spec: StepSpec) -> bool:
    """Does this rung start on paper? Only a paper step whose mounts fit and that has not outgrown paper
    (a `PaperFull` rung takes the Machine)."""
    has_machine = await Machine.for_task(ladder.session, ladder.task.id) is not None
    if ladder.outgrew_paper or not machines.starts_on_paper(
        ladder.task, spec, seat=ladder.seated, has_machine=has_machine
    ):
        return False
    mounted = await mounts.mounted_artifacts(ladder.session, ladder.task, ladder.step, spec, ladder.upstream)
    return machines.fits_on_paper(ladder.task, [a.bytes for a in mounted])
