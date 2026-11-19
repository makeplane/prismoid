# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The steers table: idempotent per task, one epoch per steer, forward-only compare-and-set."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from prismoid.db.models.steer import Steer
from prismoid.db.session import session_scope
from prismoid.operations import tasks as task_ops
from prismoid.schemas.steering import SteerState
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _task_and_rows() -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="steer rows", hold=True)
        ids = [uuid.uuid4() for _ in range(3)]
        for i, cid in enumerate(ids, start=2):
            s.add(Steer(task_id=task.id, command_id=cid, turn=1, actor_id=ada.id, text=f"s{i}", epoch=i))
        await s.commit()
        return task.id, ids


async def test_the_reads_follow_epoch_and_state(silent_dispatcher: None) -> None:
    task_id, (a, b, c) = await _task_and_rows()
    async with session_scope() as s:
        assert [r.command_id for r in await Steer.newer_than(s, task_id, turn=1, epoch=2)] == [b, c]
        assert [r.command_id for r in await Steer.seen_by(s, task_id, floor=0, epoch=3)] == [a, b]
        assert [r.command_id for r in await Steer.seen_by(s, task_id, floor=2, epoch=4)] == [b, c]
        moved = await Steer.settle(s, task_id, [b], to=SteerState.RETRACTED)
        assert [(r.command_id, r.state) for r in moved] == [(b, SteerState.RETRACTED.value)]
        assert moved[0].settled_at is not None
        assert [r.command_id for r in await Steer.newer_than(s, task_id, turn=1, epoch=2)] == [c]
        assert await Steer.settle(s, task_id, [b], to=SteerState.APPLIED) == [], (
            "a settled row keeps its first answer"
        )
        assert [r.command_id for r in await Steer.unapplied(s, task_id)] == [a, c]
        assert [r.command_id for r in await Steer.unapplied(s, task_id, turn=1)] == [a, c]
        assert [r.command_id for r in await Steer.since(s, task_id, 2)] == [b, c]
        found = await Steer.by_command(s, task_id, c)
        assert found is not None and found.epoch == 4
        at = await Steer.at_epoch(s, task_id, 2)
        assert at is not None and at.command_id == a
        with pytest.raises(ValueError):
            await Steer.settle(s, task_id, [a], to=SteerState.ACCEPTED)


async def test_a_command_id_and_an_epoch_are_unique_per_task(silent_dispatcher: None) -> None:
    task_id, (a, _, _) = await _task_and_rows()
    async with session_scope() as s:
        s.add(Steer(task_id=task_id, command_id=a, turn=1, text="again", epoch=9))
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()
        s.add(Steer(task_id=task_id, command_id=uuid.uuid4(), turn=1, text="again", epoch=2))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_settle_leaves_a_row_another_writer_settled_alone(silent_dispatcher: None) -> None:
    task_id, (a, b, _) = await _task_and_rows()
    async with session_scope() as first:
        loaded = {r.command_id: r for r in await Steer.unapplied(first, task_id)}  # both in this session
        async with session_scope() as second:
            assert [
                r.command_id for r in await Steer.settle(second, task_id, [b], to=SteerState.REJECTED)
            ] == [b]
            await second.commit()
        moved = await Steer.settle(first, task_id, [a, b], to=SteerState.APPLIED)
        assert [r.command_id for r in moved] == [a]
        assert loaded[b].state != SteerState.APPLIED.value, "the loaded copy must not read 'applied'"
