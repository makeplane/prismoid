# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The flight recorder: append, replay, fan out.

The properties here are the ones the whole streaming design rests on. If `?since=` is not an
*exact* cursor, then `Last-Event-ID` is not either, and the browser's free reconnect quietly
starts losing events - which is the failure mode the id rule exists to prevent.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.hub import Poison, hub
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import NOTIFY_MAX_BYTES, TaskEvent
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import tasks as ops
from prismoid.schemas.events import (
    TaskStatusChanged,
    is_durable,
)
from prismoid.schemas.ids import TaskId
from prismoid.schemas.plan import PlanStepView, PlanView, StepKind, StepState, TaskStatus
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


async def a_task(db: AsyncSession, actor: Actor) -> Task:
    return await ops.create_task(db, actor, goal="write a memo")


async def bump(db: AsyncSession, task: Task, status: TaskStatus) -> int:
    seq = await task.next_seq(db)
    await TaskEvent.append(
        db,
        TaskStatusChanged(seq=seq, task_id=TaskId(task.id), at=utcnow(), status=status),
    )
    await db.commit()
    return seq


async def test_create_task_appends_task_created_at_seq_1(db: AsyncSession, actor: Actor) -> None:
    task = await a_task(db, actor)
    events = await TaskEvent.replay(db, task.id, since=0)
    assert [e.seq for e in events] == [1]
    assert events[0].type == "task.created"


async def test_the_sequence_is_dense_and_gap_free(db: AsyncSession, actor: Actor) -> None:
    """Dense is what makes `?since=` exact. A gap would make it a guess."""
    task = await a_task(db, actor)
    await bump(db, task, TaskStatus.PLANNING)
    await bump(db, task, TaskStatus.RUNNING)

    events = await TaskEvent.replay(db, task.id, since=0)
    assert [e.seq for e in events] == [1, 2, 3]


async def test_since_is_an_exact_cursor(db: AsyncSession, actor: Actor) -> None:
    """This IS `Last-Event-ID`. Nothing redelivered, nothing skipped."""
    task = await a_task(db, actor)
    await bump(db, task, TaskStatus.PLANNING)
    await bump(db, task, TaskStatus.RUNNING)

    assert [e.seq for e in await TaskEvent.replay(db, task.id, since=0)] == [1, 2, 3]
    assert [e.seq for e in await TaskEvent.replay(db, task.id, since=1)] == [2, 3]
    assert [e.seq for e in await TaskEvent.replay(db, task.id, since=3)] == []


async def test_two_writers_cannot_claim_the_same_seq(db: AsyncSession, actor: Actor) -> None:
    """`next_seq` takes a row lock. A `max(seq)+1` subquery would race here and lose an event
    under exactly the concurrency the frontier fan-out creates."""
    task = await a_task(db, actor)

    async def claim() -> int:
        async with session_scope() as s:
            t = await s.get(Task, task.id)
            assert t is not None
            seq = await t.next_seq(s)
            await s.commit()
            return seq

    claimed = await asyncio.gather(*[claim() for _ in range(8)])
    assert len(set(claimed)) == 8, "two writers claimed the same sequence number"


async def test_a_duplicate_seq_is_refused_by_the_database(db: AsyncSession, actor: Actor) -> None:
    """`(task_id, seq)` is a PRIMARY KEY. A duplicate is refused by Postgres - not by
    a code review, and not by a comment. That refusal is the other half of what makes `?since=` an
    exact cursor: dense (the test above) and unique (this one)."""
    task = await a_task(db, actor)
    seq = await bump(db, task, TaskStatus.PLANNING)

    with pytest.raises(IntegrityError):
        await TaskEvent.append(
            db,
            TaskStatusChanged(seq=seq, task_id=TaskId(task.id), at=utcnow(), status=TaskStatus.RUNNING),
        )
        await db.commit()


async def test_replay_returns_typed_events_not_rows(db: AsyncSession, actor: Actor) -> None:
    """Rule 6: ORM rows never cross upward. The stream yields values."""
    task = await a_task(db, actor)
    events = await TaskEvent.replay(db, task.id)
    assert not isinstance(events[0], TaskEvent)
    assert events[0].task_id == task.id


