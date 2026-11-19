# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The context ledger - scoped memories a person reads, adds, edits and retires.

Automation (memory_extract) writes private rows only; every shared row was typed by a person
and says who. Removal is `retire` - a verb route like `cancel`, never a DELETE: the ledger keeps
its history.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from prismoid.api.deps import ActorDep, PageDep, SessionDep
from prismoid.api.serializers.common import PageOut
from prismoid.api.serializers.memories import (
    MemoryCreateIn,
    MemoryDetailOut,
    MemoryOut,
    MemoryScopeName,
    MemoryUpdateIn,
    memory_detail_out,
    memory_out,
)
from prismoid.db.models.memory import MemoryKind, MemoryScope, MemoryStatus, MemoryUse
from prismoid.operations import memory as ops


async def create_memory(body: MemoryCreateIn, actor: ActorDep, session: SessionDep) -> MemoryOut:
    row = await ops.create(
        session,
        actor,
        scope=MemoryScope(body.scope),
        kind=MemoryKind(body.kind),
        text=body.text,
        project_id=body.project_id,
        skill_name=body.skill_name,
        bot_name=body.bot_name,
    )
    return memory_out(row)


async def list_memories(
    actor: ActorDep,
    session: SessionDep,
    page: PageDep,
    scope: MemoryScopeName = "user",
    project_id: UUID | None = None,
    skill: str | None = None,
    bot: str | None = None,
    q: str | None = None,
    status: Literal["active", "superseded", "retired", "all"] = "active",
) -> PageOut[MemoryOut]:
    rows = await ops.list_memories(
        session,
        actor,
        scope=MemoryScope(scope),
        project_id=project_id,
        skill_name=skill,
        bot_name=bot,
        q=q,
        status=None if status == "all" else MemoryStatus(status),
        limit=page.limit,
        cursor=page.cursor,
    )
    next_cursor = rows[-1].cursor() if len(rows) == page.limit else None
    return PageOut(items=[memory_out(m) for m in rows], next_cursor=next_cursor)


async def get_memory(memory_id: UUID, actor: ActorDep, session: SessionDep) -> MemoryDetailOut:
    row = await ops.get(session, actor, memory_id)
    # A skill note is readable platform-wide, but the tasks that used it are not - listing
    # them would leak strangers' task ids. User and project rows only reach eyes that can
    # already see those tasks.
    uses = [] if row.scope == MemoryScope.SKILL.value else await MemoryUse.tasks_for(session, memory_id)
    return memory_detail_out(row, [u.task_id for u in uses])


async def update_memory(
    memory_id: UUID, body: MemoryUpdateIn, actor: ActorDep, session: SessionDep
) -> MemoryOut:
    row = await ops.update(
        session,
        actor,
        memory_id,
        text=body.text,
        kind=MemoryKind(body.kind) if body.kind else None,
    )
    return memory_out(row)


async def retire_memory(memory_id: UUID, actor: ActorDep, session: SessionDep) -> MemoryOut:
    return memory_out(await ops.retire(session, actor, memory_id))
