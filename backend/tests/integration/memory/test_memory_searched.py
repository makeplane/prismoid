# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A note the Lead's `memory_search` hands back shaped the turn as surely as a recalled one, so it is on the
record the same way: a `memory_uses` row, the decay bump and a `context.assembled` naming it (id and preview,
never the query), which only the task's creator reads in full."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import Memory, MemoryKind, MemoryScope, MemoryUse
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import context as context_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import ContextAssembled, redact_for
from tests.support.cast import member

pytestmark = pytest.mark.integration

TABLES = "Ada wants every comparison as a table."
QUERY = "how does Ada like comparisons"


async def _searched(s: AsyncSession, task_id: uuid.UUID) -> list[ContextAssembled]:
    events = await TaskEvent.of_types(s, task_id, ("context.assembled",))
    return [e for e in events if isinstance(e, ContextAssembled)]


async def test_a_searched_note_is_recorded_as_used_on_the_turn(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        note = await memory_ops.create(
            s, ada, scope=MemoryScope.USER, kind=MemoryKind.PREFERENCE, text=TABLES
        )
        task = await task_ops.create_task(s, ada, goal="compare three trackers")
        await s.commit()
        task_id, note_id, ada_id = task.id, note.id, ada.id

    search = context_ops.memory_searcher(task)
    assert search is not None and TABLES in await search(QUERY)

    async with row_scope(Task, task_id) as s:
        (event,) = await _searched(s, task_id)
        assert event.memory_ids == [note_id]
        assert [(lane.lane, [i.preview for i in lane.items]) for lane in event.lanes] == [("user", [TABLES])]
        assert QUERY not in event.model_dump_json(), "the query never reaches the record"
        assert [u.task_id for u in await MemoryUse.tasks_for(s, note_id)] == [task_id]
        row = await s.get(Memory, note_id)
        assert row is not None and row.use_count == 1

    assert redact_for(event, viewer_id=ada_id, creator_id=ada_id).memory_ids, (
        "the creator reads what was used"
    )
    hidden = redact_for(event, viewer_id=uuid.uuid4(), creator_id=ada_id)
    assert hidden.memory_ids == [] and not any(lane.items for lane in hidden.lanes), "nobody else does"


async def test_a_search_that_finds_nothing_records_nothing(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="compare three trackers")
        await s.commit()

    search = context_ops.memory_searcher(task)
    assert search is not None and await search("pineapple codeword") == []
    async with row_scope(Task, task.id) as s:
        assert await _searched(s, task.id) == []