async def test_the_hub_delivers_a_committed_event_to_a_registered_subscriber(
    db: AsyncSession, actor: Actor
) -> None:
    """pg_notify → the one LISTEN → the fan-out. The whole live path, end to end."""
    task = await a_task(db, actor)
    queue = await hub.register(task.id)
    try:
        async with session_scope() as writer:
            t = await writer.get(Task, task.id)
            assert t is not None
            seq = await t.next_seq(writer)
            await TaskEvent.append(
                writer,
                TaskStatusChanged(seq=seq, task_id=TaskId(task.id), at=utcnow(), status=TaskStatus.PLANNING),
            )
            await writer.commit()

        event = await asyncio.wait_for(queue.get(), timeout=5)
        assert not isinstance(event, Poison)  # the hub's slow-subscriber pill never fires in this test
        assert is_durable(event) and event.seq == seq
        assert event.type == "task.status"
    finally:
        hub.unregister(task.id, queue)
        await hub.stop()


async def test_an_event_too_big_for_notify_still_arrives(db: AsyncSession, actor: Actor) -> None:
    """Postgres refuses a NOTIFY payload over 8000 bytes. A twelve-step plan exceeds it - so the
    fan-out sends a pointer and the hub reads the row back. If this is wrong, big plans simply
    never reach a live client, and only a big plan reveals it."""
    from prismoid.schemas.events import TaskPlanned

    task = await a_task(db, actor)

    # A plan large enough to blow the NOTIFY ceiling.
    steps = [
        PlanStepView(
            id=uuid.uuid4(),  # pyright: ignore[reportArgumentType]
            title=f"step {i} " + "x" * 200,
            kind=StepKind.STEP,
            state=StepState.PENDING,
        )
        for i in range(40)
    ]
    planned_seq = await task.next_seq(db)
    planned = TaskPlanned(seq=planned_seq, task_id=TaskId(task.id), at=utcnow(), plan=PlanView(steps=steps))
    assert len(planned.model_dump_json()) > NOTIFY_MAX_BYTES, "fixture is not big enough to test this"

    queue = await hub.register(task.id)
    try:
        await TaskEvent.append(db, planned)
        await db.commit()

        event = await asyncio.wait_for(queue.get(), timeout=5)
        assert not isinstance(event, Poison)  # the hub's slow-subscriber pill never fires in this test
        assert is_durable(event) and event.seq == planned_seq
        assert event.type == "task.planned"
        assert len(event.plan.steps) == 40
    finally:
        hub.unregister(task.id, queue)
        await hub.stop()


# ── replay is paged, history is whole (the >1000-event correctness fix) ───────────────────────


async def _flood(db: AsyncSession, task: Task, count: int) -> None:
    """Append `count` durable frames in one transaction - a long-form task's record, condensed.

    Bypasses `bump`'s per-event commit: the point is the page math, not the commit cadence.
    """
    t = await db.get(Task, task.id)
    assert t is not None
    for _ in range(count):
        seq = await t.next_seq(db)
        await TaskEvent.append(
            db,
            TaskStatusChanged(seq=seq, task_id=TaskId(task.id), at=utcnow(), status=TaskStatus.RUNNING),
        )
    await db.commit()


async def test_replay_returns_one_page_and_the_page_is_the_limit(db: AsyncSession, actor: Actor) -> None:
    """`replay` is a PAGE read now - the 1000-row cliff is the caller's loop condition, not a
    silent end of history."""
    from prismoid.db.models.event import REPLAY_PAGE

    task = await a_task(db, actor)
    await _flood(db, task, REPLAY_PAGE + 50)  # +1 task.created already there

    page = await TaskEvent.replay(db, task.id, since=0)
    assert len(page) == REPLAY_PAGE
    assert [e.seq for e in page] == list(range(1, REPLAY_PAGE + 1))


async def test_replay_events_returns_the_whole_history_past_the_page(db: AsyncSession, actor: Actor) -> None:
    """The operations pager: a 2,050-frame record comes back complete and dense - the flight
    recorder does not end at row 1000 for a late reader."""
    from prismoid.db.models.event import REPLAY_PAGE

    total = 2 * REPLAY_PAGE + 50
    task = await a_task(db, actor)
    await _flood(db, task, total - 1)  # task.created is seq 1

    events = await ops.replay_events(db, actor, task.id, since=0)
    assert len(events) == total
    assert [e.seq for e in events] == list(range(1, total + 1)), "a page boundary skipped or duplicated"


