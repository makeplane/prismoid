# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Inbox - and the one property that would be a breach rather than a bug.

An inbox is the only place in the product where *"whose is this?"* and *"what is this?"* are the
same question. There is no such thing as an item you may read but which is not addressed to you - so
the actor is not a filter a view remembers to apply, it is the first clause of the only query
`InboxItem` exposes.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.session import session_scope
from prismoid.errors import NotFound
from prismoid.operations import auth as auth_ops
from prismoid.operations import inbox as ops
from prismoid.schemas.verification import Diagnosis

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def a_person(db: AsyncSession, name: str) -> Actor:
    result = await auth_ops.signup(
        db, email=f"{name}{uuid.uuid4().hex[:8]}@example.com", name=name, password="a-long-enough-password"
    )
    from prismoid.db.models.session import AuthSession

    principal = await AuthSession.resolve(db, result.token)
    assert principal is not None
    return principal.actor


async def an_item(db: AsyncSession, actor: Actor, title: str) -> InboxItem:
    item = InboxItem(
        id=uuid.uuid4(), actor_id=actor.id, kind=InboxKind.TASK_COMPLETED.value, title=title, body="…"
    )
    db.add(item)
    await db.commit()
    return item


async def test_an_inbox_is_nobody_elses_to_read(db: AsyncSession) -> None:
    """**THE test.** Not a bug if it fails - a breach."""
    mine = await a_person(db, "Ana")
    theirs = await a_person(db, "Bo")
    await an_item(db, theirs, "Bo's delivery")

    assert await InboxItem.list_for(db, mine) == []
    assert await InboxItem.unread_count(db, mine) == 0


async def test_reading_someone_elses_item_is_NotFound_never_Forbidden(db: AsyncSession) -> None:
    """A 403 would confirm that somebody else has an item with this id - which is exactly the thing
    an inbox must not leak. "It exists but you may not see it" is still a disclosure."""
    mine = await a_person(db, "Ana")
    theirs = await a_person(db, "Bo")
    item = await an_item(db, theirs, "Bo's delivery")

    with pytest.raises(NotFound):
        await ops.mark_read(db, mine, item.id)


async def test_mark_all_read_touches_only_your_own(db: AsyncSession) -> None:
    """The `where actor_id = :me` is not a nicety on a bulk UPDATE. It is the whole statement."""
    mine = await a_person(db, "Ana")
    theirs = await a_person(db, "Bo")
    await an_item(db, mine, "mine")
    their_item = await an_item(db, theirs, "theirs")

    assert await ops.mark_all_read(db, mine) == 1
    assert await InboxItem.unread_count(db, mine) == 0
    assert await InboxItem.unread_count(db, theirs) == 1  # untouched

    still_unread = await InboxItem.get_for(db, their_item.id, theirs)
    assert still_unread.is_unread()


async def test_an_escalation_carries_its_diagnosis(db: AsyncSession) -> None:
    """**An inbox item you cannot act on from is just noise with a timestamp**.

    The person reading this did not watch the three attempts. They need what was tried, what was
    wrong with it, and what they could do about it - so the diagnosis travels with the notification,
    not in a log file they do not have.
    """
    actor = await a_person(db, "Ana")
    from prismoid.db.models.task import Task

    task = Task(id=uuid.uuid4(), goal="do the thing", creator_actor_id=actor.id, criteria={})
    db.add(task)
    await db.flush()

    diagnosis = Diagnosis(
        what_failed="writer could not produce a deliverable that passes verification",
        why="3 attempts, each rejected. The last one: `numbers:grounded` failed → in no source: 99.7M",
        attempts=3,
        evidence=["attempt 1 …", "attempt 2 …", "attempt 3 …"],
        suggested_next=["The sources may not contain the figures the goal asks for."],
    )
    item = await ops.escalated(db, task, diagnosis)
    await db.commit()

    assert item.kind == InboxKind.ESCALATION.value
    assert item.task_id == task.id  # the pointer IS the product
    assert "99.7M" in item.body  # the finding, not a category
    assert "sources may not contain" in item.body  # …and what to do about it
    assert item.payload["diagnosis"]["attempts"] == 3
