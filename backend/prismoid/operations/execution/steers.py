# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The steers a step takes: the epoch an attempt starts on, the contract as steered, and fence 1,
which sends a step that missed a steer round again in place.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import steering
from prismoid.operations.execution.errors import IncompleteStep
from prismoid.operations.execution.mounts import (
    superseded_paths as _superseded_paths,
)
from prismoid.operations.execution.taps import (
    step_event as _step_event,
)
from prismoid.schemas.plan import (
    StepSpec,
    StepState,
)
from prismoid.schemas.steering import (
    SafePoint,
    merge_directives,
    rerun_text,
)


def stamp_attempt(task: Task, step: PlanStep) -> None:
    """A fresh attempt takes the task's epoch and its actor: the contract merge at the
    attempt's start folds in every steer accepted so far, so the step has seen them, and its spend
    wears the epoch's author."""
    step.epoch = task.plan_epoch
    step.epoch_actor_id = task.epoch_actor_id or task.turn_actor_id


async def reread_epoch(session: AsyncSession, step: PlanStep) -> None:
    """The run moves the step's epoch on the inbox's own session, and this session keeps
    its pre-run copy (expire_on_commit=False): read it again before anything asks what the step
    took. The caller's step has no unflushed epoch of its own here."""
    await session.refresh(step, attribute_names=["epoch", "epoch_actor_id"])


async def steered(session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec) -> StepSpec:
    """The contract as steered: the planner's acceptance plus every steer this step has
    taken, as criteria the verifier checks - never a rewrite of the goal (`merge_directives`)."""
    if spec.contract is None:
        raise IncompleteStep(step.id, "contract")
    await reread_epoch(session, step)
    taken = [row.text for row in await steering.seen_by(session, task, step)]
    if not taken:
        return spec
    _, acceptance = merge_directives(spec.contract.goal, spec.contract.acceptance, taken)
    return spec.model_copy(update={"contract": spec.contract.model_copy(update={"acceptance": acceptance})})


async def fence(
    session: AsyncSession, task: Task, step: PlanStep, produced: Sequence[Artifact], gotchas: list[str]
) -> bool:
    """Fence 1: under the task row lock, has this step missed a steer of its turn?
    If so its output is superseded and the caller sends it round again, in place - an infra rung,
    never the model's: the ladder never pays for a steer. What it took is read under the lock and
    after the step's epoch is read again (the run moved it on the inbox's session), so a steer
    retracted before the lock never reads as applied. The next fresh attempt is told
    through `gotchas`. True means go round; False, the step may finish (the lock is still held)."""
    await Task.hold(session, task.id)
    await reread_epoch(session, step)
    missed = await steering.unseen(session, task, step)
    if not missed or not await steering.reruns(session, task, step):
        # A step that acted in the world finishes (R45, R53); delivery says the steer came too late.
        return False
    taken = await steering.notes(session, missed)
    # This step's own rows: a digest hit on an earlier turn's identical file is that turn's output.
    own = list({a.id: a for a in produced if a.step_id == step.id}.values())
    ids = [a.id for a in own]
    await Artifact.supersede(session, ids)
    step.infra_attempts += 1  # cancels the fresh attempt's bump out of model_attempts()
    await steering.apply(session, task, step, taken, SafePoint.RERUN, superseded=ids)
    step.transition(StepState.RETRYING)
    await _step_event(session, task, step, StepState.RETRYING, detail="a steer arrived as it finished")
    step.transition(StepState.READY)
    step.transition(StepState.RUNNING)
    await session.commit()
    gotchas.append(rerun_text(taken, _superseded_paths(own)))
    return True
