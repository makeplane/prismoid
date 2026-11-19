# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Capture - soft intake. A thought, saved now, turned into work later.

**Capture runs nothing**. It is the opposite of `do`: a place to put an idea without deciding,
this second, whether it is worth a task. `add` writes a row and stops; `promote` is the moment it
becomes a Task - and only then does any machine start.

The two states round-trip through `promoted_task_id`: an open capture has none, a promoted one points
at the task it became, and the task's `parent_task_id`… does *not* point back, because a capture is
not a task and a promotion is not a follow-up. The link is one-directional and that is the honest
shape: the capture remembers what it turned into; the task does not need to know it was once a note.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.capture import Capture, CaptureStatus
from prismoid.db.models.project import Project, Role
from prismoid.db.models.task import Task
from prismoid.errors import NotFound, ValidationFailed


async def add(
    session: AsyncSession,
    actor: Actor,
    *,
    text: str,
    url: str | None = None,
    project_id: uuid.UUID | None = None,
) -> Capture:
    """Jot something down. Into a project needs the **editor** role there - a capture in a shared folder
    is shared work-in-waiting, and a viewer who could fill the team's project with notes is a viewer who
    is really an editor."""
    if project_id is not None:
        await Project.get_for(session, project_id, actor, min_role=Role.EDITOR)

    capture = Capture(id=uuid.uuid4(), actor_id=actor.id, text=text, url=url, project_id=project_id)
    session.add(capture)
    await session.commit()
    logfire.info("capture {c} added", c=str(capture.id))
    return capture


async def list_captures(session: AsyncSession, actor: Actor) -> list[Capture]:
    """The open captures this actor made - the inbox of un-started ideas."""
    return await Capture.open_for(session, actor)


async def promote(
    session: AsyncSession, actor: Actor, capture_id: uuid.UUID, *, goal: str | None = None
) -> Task:
    """Turn a capture into a Task - the first moment any machine runs.

    The task inherits the capture's project (so a captured idea in the team's folder becomes the team's
    task) and, by default, its text as the goal - but `goal` may sharpen it, because the note you
    scribbled ("chase the Q3 numbers") is rarely the acceptance criteria you want to hold work to.

    Idempotent by the link: a capture already promoted returns *its* task rather than making a second.
    A double-click, a retried request, an over-eager script - none of them should spawn a duplicate
    task, and `promoted_task_id` is exactly the guard that makes sure they do not.
    """
    from prismoid.operations import tasks as task_ops

    capture = await Capture.get_for(session, capture_id, actor)
    if capture is None:
        raise NotFound("no such capture")

    if capture.promoted_task_id is not None:
        existing = await session.get(Task, capture.promoted_task_id)
        if existing is not None:
            return existing  # already promoted; the link is the idempotency key

    if capture.project_id is not None:
        await Project.get_for(session, capture.project_id, actor, min_role=Role.EDITOR)

    text = (goal or capture.text).strip()
    if not text:
        raise ValidationFailed("nothing to promote - the capture is empty", fields={"goal": "required"})

    task = await task_ops.create_task(session, actor, goal=text, project_id=capture.project_id)

    capture.promoted_task_id = task.id
    capture.status = CaptureStatus.PROMOTED.value
    await session.commit()
    logfire.info("capture {c} promoted → task {t}", c=str(capture.id), t=str(task.id))
    return task
