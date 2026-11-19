# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The prewarm: a committed plan whose step may need the computer starts the task's Machine early.

A step takes the Machine on its first call that needs the computer (`capabilities/machine_acquire.py`),
and on a remote driver a Machine's create takes tens of seconds, so ADR-070 starts it before that call.
Every booted Machine also holds a host slot, so it starts only once a plan is committed (a recipe's
mint, the Lead's plan) and one of its unstarted steps may need the computer
(`machines.plan_need`): never for a talk turn, a plan of seats, or steps that write their text on
paper, and never on a guess about the words of an ask. Nobody waits on it: the step that needs the
computer binds it warm, or starts it as it would have anyway.

The boot itself lives in `machines` (`boot`, the `machine_boot` job); this module only decides
when to enqueue it. The call is best-effort: a boot that cannot be enqueued is logged, never a
failed turn.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import machines
from prismoid.operations.planning_turns import UNSTARTED
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.plan import StepSpec


async def for_plan(session: AsyncSession, task_id: uuid.UUID) -> bool:
    """After the plan's commit, start the task's Machine when it boots early, has none and an unstarted step
    may need the computer; the boot id is per turn. True when it was enqueued."""
    try:
        task = await session.get(Task, task_id)
        if (
            task is None
            or not machines.boots_early(task)
            or await Machine.for_task(session, task.id) is not None
        ):
            return False
        unstarted = [
            StepSpec.model_validate(s.spec)
            for s in await PlanStep.for_task(session, task.id)
            if s.step_state in UNSTARTED
        ]
        if machines.plan_need_for(task, unstarted).need is not MachineNeed.COMPUTER:
            return False
        await machines.boot_early(task_id, task.turn)
    except Exception:  # fail-soft: a prewarm is a guess; the step starts its Machine itself
        logfire.exception("task {task_id}: the plan's boot was not enqueued", task_id=str(task_id))
        return False
    return True