async def test_the_stream_generator_replays_across_page_boundaries(db: AsyncSession, actor: Actor) -> None:
    """`_events(follow=False)` - the scriptable flight recorder - crosses the page seam with
    nothing redelivered and nothing skipped, and still honors `since` mid-page."""
    from prismoid.api.stream import _events  # pyright: ignore[reportPrivateUsage]
    from prismoid.db.models.event import REPLAY_PAGE

    total = REPLAY_PAGE + 25
    task = await a_task(db, actor)
    await _flood(db, task, total - 1)
    await db.commit()

    seqs = [e.seq async for e in _events(task.id, since=0, follow=False) if is_durable(e)]
    assert seqs == list(range(1, total + 1))

    tail = [e.seq async for e in _events(task.id, since=REPLAY_PAGE - 3, follow=False) if is_durable(e)]
    assert tail == list(range(REPLAY_PAGE - 2, total + 1))
    await hub.stop()


# ── stream severance: a revoked credential must not keep a socket flowing ──


async def test_a_live_stream_is_severed_when_its_reproof_fails(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator ends on the FIRST failed revalidation - deadline-based, so it happens
    within one `stream_reauth_seconds` interval whether the stream is quiet or busy, with no
    client disconnect involved."""
    from prismoid.api.stream import _events  # pyright: ignore[reportPrivateUsage]
    from prismoid.conf import settings

    monkeypatch.setattr(settings.api, "stream_reauth_seconds", 0.05)
    task = await a_task(db, actor)

    proofs = 0

    async def proof() -> bool:
        nonlocal proofs
        proofs += 1
        return proofs < 3  # two passing proofs keep it alive; the third severs

    async def consume() -> list[int]:
        return [
            e.seq async for e in _events(task.id, since=0, follow=True, revalidate=proof) if is_durable(e)
        ]

    # wait_for is a test guard only: the generator must END ON ITS OWN when the proof fails.
    seqs = await asyncio.wait_for(consume(), timeout=5)
    assert seqs == [1]  # the replayed record arrived before the severance
    assert proofs == 3


async def test_the_reproof_flips_false_after_a_password_reset_style_revocation(
    db: AsyncSession, actor: Actor
) -> None:
    """The other half of the chain: `_still_authorized` is True for a live session or key row and
    False the moment the `revoke_all_for` pair (exactly what `reset_password` calls) has run -
    so an attacker's open stream dies within one interval of the victim taking the account back."""
    from prismoid.api.stream import _still_authorized  # pyright: ignore[reportPrivateUsage]
    from prismoid.db.models import WorkspaceKind, tenant
    from prismoid.db.models.api_key import ApiKey
    from prismoid.db.models.session import AuthSession
    from prismoid.db.models.user import User
    from prismoid.operations import workspaces as workspace_ops
    from prismoid.schemas.principal import Credential, CredKind

    user = await User.signup(db, email=f"s{uuid.uuid4().hex[:10]}@example.com", name="Ana", password_hash="x")
    await db.commit()
    owner = await db.get(Actor, user.actor_id)
    assert owner is not None
    # The re-proof walks the door, and the door binds a workspace: the person needs one.
    workspace = await workspace_ops.create_workspace(
        db, owner, name="Ana's workspace", kind=WorkspaceKind.PERSONAL
    )
    tenant.bind(db, workspace.id)
    task = await ops.create_task(db, owner, goal="write a memo")

    session_row, _cs_token = AuthSession.issue(user.actor_id)
    session_row.active_workspace_id = workspace.id
    db.add(session_row)
    key_row, _ck_token = ApiKey.issue(user.actor_id, "held-open-stream", workspace_id=workspace.id)
    db.add(key_row)
    await db.commit()

    session_cred = Credential(id=session_row.id, kind=CredKind.SESSION)
    key_cred = Credential(id=key_row.id, kind=CredKind.API_KEY)
    assert await _still_authorized(session_cred, user.actor_id, task.id) is True
    assert await _still_authorized(key_cred, user.actor_id, task.id) is True
    # A credential row proves only for the actor it belongs to.
    assert await _still_authorized(session_cred, uuid.uuid4(), task.id) is False

    await AuthSession.revoke_all_for(db, user.actor_id)
    await ApiKey.revoke_all_for(db, user.actor_id)
    await db.commit()

    assert await _still_authorized(session_cred, user.actor_id, task.id) is False
    assert await _still_authorized(key_cred, user.actor_id, task.id) is False
    assert (
        await _still_authorized(Credential(id=uuid.uuid4(), kind=CredKind.SESSION), user.actor_id, task.id)
        is False
    )
