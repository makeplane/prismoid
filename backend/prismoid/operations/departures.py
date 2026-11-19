# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Leaving or losing a workspace stops your work there.

A member who is removed, or who leaves, must not leave tasks spending in a workspace they can no
longer act in. These wrappers sit in the engine layer because the cancel machinery does
(`steering`), which the nouns layer (`workspaces`) may not import.

Order is the contract: ask the verb's own refusals first (nothing written), cancel every unsettled task
the person created in that workspace or whose current turn is theirs (ADR-087 section 2: a turn spends
for its author), then run the verb, which re-checks under its own lock and removes the membership.
`cancel_task_row` commits per task, so one commit cannot cover both; the order makes the failure safe
instead: a cancel that fails leaves the person a member and a retry finishes, where the reverse order
would leave someone out with their work still spending. Cancelling is idempotent.

The actor on each cancel is the person who did the removing (or the leaver): `attribute=True`, because
this is a human's decision and the task's record should name who ended it, as the user cancel door
does; only a system cancel that borrows an owner's authority goes anonymous.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.workspace import WorkspaceMember
from prismoid.operations import steering, workspaces


async def _stop_work(
    session: AsyncSession, actor: Actor, person_id: uuid.UUID, workspace_id: uuid.UUID
) -> None:
    """Commits per task (inside `cancel_task_row`), so it ends with no lock held. Task rows are locked
    one at a time and no Machine row is touched, so the Machine-then-task order cannot invert."""
    for task in await WorkspaceMember.live_tasks(session, workspace_id, person_id):
        await steering.cancel_task_row(session, task, actor)  # re-reads the row under its lock


async def remove_member(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    await workspaces.authorize_removal(session, actor, workspace_id, target_id)
    await _stop_work(session, actor, target_id, workspace_id)
    await workspaces.remove_member(session, actor, workspace_id, target_id)


async def leave_workspace(session: AsyncSession, actor: Actor, workspace_id: uuid.UUID) -> None:
    await workspaces.authorize_leave(session, actor, workspace_id)
    await _stop_work(session, actor, actor.id, workspace_id)
    await workspaces.leave_workspace(session, actor, workspace_id)
