# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Unread means not seen yet: an item stops being unread when its gate closes or its person looks.

Every item is still written - the inbox stays the record - but a knock that asked someone to act
on a gate reads the moment the gate closes, for everyone it reached (nobody can act any more), and a
person looking at a task has seen what it would tell them: the task page marks their own items about
it read, never a teammate's. A task that finishes while nobody is looking still knocks, unread.
Either way, the phones of whoever it read for hear which of their items read (ADR-100).

The cast: Ada owns a Project and created the task; Bob, an editor, authored its second turn, so the
knocks that reach "the creator and the turn's author" reach both of them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.push_device import PushDevice
from prismoid.db.models.session import AuthSession
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import dispatch, gate_budget, gates, inbox
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateState, TaskStatus
from prismoid.schemas.push import PushDevicePlatform
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


@dataclass
class Cast:
    ada: Actor
    bob: Actor
    workspace: Workspace
    task: Task
    step: PlanStep
    ada_key: str
    bob_key: str


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cast(db: AsyncSession) -> Cast:
    """Ada's Project task on its second turn, which Bob (an editor) asked for; a key each."""
    ada, workspace = await member(db, "Ada")
    bob, _ = await member(db, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
    project = (await project_ops.create(db, ada, name=f"seen-{uuid.uuid4().hex[:6]}")).project
    db.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
    task = await task_ops.create_task(db, ada, goal="compare three trackers", project_id=project.id)
    step = PlanStep(task_id=task.id, ordinal=0, title="open the pricing pages")
    db.add(step)
    await db.flush()
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.DELIVERED)
    await db.commit()
    await task_ops.continue_task(db, bob, task.id, goal="add pricing")
    assert task.turn_actor_id == bob.id
    keys: list[str] = []
    for who in (ada, bob):
        key, raw = ApiKey.issue(who.id, "t", workspace_id=workspace.id)
        db.add(key)
        keys.append(raw)
    await db.commit()
    return Cast(ada, bob, workspace, task, step, *keys)


async def _browser_approval(db: AsyncSession, c: Cast) -> Gate:
    """A browser ask: any editor may answer it, so it knocks Ada and Bob both."""
    gate = await gates.open_approval(
        db,
        c.task,
        StepId(c.step.id),
        [("call_0", "Browser", "browser_open", "https://example.com")],
        source="browser",
        host="example.com",
        scopes=("once", "always"),
    )
    await db.commit()
    return gate


