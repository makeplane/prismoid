# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Memories - request and response bodies. The ledger speaks in users: `created_by_id` is
the author's user id, and a private row's owner column never appears (a user-scope row you can
see is by definition yours)."""

from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.memory import TEXT_MAX, Memory

MemoryScopeName = Literal["user", "project", "skill", "bot"]
MemoryKindName = Literal["instruction", "fact", "preference", "note"]
MemoryStatusName = Literal["active", "superseded", "retired"]


class MemoryCreateIn(BaseModel):
    scope: MemoryScopeName = "user"
    kind: MemoryKindName = "note"
    text: str = Field(min_length=1, max_length=TEXT_MAX)
    project_id: UUID | None = None  # required when scope=project; the carrier for a project bot's row
    skill_name: str | None = Field(default=None, min_length=1, max_length=80)  # required when scope=skill
    bot_name: str | None = Field(default=None, min_length=1, max_length=64)  # required when scope=bot


class MemoryUpdateIn(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=TEXT_MAX)
    kind: MemoryKindName | None = None


class MemoryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    scope: str
    kind: str
    text: str
    status: str
    project_id: UUID | None
    skill_name: str | None
    bot_name: str | None
    source_task_id: UUID | None  # the task that minted it - None when a person typed it
    created_by_id: UUID  # the author, as a user id
    use_count: int
    last_used_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class MemoryDetailOut(MemoryOut):
    """One memory with its blast radius: the tasks it most recently shaped, newest first.
    The list view stays light; the detail answers "where has this been used"."""

    used_by: list[UUID] = []


def memory_detail_out(m: Memory, used_by: list[UUID]) -> MemoryDetailOut:
    return MemoryDetailOut(**memory_out(m).model_dump(), used_by=used_by)


def memory_out(m: Memory) -> MemoryOut:
    return MemoryOut(
        id=m.id,
        scope=m.scope,
        kind=m.kind,
        text=m.text,
        status=m.status,
        project_id=m.project_id,
        skill_name=m.skill_name,
        bot_name=m.bot_name,
        source_task_id=m.source_task_id,
        created_by_id=m.created_by_actor_id,
        use_count=m.use_count,
        last_used_at=m.last_used_at,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )
