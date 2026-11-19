# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The inbox on a phone (ADR-100): an item asks for its push only once its transaction commits, never
when it rolls back (a released savepoint included), and only for a person with a live device; an item
written in a job's body asks for a job of its own, never that job's child; the push reaches every live
device and prunes the tokens FCM calls dead; a read, here or elsewhere, sends the phones a silent
update with the new counts once it commits; the badge counts every workspace the person belongs to.
FCM is a fake: no network.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from dbos import DBOS
from dbos._context import EnterDBOSWorkflow
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import fcm
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.push_device import PushDevice
from prismoid.db.models.session import AuthSession
from prismoid.db.models.workspace import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceStatus
from prismoid.db.session import session_scope
from prismoid.errors import TenantUnbound
from prismoid.operations import auth as auth_ops
from prismoid.operations import dispatch, push
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import workspaces as workspace_ops
from prismoid.schemas.push import PushChannel, PushDevicePlatform, SendOutcome
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.push_sender import FakePushSender

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Person:
    actor_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    email: str


@pytest.fixture
def sender() -> Iterator[FakePushSender]:
    fake = FakePushSender()
    fcm.install(fake)
    yield fake
    fcm.install(None)


async def _person(db: AsyncSession, name: str) -> Person:
    email = f"push{uuid.uuid4().hex[:12]}@example.com"
    signed = await auth_ops.signup(db, email=email, name=name, password="a-long-enough-password")
    principal = await AuthSession.resolve(db, signed.token)
    assert principal is not None and principal.workspace_id and principal.auth_session_id
    return Person(principal.actor.id, principal.workspace_id, principal.auth_session_id, email)


async def _phone(db: AsyncSession, person: Person, session_id: uuid.UUID | None = None) -> str:
    token = f"fcm-{uuid.uuid4().hex}"
    await PushDevice.register(
        db,
        actor_id=person.actor_id,
        session_id=session_id or person.session_id,
        token=token,
        platform=PushDevicePlatform.IOS,
        app_id="so.plane.prismoid",
        app_version="1.0.0",
    )
    await db.commit()
    return token


async def _knock(person: Person, *, commit: bool = True, workspace_id: uuid.UUID | None = None) -> uuid.UUID:
    async with session_scope(workspace_id or person.workspace_id) as session:
        item = await inbox_ops.connector_broken(
            session, actor_id=person.actor_id, name="Linear", slug="linear", why="the token expired"
        )
        if not commit:
            await session.rollback()  # the item never was; the same session's next commit asks for nothing
            await session.execute(select(1))
        await session.commit()
        return item.id


async def test_an_item_asks_for_its_push_once_committed(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)

    item_id = await _knock(ana)
    await dispatch.flush_enqueues()

    assert ("push_inbox", f"push-inbox-{item_id}") in recording_dispatcher.enqueues
    assert recording_dispatcher.arguments[f"push-inbox-{item_id}"] == {"item_id": str(item_id)}
    assert recording_dispatcher.delays[f"push-inbox-{item_id}"] == settings.push.alert_delay_s


async def test_a_rolled_back_item_asks_for_nothing(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)

    await _knock(ana, commit=False)
    await dispatch.flush_enqueues()

    assert not [job for job, _ in recording_dispatcher.enqueues if job == "push_inbox"]


