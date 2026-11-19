# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker's refusal of work whose turn's person is suspended or gone (ADR-087 section 2).

The person is the one the current turn spends for (`turn_standing`: its author, the creator when
unset). Two doors ask it: a step's triage and a turn's start. Cancelling is the one cancel machinery,
with nobody's authority borrowed: `attribute=False` writes the status event and the gate resolutions
without an actor, the way a system cancel does, because no person decided this - the account's state
did, and naming them would blame the person we just refused.
"""

from __future__ import annotations

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.errors import NotFound
from prismoid.operations import steering
from prismoid.operations.standing import turn_standing
from prismoid.schemas.standing import Standing


async def refuse_if_gone(session: AsyncSession, task: Task) -> bool:
    """True when the person the turn spends for cannot act and the task is now cancelled (committed by the cancel, no
    row lock left held); False for an active member. A terminal task is already stopped: the cancel is
    a no-op. Task row lock only, never a Machine's, so the Machine-then-task order cannot invert."""
    standing = await turn_standing(session, task)
    if standing is Standing.ACTIVE:
        return False
    creator = await session.get(Actor, task.creator_actor_id)
    if creator is None:  # the FK restricts it; a task never outlives its creator's row
        raise NotFound("the task's creator is gone")
    logfire.warn(
        "task {task} refused: the person its turn spends for is {standing}",
        task=str(task.id),
        standing=standing.value,
    )
    await steering.cancel_task_row(session, task, creator, attribute=False)
    return True
