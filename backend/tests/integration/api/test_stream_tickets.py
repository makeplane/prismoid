# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Stream tickets over the real routes: how a browser opens an event stream without its session
token in the URL.

A ticket is minted by an authenticated POST for one task or conversation the caller can read; it
opens that one stream, once, within thirty seconds; it is stored only as a hash and names the
credential row that minted it, never the token; the stream it opens keeps re-proving that row, so
a logout severs it within one interval. The session token in `?token=` authenticates nothing.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.session import AuthSession
from prismoid.db.models.stream_ticket import StreamTicket
from prismoid.db.session import session_scope
from prismoid.operations import bots as bot_ops
from prismoid.operations import chat as chat_ops
from prismoid.operations import stream_tickets as ticket_ops
from prismoid.schemas.bots import BotSource
from prismoid.schemas.ids import hash_token
from prismoid.schemas.principal import Credential, Principal

pytestmark = pytest.mark.integration

# A ceiling on waiting for something the stream does on its own: far above its re-proof interval, so
# a slow machine never fails the test, and still a bound, so a stream that never ends does.
_GENEROUS_S = 15
_TASK_BODY = {"goal": "write a short memo about tickets", "clarify": "never", "budget": {"usd": "1"}}
_BOT_MD = """---
name: {name}
description: >
  Writes release notes from shipped work. Load when: a release needs notes. Not for: contracts.
capability: fast-executor
produces: document
---
Write plainly.
"""


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: AsyncClient, name: str) -> str:
    signed = await client.post(
        "/v1/auth/signup",
        json={
            "email": f"tk{uuid.uuid4().hex[:12]}@example.com",
            "name": name,
            "password": "a-long-enough-password",
        },
    )
    assert signed.status_code == 201, signed.text
    return str(signed.json()["token"])


async def _task(client: AsyncClient, token: str) -> str:
    created = await client.post("/v1/tasks", headers=_h(token), json=_TASK_BODY)
    assert created.status_code == 202, created.text
    return str(created.json()["task_id"])


async def _conversation(token: str) -> str:
    """A conversation with one line on its record, opened through the operations (the API has no
    route that starts one without a bot to talk to)."""
    async with session_scope() as session:
        principal = await AuthSession.resolve(session, token)
        assert principal is not None and principal.workspace_id is not None
        tenant.bind(session, principal.workspace_id, principal.role)  # what the door would bind
        name = f"scribe-{uuid.uuid4().hex[:8]}"
        await bot_ops.push(
            session, actor=principal.actor, name=name, source=BotSource(bot_md=_BOT_MD.format(name=name))
        )
        conv = await chat_ops.open_conversation(session, principal.actor, bot_name=name)
        await chat_ops.send(session, principal.actor, conv.id, "what changed in 3.2?")
        await session.commit()
        return str(conv.id)


async def _mint(client: AsyncClient, token: str, path: str) -> str:
    minted = await client.post(f"{path}/events/ticket", headers=_h(token))
    assert minted.status_code == 200, minted.text
    body = minted.json()
    assert body["expires_in"] == 30
    assert str(body["ticket"]).startswith("et_")
    return str(body["ticket"])


async def _replay(client: AsyncClient, path: str, ticket: str) -> Response:
    """A finite replay (`follow=false`) opened the way a browser opens the stream."""
    return await client.get(f"{path}/events?since=0&follow=false&ticket={ticket}")


