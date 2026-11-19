# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ghost sweep's detection invariant (promoted by ADR-009).

Operations enqueue AFTER commit. That leaves exactly one ghost: a crash in the window
between COMMIT and enqueue strands a `created` task no worker will ever pick up - the API said
202, the row is real, nothing runs. The sweeper's contract has two halves: (a) find exactly
those tasks - old enough, still `created` - and (b) re-enqueue under the deterministic job id,
which DBOS dedupes, so sweeping a task that IS running is a no-op rather than a second run.
(b) is DBOS's documented queue semantics; (a) and the id determinism are ours, pinned here.

The tests drive the *cutoff* rather than backdating rows: same predicate, no mutated
timestamps - and the conftest's time-scoped cleanup keeps working. `tools/make_ghost.py`
manufactures the real backdated state against a live stack.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import execution
from prismoid.operations import tasks as task_ops
from prismoid.operations.dispatch import task_job_key
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration

# Mirrors jobs/sweeper.py STALE_AFTER (60 s). Not imported: pulling jobs/ into a process that
# never launches DBOS would register scheduled workflows with nothing to run them.
STALE = dt.timedelta(seconds=60)

# A cutoff safely in the future: anything already committed is "older than" it. Wide enough to
# shrug off host↔container clock drift.
SOON = dt.timedelta(seconds=30)


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


async def test_a_committed_never_enqueued_task_is_detected(db: AsyncSession, actor: Actor) -> None:
    """No dispatcher is wired in a test process (NullDispatcher swallows the enqueue) - which IS
    the crash window, the same simulation `make_ghost.py` runs against a live stack."""
    task = await task_ops.create_task(db, actor, goal="a ghost: committed, never enqueued")

    stale = {t.id for t in await Task.stale_created(db, utcnow() + SOON)}
    assert task.id in stale, "the one failure a user cannot see or retry went undetected"


async def test_a_fresh_task_is_left_alone(db: AsyncSession, actor: Actor) -> None:
    """Sixty seconds of grace - the sweeper's own cutoff: the API's enqueue-after-commit needs a
    moment. Sweeping eagerly would race the happy path (harmless via dedupe, but noise is how
    real alarms die)."""
    task = await task_ops.create_task(db, actor, goal="just born")

    stale = {t.id for t in await Task.stale_created(db, utcnow() - STALE)}
    assert task.id not in stale


async def test_a_task_that_left_created_is_never_swept(db: AsyncSession, actor: Actor) -> None:
    """Old but past `created` is not a ghost - a task that advanced was enqueued. Even a cutoff
    that catches everything by age must not catch it."""
    task = await task_ops.create_task(db, actor, goal="already picked up")
    await execution.advance(db, task.id, TaskStatus.PLANNING)
    await db.commit()

    stale = {t.id for t in await Task.stale_created(db, utcnow() + SOON)}
    assert task.id not in stale


def test_the_job_id_is_deterministic() -> None:
    """`task-{id}`: ghost-freedom rests on the job id being a pure function of the task, so a
    sweeper's re-enqueue collides with the original instead of running beside it."""
    task_id = uuid.uuid4()
    assert task_job_key(task_id) == task_job_key(task_id)
    assert str(task_id) in task_job_key(task_id)


async def test_a_quiet_planning_turn_is_detected(db: AsyncSession, actor: Actor) -> None:
    """The continuation's ghost: `continue_task` has the same commit→enqueue crash
    window `create_task` has, and its task sits in `planning`. Same cure, per-turn job id -
    re-enqueueing a LIVE planning turn dedupes to a no-op, so detection can be generous."""
    task = await task_ops.create_task(db, actor, goal="a thread about to continue")
    await execution.advance(db, task.id, TaskStatus.PLANNING)
    await db.commit()

    stuck = {t.id for t in await Task.stale_planning(db, utcnow() + SOON)}
    assert task.id in stuck, "a planning turn nothing is moving went undetected"


async def test_an_actively_planning_turn_is_left_alone(db: AsyncSession, actor: Actor) -> None:
    """`updated_at` refreshes on every event append (`next_seq` is an UPDATE on the row), so a
    task actually writing its plan never looks stale inside the horizon."""
    task = await task_ops.create_task(db, actor, goal="planning right now")
    await execution.advance(db, task.id, TaskStatus.PLANNING)
    await db.commit()

    stuck = {t.id for t in await Task.stale_planning(db, utcnow() - STALE)}
    assert task.id not in stuck


async def test_only_planning_rows_are_swept_as_stuck_turns(db: AsyncSession, actor: Actor) -> None:
    created = await task_ops.create_task(db, actor, goal="still created")
    running = await task_ops.create_task(db, actor, goal="already running")
    await execution.advance(db, running.id, TaskStatus.PLANNING)
    await execution.advance(db, running.id, TaskStatus.RUNNING)
    await db.commit()

    stuck = {t.id for t in await Task.stale_planning(db, utcnow() + SOON)}
    assert created.id not in stuck and running.id not in stuck


def test_the_turn_job_id_is_deterministic_and_turn_one_is_unchanged() -> None:
    """`task-{id}` for turn 1 - every pre-turns workflow, sweep and dedupe keeps working -
    and `task-{id}-t{n}` after: a completed DBOS workflow id never re-executes, so each turn
    is its own durable job and the sweeper's rescue collides with the right one."""
    from prismoid.operations.dispatch import task_turn_job_key

    task_id = uuid.uuid4()
    assert task_turn_job_key(task_id, 1) == task_job_key(task_id)
    assert task_turn_job_key(task_id, 3) == f"task-{task_id}-t3"
    assert task_turn_job_key(task_id, 3) == task_turn_job_key(task_id, 3)
