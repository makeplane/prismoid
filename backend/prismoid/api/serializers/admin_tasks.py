# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the operator's task console shapes (admin surface). `*In` = request, `*Out` = response.

The row is METADATA + the owner + the diagnosis: goal first-line, status, spend, stuck reason.
Never step prompts, never transcripts - the console's peek reads the task's own durable events
(`GET /v1/admin/tasks/{id}/events`), which are the SAME wire payloads the owner's stream carries,
so nothing here invents a second disclosure surface.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminTaskOwnerOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    name: str


class AdminTaskOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    goal_line: str  # the first line only - the list is a console, not a transcript
    title: str | None
    status: str
    owner: AdminTaskOwnerOut | None  # None = a non-human or erased creator
    project_id: UUID | None
    turn: int
    created_at: dt.datetime
    updated_at: dt.datetime
    last_event_at: dt.datetime | None
    spend_usd: Decimal
    stuck_reason: str | None
    held: bool


class AdminCancelIn(BaseModel):
    """An admin cancel takes a reason - it lands in the audit row the owner can be answered with."""

    reason: str = Field(min_length=1, max_length=500)


class AdminEventsOut(BaseModel):
    """One page of the task's durable record - raw wire payloads, exactly as the stream framed
    them, so the web's replay reducer folds them unchanged. `next_since` is the last seq, or
    null when the page came back short (the history is drained)."""

    model_config = ConfigDict(frozen=True)

    events: list[dict[str, Any]]
    next_since: int | None


def admin_task_out(row: dict[str, Any]) -> AdminTaskOut:
    task = row["task"]
    owner = row["owner"]
    return AdminTaskOut(
        id=task.id,
        goal_line=(task.goal or "").splitlines()[0][:200] if task.goal else "",
        title=task.title,
        status=task.status,
        owner=None
        if owner is None
        else AdminTaskOwnerOut(id=owner.actor_id, email=owner.email, name=owner.name),
        project_id=task.project_id,
        turn=task.turn,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_event_at=row["last_event_at"],
        spend_usd=Decimal(row["spend_usd"] or 0),
        stuck_reason=row["stuck_reason"],
        held=task.held_at is not None,
    )
