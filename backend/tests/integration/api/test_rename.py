# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Rename is labelling (step 3 of the nav restructure): the creator names their own work in
any state, the goal never changes, and the record keeps the change as `task.renamed`."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from prismoid.db.models.event import TaskEvent
from prismoid.db.session import session_scope
from prismoid.errors import NotFound
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_creator_renames_and_the_record_keeps_it() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Rae")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="write the launch memo")
        assert task.title is None

        renamed = await steering.update_meta(session, actor, task.id, title="Launch memo")
        assert renamed.title == "Launch memo"
        assert renamed.goal == "write the launch memo", "the goal is immutable"

        rows = await session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.seq)
        )
        types = [r.payload["type"] for r in rows]
        assert "task.renamed" in types, "a label the record never saw never happened"

        again = await steering.update_meta(session, actor, task.id, title="Launch memo")
        assert again.title == "Launch memo"
        assert types.count("task.renamed") == 1, "a same-name rename is a no-op, not an event"


async def test_a_stranger_cannot_even_see_it() -> None:
    async with session_scope() as session:
        creator, _ = await member(session, "Rae")
        stranger, _ = await member(session, "Sol")
        await session.flush()

        task = await task_ops.create_task(session, creator, goal="private work")
        with pytest.raises(NotFound):
            await steering.update_meta(session, stranger, task.id, title="mine now")


async def test_pin_is_triage_not_status() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Rae")
        await session.flush()

        task = await task_ops.create_task(session, actor, goal="pin me")
        pinned = await steering.update_meta(session, actor, task.id, pinned=True)
        assert pinned.pinned_at is not None
        assert pinned.status == "created", "a pin never touches the machine's state"

        unpinned = await steering.update_meta(session, actor, task.id, pinned=False)
        assert unpinned.pinned_at is None

        rows = await session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.seq)
        )
        types = [r.payload["type"] for r in rows]
        assert "task.renamed" not in types, "pins are view preference - not part of the record"
