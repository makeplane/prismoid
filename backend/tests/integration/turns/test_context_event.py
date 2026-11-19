# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`assemble()` + `record()` end to end: assemble reads (no locks - planning holds its
transaction across the planner call), then record freezes the pack onto the row, bumps the
ledger's decay signal, lands the spend join, and `context.assembled` tells the whole story.

And the silence case: a task with nothing to remember gets no pack, no file, no event -
context is never padded into ceremony.
"""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import MemoryKind, MemoryScope, MemoryUse
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import context as context_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import projects as project_ops
from prismoid.schemas.events import ContextAssembled
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_assemble_records_everything_in_one_story() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Rememberer")
        await session.flush()
        project = (await project_ops.create(session, actor, name="context-probe")).project
        await project_ops.update(session, actor, project.id, instructions="Cite sources. UK English.")

        kept = await memory_ops.create(
            session,
            actor,
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            text="Prefers concise memos with numbers tables.",
        )
        shared = await memory_ops.create(
            session,
            actor,
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.NOTE,
            text="This team ships UK English.",
            project_id=project.id,
        )

        task = Task(
            id=uuid.uuid4(),
            goal="Write a concise memo on Q3",
            creator_actor_id=actor.id,
            project_id=project.id,
            criteria={},
            inputs=[],
        )
        session.add(task)
        await session.flush()

        pack = await context_ops.assemble(session, task)
        await context_ops.record(session, task, pack)
        await session.commit()

        assert pack is not None
        # The creator's private USER memory never rides a Project task's pack
        assert [lane.lane for lane in pack.lanes] == ["instructions", "project"]
        assert set(pack.memory_ids()) == {shared.id}
        assert task.context_pack is not None and task.context_pack["total_tokens"] == pack.total_tokens

        events = await TaskEvent.replay(session, task.id)
        assembled = [e for e in events if isinstance(e, ContextAssembled)]
        assert len(assembled) == 1, "one pack, one record"
        assert set(assembled[0].memory_ids) == {shared.id}
        assert assembled[0].total_tokens == pack.total_tokens
        assert all(len(i.preview) <= 120 for lane in assembled[0].lanes for i in lane.items)

        await session.refresh(shared)
        assert shared.use_count == 1 and shared.last_used_at is not None, "assembly bumps the decay signal"
        uses = await MemoryUse.tasks_for(session, shared.id)
        assert [u.task_id for u in uses] == [task.id], "the spend join answers 'which tasks used this'"

        await session.refresh(kept)
        assert kept.use_count == 0 and await MemoryUse.tasks_for(session, kept.id) == [], (
            "the fenced-out private memory is neither used nor joined"
        )


async def test_nothing_to_remember_means_no_pack_and_no_event() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Blank")
        await session.flush()
        task = Task(id=uuid.uuid4(), goal="hello", creator_actor_id=actor.id, criteria={}, inputs=[])
        session.add(task)
        await session.flush()

        pack = await context_ops.assemble(session, task)
        assert pack is None
        await context_ops.record(session, task, pack)  # a None pack records nothing
        assert task.context_pack is None
        assert await TaskEvent.replay(session, task.id) == [], "silence leaves no ceremony behind"
