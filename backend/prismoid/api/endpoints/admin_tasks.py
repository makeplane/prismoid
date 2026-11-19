# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the operator's task console: the cross-user list, the audited peek, the kill switch.

These routes deliberately do NOT ride `Task._visible_to` (admin_ops law 1): they are the
separate, audited door. The peek (`get_task`) writes a `task.view` audit row; the cancel runs
the user door's exact machinery (`steering.cancel_task_row`) with the reason on the record.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Query

from prismoid.api.deps import AdminDep, PageDep, SessionDep
from prismoid.api.serializers.admin_tasks import (
    AdminCancelIn,
    AdminEventsOut,
    AdminTaskOut,
    admin_task_out,
)
from prismoid.api.serializers.common import PageOut
from prismoid.operations import admin_ops


async def list_tasks(
    page: PageDep,
    actor: AdminDep,
    session: SessionDep,
    status: Annotated[list[str] | None, Query()] = None,
    project_id: UUID | None = None,
    user_id: UUID | None = None,
    stuck: bool = False,
    q: str | None = None,
) -> PageOut[AdminTaskOut]:
    rows = await admin_ops.list_tasks_admin(
        session,
        statuses=status,
        project_id=project_id,
        creator_id=user_id,
        stuck=stuck,
        q=q,
        limit=page.limit,
        cursor=page.cursor,
    )
    next_cursor = rows[-1]["task"].cursor() if len(rows) == page.limit else None
    return PageOut(items=[admin_task_out(r) for r in rows], next_cursor=next_cursor)


async def get_task(task_id: UUID, actor: AdminDep, session: SessionDep) -> AdminTaskOut:
    return admin_task_out(await admin_ops.get_task_admin(session, actor, task_id))


async def get_task_events(
    task_id: UUID,
    actor: AdminDep,
    session: SessionDep,
    since: int = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> AdminEventsOut:
    events = await admin_ops.events_admin(session, task_id, since=since, limit=limit)
    next_since = events[-1].seq if len(events) == limit else None
    return AdminEventsOut(events=[e.model_dump(mode="json") for e in events], next_since=next_since)


async def cancel_task(
    task_id: UUID, body: AdminCancelIn, actor: AdminDep, session: SessionDep
) -> AdminTaskOut:
    await admin_ops.cancel_admin(session, actor, task_id, reason=body.reason)
    row = await admin_ops.get_task_admin(session, actor, task_id, record_view=False)
    return admin_task_out(row)
