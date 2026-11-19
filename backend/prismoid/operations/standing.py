# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Can the person a task spends for still act? The worker asks before it spends on their behalf.

The request side stops a suspended user's tasks and a departing member's tasks; the worker rebuilds
every step and turn from the task row, so queued or parked work, or a task minted in the gap, would
resume without this read. A leaf: it reads two rows and decides, and cancels nothing.

Two questions, two people. `turn_standing` is about whoever the CURRENT turn spends for (its author,
`turn_actor_id`, the creator when unset), so a teammate's follow-up on a project task whose creator
left still runs (ADR-062). `creator_standing` is about the CREATOR alone: a connector mounts the
creator's credential, whoever is steering.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceMember
from prismoid.schemas.standing import Standing


async def _standing(session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> Standing:
    """SUSPENDED when the actor is missing or not active; LEFT when they hold no membership in the
    workspace; else ACTIVE. Two primary-key reads; commits nothing, locks nothing."""
    who = await session.get(Actor, actor_id)
    if who is None or not who.is_active():
        return Standing.SUSPENDED
    if await WorkspaceMember.get(session, workspace_id, actor_id) is None:
        return Standing.LEFT
    return Standing.ACTIVE


async def turn_standing(session: AsyncSession, task: Task) -> Standing:
    """The standing of the person the current turn spends for."""
    return await _standing(session, task.workspace_id, task.turn_actor_id or task.creator_actor_id)


async def creator_standing(session: AsyncSession, task: Task) -> Standing:
    """The standing of the task's creator, whose connector credentials a step would mount."""
    return await _standing(session, task.workspace_id, task.creator_actor_id)