async def test_no_phone_or_push_off_asks_for_nothing(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Off for certain: a developer's own `.env` may carry a Firebase key.
    monkeypatch.setattr(settings.push, "credentials_json", SecretStr(""))
    ana, bo = await _person(db, "Ana"), await _person(db, "Bo")
    await _phone(db, bo)
    fcm.install(FakePushSender())
    try:
        await _knock(ana)  # push on, but Ana has no phone
    finally:
        fcm.install(None)
    await _knock(bo)  # Bo has a phone, but this deployment has no Firebase key
    await dispatch.flush_enqueues()

    assert not [job for job, _ in recording_dispatcher.enqueues if job == "push_inbox"]


async def test_the_push_reaches_every_live_phone_and_prunes_the_dead(
    db: AsyncSession, silent_dispatcher: None, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    live, dead = await _phone(db, ana), await _phone(db, ana)
    signed_in = await auth_ops.login(db, email=ana.email, password="a-long-enough-password")
    lapsed_session = await AuthSession.resolve(db, signed_in.token)
    assert lapsed_session is not None and lapsed_session.auth_session_id
    lapsed = await _phone(db, ana, lapsed_session.auth_session_id)
    await db.execute(
        update(AuthSession)
        .where(AuthSession.id == lapsed_session.auth_session_id)
        .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
    )
    await db.commit()
    sender.answers[dead] = SendOutcome.DEAD
    item_id = await _knock(ana)

    async with session_scope(ana.workspace_id) as session:
        sent = await push.send_inbox(session, item_id)

    assert sent == 1
    [(tokens, message)] = sender.sent
    assert sorted(tokens) == sorted([live, dead])  # the expired session's phone hears nothing
    assert lapsed not in tokens
    assert message.data["inbox_id"] == str(item_id)
    assert message.data["kind"] == "connector_broken"
    assert message.data["connector_slug"] == "linear"
    assert message.data["unread"] == "1"
    assert message.data["unread_total"] == "1"
    assert message.badge == 1
    assert message.channel is PushChannel.UPDATES
    db.expire_all()
    left = set(await db.scalars(select(PushDevice.token).where(PushDevice.actor_id == ana.actor_id)))
    assert left == {live, lapsed}


async def test_an_item_read_before_its_push_runs_rings_nothing(
    db: AsyncSession, silent_dispatcher: None, sender: FakePushSender
) -> None:
    """The person read it first (its task was open, or its gate closed): the alert must not ring,
    nor land after the silent update that cleared it."""
    ana = await _person(db, "Ana")
    await _phone(db, ana)
    item_id = await _knock(ana)
    async with session_scope(ana.workspace_id) as session:
        actor = await session.get(Actor, ana.actor_id)
        assert actor is not None
        await inbox_ops.mark_read(session, actor, item_id)

    async with session_scope(ana.workspace_id) as session:
        assert await push.send_inbox(session, item_id) == 0
    assert sender.sent == []


async def test_an_item_gone_by_send_time_pushes_nothing(
    db: AsyncSession, silent_dispatcher: None, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)
    async with session_scope(ana.workspace_id) as session:
        assert await push.send_inbox(session, uuid.uuid4()) == 0
    assert sender.sent == []


async def test_a_read_sends_the_phones_a_silent_update(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    token = await _phone(db, ana)
    first = await _knock(ana)
    await _knock(ana)

    async with session_scope(ana.workspace_id) as session:
        actor = await session.get(Actor, ana.actor_id)
        assert actor is not None
        await inbox_ops.mark_read(session, actor, first)
        await inbox_ops.mark_read(session, actor, first)  # already read: nothing new to say
    await dispatch.flush_enqueues()

    key = f"push-read-{first}"
    assert [k for job, k in recording_dispatcher.enqueues if job == "push_read"] == [key]
    assert recording_dispatcher.arguments[key] == {
        "actor_id": str(ana.actor_id),
        "workspace_id": str(ana.workspace_id),
        "inbox_ids": str(first),
    }

    async with session_scope(ana.workspace_id) as session:
        await push.send_read(session, ana.actor_id, str(first))
    [(tokens, message)] = sender.sent
    assert tokens == [token]
    assert message.silent and message.title is None
    assert message.data == {
        "type": "inbox_read",
        "inbox_ids": str(first),
        "workspace_id": str(ana.workspace_id),
        "unread": "1",
        "unread_total": "1",
    }
    assert message.badge == 1


async def test_read_all_sends_one_silent_update_for_all(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)
    await _knock(ana)
    await _knock(ana)

    async with session_scope(ana.workspace_id) as session:
        actor = await session.get(Actor, ana.actor_id)
        assert actor is not None
        assert await inbox_ops.mark_all_read(session, actor) == 2
        assert await inbox_ops.mark_all_read(session, actor) == 0  # nothing changed: no push
    await dispatch.flush_enqueues()

    reads = [k for job, k in recording_dispatcher.enqueues if job == "push_read"]
    assert len(reads) == 1 and reads[0].startswith(f"push-read-all-{ana.actor_id}-{ana.workspace_id}-")
    assert recording_dispatcher.arguments[reads[0]]["inbox_ids"] == "all"

    async with session_scope(ana.workspace_id) as session:
        await push.send_read(session, ana.actor_id, "all")
    assert sender.sent[-1][1].data == {
        "type": "inbox_read",
        "inbox_ids": "all",
        "workspace_id": str(ana.workspace_id),
        "unread": "0",
        "unread_total": "0",
    }
    assert sender.sent[-1][1].badge == 0


class _ParentWatchingDispatcher(RecordingDispatcher):
    """Records, per job key, the job the enqueue ran inside: the engine starts a job enqueued from a
    job's body as that job's child."""

    def __init__(self) -> None:
        super().__init__()
        self.parents: dict[str, str | None] = {}

    async def enqueue(self, job: str, key: str, *, delay_s: float = 0.0, **kwargs: object) -> None:
        self.parents[key] = DBOS.workflow_id
        await super().enqueue(job, key, delay_s=delay_s, **kwargs)


async def test_an_item_written_in_a_jobs_body_asks_for_a_job_of_its_own(
    db: AsyncSession, sender: FakePushSender
) -> None:
    """A routine's fire writes its inbox item in the workflow body; the push must not become that
    workflow's child (journaled out of order, unreplayable on recovery)."""
    ana = await _person(db, "Ana")
    await _phone(db, ana)
    watching = _ParentWatchingDispatcher()
    dispatch.set_dispatcher(watching)
    try:
        with EnterDBOSWorkflow({"name": "fire_routine"}, None):
            assert DBOS.workflow_id is not None  # inside a job's body, as `execute_fire` runs
            item_id = await _knock(ana)
        await dispatch.flush_enqueues()
    finally:
        dispatch.set_dispatcher(dispatch.NullDispatcher())

    key = f"push-inbox-{item_id}"
    assert ("push_inbox", key) in watching.enqueues
    assert watching.parents[key] is None  # a top-level job, not a child of the workflow


async def test_a_released_savepoint_rolled_back_with_its_transaction_asks_for_nothing(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)

    async with session_scope(ana.workspace_id) as session:
        async with session.begin_nested():  # released: the item now rides the outer transaction
            await inbox_ops.connector_broken(
                session, actor_id=ana.actor_id, name="Linear", slug="linear", why="the token expired"
            )
        await session.rollback()  # and the outer transaction takes it back
    await dispatch.flush_enqueues()

    assert not [job for job, _ in recording_dispatcher.enqueues if job == "push_inbox"]
    assert sender.sent == []


async def test_a_savepoint_rolled_back_under_a_commit_pushes_nothing(
    db: AsyncSession, recording_dispatcher: RecordingDispatcher, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)

    async with session_scope(ana.workspace_id) as session:
        nested = await session.begin_nested()
        item = await inbox_ops.connector_broken(
            session, actor_id=ana.actor_id, name="Linear", slug="linear", why="the token expired"
        )
        item_id = item.id
        await nested.rollback()  # the item never was
        await session.commit()  # but the outer transaction commits
    await dispatch.flush_enqueues()

    async with session_scope(ana.workspace_id) as session:
        assert await push.send_inbox(session, item_id) == 0  # the job finds no item: no push
    assert sender.sent == []


async def test_the_badge_counts_every_active_workspace_the_person_belongs_to(
    db: AsyncSession, silent_dispatcher: None, sender: FakePushSender
) -> None:
    ana = await _person(db, "Ana")
    await _phone(db, ana)
    actor = await db.get(Actor, ana.actor_id)
    assert actor is not None
    team = await workspace_ops.create_workspace(db, actor, name="Team", kind=WorkspaceKind.TEAM)
    left = await workspace_ops.create_workspace(db, actor, name="Left", kind=WorkspaceKind.TEAM)
    paused = await workspace_ops.create_workspace(db, actor, name="Paused", kind=WorkspaceKind.TEAM)
    await db.commit()
    await _knock(ana, workspace_id=team.id)
    await _knock(ana, workspace_id=left.id)
    await _knock(ana, workspace_id=paused.id)
    await WorkspaceMember.remove(db, left.id, ana.actor_id)  # gone: its items reach no badge
    # suspended: still a member, but its inbox can't be opened, so it can't hold the badge up
    await db.execute(
        update(Workspace).where(Workspace.id == paused.id).values(status=WorkspaceStatus.SUSPENDED.value)
    )
    await db.commit()
    item_id = await _knock(ana)

    async with session_scope(ana.workspace_id) as session:
        await push.send_inbox(session, item_id)
        await push.send_read(session, ana.actor_id, str(item_id))

    alert, read = sender.sent[0][1], sender.sent[1][1]
    assert (alert.data["unread"], alert.data["unread_total"], alert.badge) == ("1", "2", 2)
    assert (read.data["workspace_id"], read.data["unread"], read.data["unread_total"]) == (
        str(ana.workspace_id),
        "1",
        "2",
    )
    assert read.badge == 2


async def test_the_total_is_a_platform_read_and_fails_closed_on_a_workspace_session(db: AsyncSession) -> None:
    ana = await _person(db, "Ana")
    async with session_scope(ana.workspace_id) as session:
        with pytest.raises(TenantUnbound):
            await InboxItem.unread_total_of(session, ana.actor_id)
