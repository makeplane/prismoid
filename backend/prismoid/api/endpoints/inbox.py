# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Inbox - notifications out. Never sends outbound, never runs anything.

Thin: auth → one fat-model or operation call → serialize. **No transaction here** - the
first version of `mark_read` committed, `just audit` refused it, and it was right to: a codebase
where views commit "when it is simple enough" is a codebase whose boundary is a judgement call, and
the next one will not be simple.

**The actor is not a filter these views remember to apply.** It is the first clause of the only
query `InboxItem` exposes, so there is no path by which one person's inbox reaches another - not
even by forgetting a `where`.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.inbox import InboxItemOut, InboxOut, InboxTaskReadOut, inbox_item_out
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.task import Task
from prismoid.operations import inbox as ops


async def list_inbox(actor: ActorDep, session: SessionDep, unread: bool = False) -> InboxOut:
    items = await InboxItem.list_for(session, actor, unread_only=unread)
    labels = await Task.labels_for(session, {i.task_id for i in items if i.task_id})
    return InboxOut(
        items=[inbox_item_out(i, task_title=labels.get(i.task_id) if i.task_id else None) for i in items],
        unread=await InboxItem.unread_count(session, actor),
    )


async def mark_read(item_id: UUID, actor: ActorDep, session: SessionDep) -> InboxItemOut:
    return inbox_item_out(await ops.mark_read(session, actor, item_id))


async def mark_all_read(actor: ActorDep, session: SessionDep) -> InboxOut:
    await ops.mark_all_read(session, actor)
    return InboxOut(items=[], unread=0)


async def mark_task_read(task_id: UUID, actor: ActorDep, session: SessionDep) -> InboxTaskReadOut:
    """The task is on the caller's screen: their own items about it read, nobody else's. A task
    they cannot see is a 404."""
    marked, unread = await ops.mark_task_read(session, actor, task_id)
    return InboxTaskReadOut(marked=marked, unread=unread)