async def test_a_ticket_opens_its_own_task_stream_once(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    path = f"/v1/tasks/{await _task(client, token)}"
    ticket = await _mint(client, token, path)

    opened = await _replay(client, path, ticket)
    assert opened.status_code == 200, opened.text
    assert "task.created" in opened.text

    again = await _replay(client, path, ticket)
    assert again.status_code == 401, "a ticket is spent by the stream it opened"


async def test_a_ticket_opens_its_own_conversation_stream_once(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    path = f"/v1/conversations/{await _conversation(token)}"
    ticket = await _mint(client, token, path)

    opened = await _replay(client, path, ticket)
    assert opened.status_code == 200, opened.text
    assert "what changed in 3.2?" in opened.text
    assert (await _replay(client, path, ticket)).status_code == 401


async def test_the_database_holds_only_the_hash_and_the_credential_row(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    path = f"/v1/tasks/{await _task(client, token)}"
    ticket = await _mint(client, token, path)
    async with session_scope() as session:  # read inside: the scope's rollback expires the rows
        row = await session.scalar(select(StreamTicket).where(StreamTicket.token_hash == hash_token(ticket)))
        session_row = await session.scalar(
            select(AuthSession).where(AuthSession.token_hash == hash_token(token))
        )
        assert row is not None and session_row is not None
        assert row.credential_kind == "session" and row.credential_id == session_row.id
        assert row.subject_kind == "task" and str(row.subject_id) == path.rsplit("/", 1)[1]
        stored = " ".join(str(getattr(row, column.key)) for column in StreamTicket.__table__.columns)
    assert ticket not in stored and token not in stored


async def test_an_expired_ticket_opens_nothing(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    path = f"/v1/tasks/{await _task(client, token)}"
    ticket = await _mint(client, token, path)
    async with session_scope() as session:
        await session.execute(
            update(StreamTicket)
            .where(StreamTicket.token_hash == hash_token(ticket))
            .values(expires_at=func.now() - dt.timedelta(seconds=1))
        )
        await session.commit()
    assert (await _replay(client, path, ticket)).status_code == 401


async def test_a_ticket_opens_no_other_stream_and_is_spent_by_trying(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    one, other = f"/v1/tasks/{await _task(client, token)}", f"/v1/tasks/{await _task(client, token)}"
    conversation = f"/v1/conversations/{await _conversation(token)}"

    for minted_for, presented_at in ((one, other), (one, conversation), (conversation, one)):
        ticket = await _mint(client, token, minted_for)
        assert (await _replay(client, presented_at, ticket)).status_code == 401, (minted_for, presented_at)
        assert (await _replay(client, minted_for, ticket)).status_code == 401, "spent at the wrong door"


async def test_only_someone_who_can_read_the_subject_mints(client: AsyncClient) -> None:
    ada, bob = await _signup(client, "Ada"), await _signup(client, "Bob")
    task = f"/v1/tasks/{await _task(client, ada)}"
    conversation = f"/v1/conversations/{await _conversation(ada)}"
    for path in (task, conversation, f"/v1/tasks/{uuid.uuid4()}"):
        refused = await client.post(f"{path}/events/ticket", headers=_h(bob))
        assert refused.status_code == 404, (path, refused.text)
    assert (await client.post(f"{task}/events/ticket")).status_code == 401


async def test_the_session_token_in_the_url_authenticates_nothing(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    task = f"/v1/tasks/{await _task(client, token)}"
    for path in (f"{task}/events?since=0&follow=false&token={token}", f"{task}/events?ticket={token}"):
        assert (await client.get(path)).status_code == 401, path
    assert (await client.get(f"{task}/events?since=0&follow=false", headers=_h(token))).status_code == 200


async def test_a_ticketed_stream_is_severed_when_its_credential_is_revoked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket outlives nothing its credential does not: logging out revokes the session row the
    ticket named, and the open stream finishes on its own at the next re-proof."""
    token = await _signup(client, "Ada")
    path = f"/v1/tasks/{await _task(client, token)}"
    ticket = await _mint(client, token, path)

    # Count the proofs that pass from here on: the door's at admission, then the stream's re-proofs.
    passed, proven = 0, asyncio.Event()
    door_and_two_reproofs = 3
    prove = ticket_ops.prove

    async def counted(
        session: AsyncSession, credential: Credential, actor_id: uuid.UUID
    ) -> Principal[Actor] | None:
        nonlocal passed
        principal = await prove(session, credential, actor_id)
        if principal is not None:
            passed += 1
            if passed >= door_and_two_reproofs:
                proven.set()
        return principal

    monkeypatch.setattr(ticket_ops, "prove", counted)
    stream = asyncio.create_task(client.get(f"{path}/events?since=0&ticket={ticket}"))
    try:
        await asyncio.wait_for(proven.wait(), timeout=_GENEROUS_S)
        assert not stream.done(), "a live credential's stream stays open across passing re-proofs"
        out = await client.post("/v1/auth/logout", headers=_h(token))
        assert out.status_code < 300, out.text
        # No client disconnect, no cancel: the response must FINISH on its own. wait_for is the guard.
        response = await asyncio.wait_for(stream, timeout=_GENEROUS_S)
    finally:
        if not stream.done():
            stream.cancel()  # a failed assertion must not leave the request hanging
            with contextlib.suppress(asyncio.CancelledError):
                await stream
    assert response.status_code == 200 and "task.created" in response.text
    # And the dead credential cannot mint the reconnect's next ticket.
    assert (await client.post(f"{path}/events/ticket", headers=_h(token))).status_code == 401


async def test_an_api_key_mints_tickets_too(client: AsyncClient) -> None:
    session_token = await _signup(client, "Ada")
    key = (await client.post("/v1/auth/api-keys", headers=_h(session_token), json={"name": "tab"})).json()[
        "key"
    ]
    path = f"/v1/tasks/{await _task(client, key)}"
    ticket = await _mint(client, key, path)
    async with session_scope() as session:
        kind = await session.scalar(
            select(StreamTicket.credential_kind).where(StreamTicket.token_hash == hash_token(ticket))
        )
    assert kind == "api_key"
    assert (await _replay(client, path, ticket)).status_code == 200