async def _unread(*, gate_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Who still has an unread item about this gate (or task), read fresh from the database."""
    async with session_scope() as s:
        query = select(InboxItem.actor_id).where(InboxItem.read_at.is_(None))
        if gate_id is not None:
            query = query.where(InboxItem.gate_id == gate_id)
        if task_id is not None:
            query = query.where(InboxItem.task_id == task_id)
        return sorted(await s.scalars(query))


async def _kind_unread(task_id: uuid.UUID, kind: InboxKind) -> list[uuid.UUID]:
    async with session_scope() as s:
        rows = await s.scalars(
            select(InboxItem.actor_id).where(
                InboxItem.task_id == task_id, InboxItem.kind == kind.value, InboxItem.read_at.is_(None)
            )
        )
        return sorted(rows)


# ── a gate closes → its knocks read ───────────────────────────────────────────────────────────


async def test_answering_an_approval_on_the_task_page_reads_every_knock_about_it(
    db: AsyncSession, client: AsyncClient
) -> None:
    """Bob answers on the task page: his knock and Ada's both read - neither of them is needed
    any more. A gate still open on the same task keeps its own knock."""
    c = await _cast(db)
    approval = await _browser_approval(db, c)
    connect = await gates.open_connect(
        db, c.task, StepId(c.step.id), slug="notion", display="Notion", why="To create the page there."
    )
    await db.commit()
    assert await _unread(gate_id=approval.id) == sorted([c.ada.id, c.bob.id])

    r = await client.post(
        f"/v1/gates/{approval.id}/resolve",
        json={"action": "accept", "content": {"scope": "once"}},
        headers=_h(c.bob_key),
    )
    assert r.status_code == 200, r.text

    assert await _unread(gate_id=approval.id) == []
    assert await _unread(gate_id=connect.id) == [c.ada.id], "a gate still open still asks"


async def test_a_cancel_from_a_gate_reads_every_open_gates_knocks(
    db: AsyncSession, client: AsyncClient
) -> None:
    """Cancel on an approval stops the task and closes every gate it held (the batch close)."""
    c = await _cast(db)
    approval = await _browser_approval(db, c)
    connect = await gates.open_connect(
        db, c.task, StepId(c.step.id), slug="notion", display="Notion", why="To create the page there."
    )
    await db.commit()

    r = await client.post(
        f"/v1/gates/{approval.id}/resolve", json={"action": "cancel"}, headers=_h(c.bob_key)
    )
    assert r.status_code == 200, r.text

    async with session_scope() as s:
        task = await s.get(Task, c.task.id)
        assert task is not None and task.state is TaskStatus.CANCELLED
    assert await _unread(gate_id=approval.id) == []
    assert await _unread(gate_id=connect.id) == []


async def test_a_budget_knock_carries_its_gate_and_reads_when_it_is_answered(
    db: AsyncSession, client: AsyncClient
) -> None:
    c = await _cast(db)
    c.task.status = TaskStatus.RUNNING.value  # the Bursar parks a running task
    gate = await gate_budget.open_budget_gate(
        db, c.task, title=gate_budget.BUDGET_TITLE, reason="the task budget of $0.01 is spent"
    )
    knocks = list(await db.scalars(select(InboxItem).where(InboxItem.task_id == c.task.id)))
    assert {k.gate_id for k in knocks if k.kind == InboxKind.BUDGET.value} == {gate.id}
    assert await _kind_unread(c.task.id, InboxKind.BUDGET) == sorted([c.ada.id, c.bob.id])

    r = await client.post(f"/v1/gates/{gate.id}/resolve", json={"action": "cancel"}, headers=_h(c.ada_key))
    assert r.status_code == 200, r.text

    assert await _kind_unread(c.task.id, InboxKind.BUDGET) == []


async def test_an_approval_nobody_answered_reads_when_its_clock_closes_it(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, silent_dispatcher: None
) -> None:
    """The sweep declines an expired approval as the system: the knock stops saying it needs
    anyone, though nobody clicked it."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)  # the TTL arm only
    c = await _cast(db)
    gate = await _browser_approval(db, c)
    gate.expires_at = utcnow() - dt.timedelta(minutes=1)
    await db.commit()

    assert gate.id in await gates.sweep(db)

    async with session_scope() as s:
        closed = await s.get(Gate, gate.id)
        assert closed is not None and closed.state == GateState.RESOLVED.value
    assert await _unread(gate_id=gate.id) == []


async def test_delivery_expires_the_open_gates_knocks_and_still_knocks_unread(
    db: AsyncSession, silent_dispatcher: None
) -> None:
    """A task that delivers while nobody is looking: the gate it no longer waits on stops asking,
    and the delivery itself is news for both of them - closing a gate reads only its own knocks."""
    c = await _cast(db)
    gate = await _browser_approval(db, c)

    await gates.expire_open(db, c.task)
    await inbox.task_completed(db, c.task, "Three trackers, priced.")
    await db.commit()

    async with session_scope() as s:
        expired = await s.get(Gate, gate.id)
        assert expired is not None and expired.state == GateState.EXPIRED.value
    assert await _unread(gate_id=gate.id) == []
    assert await _kind_unread(c.task.id, InboxKind.TASK_COMPLETED) == sorted([c.ada.id, c.bob.id])


# ── the person looks → their own items read ─────────────────────────────────────────────────


async def test_opening_a_task_reads_only_the_callers_own_items_about_it(
    db: AsyncSession, client: AsyncClient
) -> None:
    """Bob opens the task page: his delivery note reads, Ada's stays unread (she has not looked),
    and Bob's note about another task is untouched."""
    c = await _cast(db)
    await inbox.task_completed(db, c.task, "Three trackers, priced.")
    other = await task_ops.create_task(db, c.bob, goal="a solo memo")
    await inbox.task_completed(db, other, "The memo.")
    await db.commit()

    r = await client.post(f"/v1/inbox/tasks/{c.task.id}/read", headers=_h(c.bob_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"marked": 1, "unread": 1}  # the solo memo's note is still his to read

    assert await _unread(task_id=c.task.id) == [c.ada.id]
    assert await _unread(task_id=other.id) == [c.bob.id]

    again = await client.post(f"/v1/inbox/tasks/{c.task.id}/read", headers=_h(c.bob_key))
    assert again.json() == {"marked": 0, "unread": 1}, "a second look reads nothing new"


async def test_watching_a_task_reads_the_knock_its_stream_just_brought(
    db: AsyncSession, client: AsyncClient
) -> None:
    """The page is open when the task delivers: the stream's news is followed by another call, and
    the delivery Bob watched land is not left waiting in his inbox."""
    c = await _cast(db)
    first = await client.post(f"/v1/inbox/tasks/{c.task.id}/read", headers=_h(c.bob_key))
    assert first.json()["marked"] == 0

    await inbox.task_completed(db, c.task, "Three trackers, priced.")
    await db.commit()
    seen = await client.post(f"/v1/inbox/tasks/{c.task.id}/read", headers=_h(c.bob_key))
    assert seen.json()["marked"] == 1

    assert await _kind_unread(c.task.id, InboxKind.TASK_COMPLETED) == [c.ada.id]


async def test_a_task_the_caller_cannot_see_is_not_found_and_touches_nothing(
    db: AsyncSession, client: AsyncClient
) -> None:
    """A stranger from another workspace naming the task gets the 404 a made-up id gets, and
    reads nothing of Ada's or Bob's."""
    c = await _cast(db)
    await inbox.task_completed(db, c.task, "Three trackers, priced.")
    await db.commit()
    async with session_scope() as s:
        stranger, elsewhere = await member(s, "Eve")
        key, raw = ApiKey.issue(stranger.id, "t", workspace_id=elsewhere.id)
        s.add(key)
        await s.commit()

    def said(response: Response) -> tuple[int, str, str]:
        error = response.json()["error"]
        return response.status_code, error["code"], error["message"]

    theirs = await client.post(f"/v1/inbox/tasks/{c.task.id}/read", headers=_h(raw))
    missing = await client.post(f"/v1/inbox/tasks/{uuid.uuid4()}/read", headers=_h(raw))
    assert said(theirs) == (404, "not_found", "no such task"), theirs.text
    assert said(missing) == said(theirs), "someone else's task reads as no task"

    assert await _unread(task_id=c.task.id) == sorted([c.ada.id, c.bob.id])


# ── the phones hear it ────────────────────────────────────────────────────────────────────────


async def _phones(db: AsyncSession, *people: Actor) -> None:
    """A users row, a live login session and a phone each, so a read asks for their silent push."""
    for person in people:
        db.add(User(actor_id=person.id, email=f"seen{uuid.uuid4().hex[:10]}@example.com", name="Seen"))
        await db.flush()  # the login hangs off the users row
        login, _ = AuthSession.issue(person.id)
        db.add(login)
        await db.flush()
        await PushDevice.register(
            db,
            actor_id=person.id,
            session_id=login.id,
            token=f"fcm-{uuid.uuid4().hex}",
            platform=PushDevicePlatform.IOS,
            app_id="so.plane.prismoid",
            app_version="1.0.0",
        )
    await db.commit()


async def _items(**where: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
    """Each recipient's item matching `where` (`gate_id`, `task_id`): recipient → item."""
    async with session_scope() as s:
        query = select(InboxItem.actor_id, InboxItem.id)
        for column, value in where.items():
            query = query.where(getattr(InboxItem, column) == value)
        return dict((await s.execute(query)).tuples().all())


def _reads(d: RecordingDispatcher) -> dict[str, str]:
    """The silent pushes asked for: recipient → the ids they name."""
    asked = [d.arguments[key] for job, key in d.enqueues if job == "push_read"]
    return {str(a["actor_id"]): str(a["inbox_ids"]) for a in asked}


async def test_a_closed_gate_tells_every_recipients_phones_which_knock_read(
    db: AsyncSession, client: AsyncClient, recording_dispatcher: RecordingDispatcher, push_on: None
) -> None:
    """Bob answers: his phone and Ada's each hear that their own knock about the gate read."""
    c = await _cast(db)
    await _phones(db, c.ada, c.bob)
    approval = await _browser_approval(db, c)
    knocks = await _items(gate_id=approval.id)

    r = await client.post(
        f"/v1/gates/{approval.id}/resolve",
        json={"action": "accept", "content": {"scope": "once"}},
        headers=_h(c.bob_key),
    )
    assert r.status_code == 200, r.text
    await dispatch.flush_enqueues()

    assert _reads(recording_dispatcher) == {
        str(c.ada.id): str(knocks[c.ada.id]),
        str(c.bob.id): str(knocks[c.bob.id]),
    }


async def test_opening_a_task_tells_only_the_callers_phones(
    db: AsyncSession, client: AsyncClient, recording_dispatcher: RecordingDispatcher, push_on: None
) -> None:
    """Bob opens the task page: his phone hears his note read; Ada's hears nothing (hers is still
    unread), and a second look asks for nothing."""
    c = await _cast(db)
    await _phones(db, c.ada, c.bob)
    await inbox.task_completed(db, c.task, "Three trackers, priced.")
    await db.commit()
    notes = await _items(task_id=c.task.id)

    for _ in range(2):
        r = await client.post(f"/v1/inbox/tasks/{c.task.id}/read", headers=_h(c.bob_key))
        assert r.status_code == 200, r.text
    await dispatch.flush_enqueues()

    assert _reads(recording_dispatcher) == {str(c.bob.id): str(notes[c.bob.id])}
    key = f"push-read-{notes[c.bob.id]}"
    assert recording_dispatcher.arguments[key]["workspace_id"] == str(c.workspace.id)
