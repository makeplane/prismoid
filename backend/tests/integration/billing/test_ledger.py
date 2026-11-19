# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ledger (the ledger law: reconcile to the cent).

Money is the one thing in the product that cannot be *approximately* right.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project
from prismoid.db.models.usage import ProjectUsageDaily, UsageEvent
from prismoid.db.session import session_scope
from prismoid.operations import tasks as task_ops
from prismoid.operations import usage as ops
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit, UsageRecord
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Just a session. Cleanup is autouse in `tests/conftest.py`, scoped to what this test made -
    a suite that nukes `actors` also nukes the developer's login."""
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


def a_record(
    *,
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    cost: str = "0.00000344",
    family: str = "llama",
) -> UsageRecord:
    return UsageRecord(
        task_id=TaskId(task_id) if task_id else None,
        project_id=ProjectId(project_id) if project_id else None,
        provider="local",
        model="llama3.2:3b",
        family=Family(family),
        units={Unit.INPUT_TOKEN: 37, Unit.OUTPUT_TOKEN: 2, Unit.REQUEST: 1},
        cost_usd=Decimal(cost),
        price_version=1,
        at=utcnow(),
    )


async def test_the_ledger_keeps_every_decimal_it_was_given(db: AsyncSession, actor: Actor) -> None:
    """NUMERIC(20,12), not (14,6).

    Six places sounds generous until you price a real call: $0.00000344 becomes **0.000003** - the
    row is wrong by 9% before it is written, and a million calls rounds away half a dollar. The
    ledger must reconcile *to the cent*, not to the nearest fifty.
    """
    task = await task_ops.create_task(db, actor, goal="g")
    await ops.record(db, a_record(task_id=task.id, cost="0.00000344"))
    await db.commit()

    rows = await UsageEvent.for_task(db, task.id)
    assert rows[0].cost_usd == Decimal("0.00000344")


async def test_a_call_lands_a_row_a_rollup_and_an_event(db: AsyncSession, actor: Actor) -> None:
    project = Project(id=uuid.uuid4(), name="Pricing", owner_actor_id=actor.id)
    db.add(project)
    await db.flush()

    task = await task_ops.create_task(db, actor, goal="g", project_id=project.id)
    await ops.record(db, a_record(task_id=task.id, project_id=project.id))
    await db.commit()

    assert len(await UsageEvent.for_task(db, task.id)) == 1

    # The rollup is bumped in the SAME transaction - two transactions would let a crash leave the
    # graph disagreeing with the truth, and the graph is what people look at.
    total = await db.scalar(
        text("select cost_usd from project_usage_daily where kind = 'total' and project_id = :s"),
        {"s": project.id},
    )
    assert total == Decimal("0.00000344")

    # And the money is on the stream: a user sees it spent, not discovers it on an invoice.
    kinds = list(await db.scalars(text("select type from task_events order by seq")))
    assert "usage.recorded" in kinds


async def test_the_rollup_upsert_does_not_lose_increments_under_concurrency(
    db: AsyncSession, actor: Actor
) -> None:
    """Eight steps metering at once is eight writers to the same (project, day) row.

    `INSERT … ON CONFLICT DO UPDATE SET cost = cost + :cost` is one statement and cannot lose an
    increment. A read-modify-write would - and a lost increment is money that vanishes from the
    graph but not from the bill.
    """
    import asyncio

    project = Project(id=uuid.uuid4(), name="Pricing", owner_actor_id=actor.id)
    db.add(project)
    await db.commit()

    day = utcnow().date()

    async def bump() -> None:
        async with session_scope() as s:
            await ProjectUsageDaily.bump(
                s, project_id=project.id, day=day, kind="total", cost=Decimal("0.001")
            )
            await s.commit()

    await asyncio.gather(*[bump() for _ in range(8)])

    # Scoped to THIS test's project. The first version asked for "the row where kind = 'total'" -
    # which is global state, and a test that asserts on global state is a test that passes alone
    # and fails in a suite.
    total = await db.scalar(
        text("select cost_usd from project_usage_daily where kind = 'total' and project_id = :s"),
        {"s": project.id},
    )
    assert total == Decimal("0.008"), "the rollup lost an increment under concurrency"


async def test_two_writers_on_one_project_task_never_deadlock(db: AsyncSession, actor: Actor) -> None:
    """One sibling's transaction already held
    the task row - a step event earlier in it - when it recorded a call, while another sibling's
    `record` had bumped the Project's rollup first and then waited for the task row. Postgres killed
    one, and the wave failed the task. Every writer takes the task row first."""
    import asyncio

    from prismoid.db.models.task import Task

    project = Project(id=uuid.uuid4(), name="Pricing", owner_actor_id=actor.id)
    db.add(project)
    await db.flush()
    task = await task_ops.create_task(db, actor, goal="g", project_id=project.id)
    await db.commit()
    holding = asyncio.Event()

    async def step_with_an_event_first() -> None:
        async with session_scope() as s:
            row = await s.get(Task, task.id)
            assert row is not None
            await row.next_seq(s)  # the task row, held until commit
            holding.set()
            await asyncio.sleep(0.5)  # the sibling's `record` takes whatever it takes first
            await ops.record(s, a_record(task_id=task.id, project_id=project.id))
            await s.commit()

    async def sibling_recording_a_call() -> None:
        await holding.wait()
        async with session_scope() as s:
            await ops.record(s, a_record(task_id=task.id, project_id=project.id))
            await s.commit()

    await asyncio.wait_for(asyncio.gather(step_with_an_event_first(), sibling_recording_a_call()), 20)
    assert len(await UsageEvent.for_task(db, task.id)) == 2


async def test_a_task_move_never_rewrites_the_ledger(db: AsyncSession, actor: Actor) -> None:
    """The ledger law. Spend before the move stays where it happened.

    Repricing that rewrites history is forgery; re-attributing that rewrites history is the same
    crime with a nicer name.
    """
    old = Project(id=uuid.uuid4(), name="Old", owner_actor_id=actor.id)
    new = Project(id=uuid.uuid4(), name="New", owner_actor_id=actor.id)
    db.add_all([old, new])
    await db.flush()

    task = await task_ops.create_task(db, actor, goal="g", project_id=old.id)
    await ops.record(db, a_record(task_id=task.id, project_id=old.id))
    await db.commit()

    task.project_id = new.id  # the move
    await db.commit()

    rows = await UsageEvent.for_task(db, task.id)
    assert rows[0].project_id == old.id, "the move rewrote history"
