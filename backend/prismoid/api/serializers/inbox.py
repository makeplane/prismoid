# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every inbox shape, one file. `*Out` = response."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from prismoid.db.models.inbox import InboxItem


class InboxItemOut(BaseModel):
    """One notification.

    `task_id` is the point of the whole thing. **An inbox item you cannot act on from is just noise
    with a timestamp** - so every item that is *about* something carries the id of that thing,
    and the next command is always obvious: `prismoid tasks show $TASK_ID`.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    title: str
    body: str
    task_id: UUID | None
    # The owning task's display label (its minted/user title, else the goal) - the list
    # names rows by the TASK; `title` above is the event ("Delivered: …"). None when the
    # item has no task (a shared project, say) or on single-item responses (mark_read).
    task_title: str | None
    gate_id: UUID | None
    project_id: UUID | None
    payload: dict[str, Any]
    read: bool
    created_at: dt.datetime


class InboxOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[InboxItemOut]
    unread: int


class InboxTaskReadOut(BaseModel):
    """What opening a task did to the caller's inbox: how many of their items about it it read,
    and how many of theirs stay unread anywhere (the badge). A client refreshes only when
    `marked` moved."""

    model_config = ConfigDict(frozen=True)

    marked: int
    unread: int


def inbox_item_out(item: InboxItem, task_title: str | None = None) -> InboxItemOut:
    return InboxItemOut(
        id=item.id,
        kind=item.kind,
        title=item.title,
        body=item.body,
        task_id=item.task_id,
        task_title=task_title,
        gate_id=item.gate_id,
        project_id=item.project_id,
        payload=item.payload,
        read=not item.is_unread(),
        created_at=item.created_at,
    )
