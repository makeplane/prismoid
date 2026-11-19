# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A routine fire that errors knocks the owner's inbox."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.routine import Routine
from prismoid.db.session import session_scope
from prismoid.errors import ValidationFailed
from prismoid.operations import routines as ops
from prismoid.operations import tasks as tasks_ops
from prismoid.schemas.routines import RunDecision, RunSource, occurrence_for_clock
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _human(session: AsyncSession) -> Actor:
    return (await member(session, "Rou Tine"))[0]


async def _routine(session: AsyncSession, actor: Actor) -> Routine:
    schedule = {"cron": "0 9 * * 1-5", "every_s": None, "at": None, "timezone": "UTC"}
    return await ops.create_routine(
        session, actor, name="standup", instruction="post the standup summary", schedule=schedule
    )  # pyright: ignore[reportArgumentType]


def _past(minutes: int) -> dt.datetime:
    return utcnow() - dt.timedelta(minutes=minutes)


async def _fire_twice(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> tuple[list[str], int, int]:
    async def boom(*a: object, **k: object) -> object:
        raise exc

    monkeypatch.setattr(tasks_ops, "create_task", boom)
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        await session.commit()
        me_id, routine_id = me.id, routine.id
        decisions = []
        for minutes in (10, 5):
            occ = occurrence_for_clock(_past(minutes))
            r = await ops.execute_fire(session, routine_id, occurrence=occ, source=RunSource.MANUAL)
            decisions.append(r.decision.value)
        await session.refresh(me)
        assert me.id == me_id
        items = await InboxItem.list_for(session, me)
        fresh = await session.get(Routine, routine_id, populate_existing=True)
        assert fresh is not None
        return decisions, len(items), fresh.consecutive_failures


async def test_a_crashed_fire_knocks_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions, items, failures = await _fire_twice(monkeypatch, RuntimeError("poison"))
    assert decisions == [RunDecision.ERROR.value] * 2
    assert items == 1, f"owner inbox items after two crashed fires: {items} (consecutive_failures={failures})"


async def test_a_poison_fire_walks_the_ladder_and_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: object, **k: object) -> object:
        raise RuntimeError("poison")

    monkeypatch.setattr(tasks_ops, "create_task", boom)
    async with session_scope() as session:
        me = await _human(session)
        routine = await _routine(session, me)
        routine.min_interval_s = 0
        await session.commit()
        routine_id, me_id = routine.id, me.id
        for minutes in (15, 10, 5):
            occ = occurrence_for_clock(_past(minutes))
            await ops.execute_fire(session, routine_id, occurrence=occ, source=RunSource.MANUAL)
        await session.refresh(me)
        assert me.id == me_id
        kinds = sorted(i.kind for i in await InboxItem.list_for(session, me))
        fresh = await session.get(Routine, routine_id, populate_existing=True)
        assert fresh is not None and fresh.status == "paused", "a poison fire pauses, never errors forever"
        assert kinds == ["routine_paused", "routine_run"], kinds


async def test_a_refused_mint_knocks_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions, items, failures = await _fire_twice(monkeypatch, ValidationFailed("dead pin"))
    assert decisions == [RunDecision.ERROR.value] * 2
    assert items == 1, f"owner inbox items after two refused mints: {items} (consecutive_failures={failures})"
