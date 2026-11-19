# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation stream's wire (ADR-059) - the bytes an EventSource sees on
`GET /v1/conversations/{id}/events`, the task stream's wire test one table over.

Pinned against the real route, in-process: `message` frames carry `id: <seq>` (the browser's
reconnect cursor) and parse as the list route's own `MessageOut`; the header authenticates
exactly as on the task stream (a browser's ticket: test_stream_tickets.py); `since` replays
past the cursor; a stranger's token is a 404 (existence is privileged, like the row).

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.api.serializers.conversations import MessageOut
from prismoid.db.models import tenant
from prismoid.db.models.session import AuthSession
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import bots as bot_ops
from prismoid.operations import chat as chat_ops
from prismoid.schemas.bots import BotSource

pytestmark = pytest.mark.integration

BOT_MD = """---
name: {name}
description: >
  Writes release notes from shipped work. Load when: a release needs notes. Not for: contracts.
capability: fast-executor
produces: document
---
Write plainly.
"""


@pytest.fixture(autouse=True)
async def quiet_hub() -> AsyncIterator[None]:
    yield
    await hub.stop()


async def _login(name: str) -> str:
    async with session_scope() as session:
        result = await auth_ops.signup(
            session,
            email=f"u{uuid.uuid4().hex[:12]}@example.com",
            name=name,
            password="a-long-enough-password",
        )
        await session.commit()
        return result.token


@pytest.fixture
async def wired() -> tuple[str, str]:
    """A real login, a draft bot (the author's own workbench chats), two lines on the record."""
    token = await _login("Ana")
    async with session_scope() as session:
        principal = await AuthSession.resolve(session, token)
        assert principal is not None and principal.workspace_id is not None
        tenant.bind(session, principal.workspace_id, principal.role)  # what the door would bind
        actor = principal.actor
        name = f"scribe-{uuid.uuid4().hex[:8]}"
        await bot_ops.push(session, actor=actor, name=name, source=BotSource(bot_md=BOT_MD.format(name=name)))
        conv = await chat_ops.open_conversation(session, actor, bot_name=name)
        await chat_ops.send(session, actor, conv.id, "what changed in 3.2?")
        await chat_ops.send(session, actor, conv.id, "and 3.1?")
        await session.commit()
        return str(conv.id), token


async def _wire(path: str, token: str | None = None) -> tuple[int, list[dict[str, str | None]]]:
    transport = httpx.ASGITransport(app=create_app())
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=headers)
        if response.status_code != 200:
            return response.status_code, []
        raw = response.text.replace("\r\n", "\n")
    frames: list[dict[str, str | None]] = []
    for block in raw.split("\n\n"):
        frame: dict[str, str | None] = {"event": None, "id": None, "data": None}
        for line in block.splitlines():
            for key in ("event", "id", "data"):
                if line.startswith(f"{key}: "):
                    frame[key] = line.removeprefix(f"{key}: ")
        if frame["data"] is not None:
            frames.append(frame)
    return response.status_code, frames


async def test_message_frames_carry_the_seq_as_their_id_and_parse_as_rows(wired: tuple[str, str]) -> None:
    cid, token = wired
    status, frames = await _wire(f"/v1/conversations/{cid}/events?since=0&follow=false", token)
    assert status == 200
    assert [f["event"] for f in frames] == ["message", "message"]
    assert [f["id"] for f in frames] == ["1", "2"]
    rows = [MessageOut.model_validate_json(f["data"] or "") for f in frames]
    assert [r.text for r in rows] == ["what changed in 3.2?", "and 3.1?"]
    assert all(r.role == "user" and r.verdict == "answer" for r in rows)


async def test_since_replays_past_the_cursor(wired: tuple[str, str]) -> None:
    cid, token = wired
    status, frames = await _wire(f"/v1/conversations/{cid}/events?since=1&follow=false", token)
    assert status == 200 and [f["id"] for f in frames] == ["2"]


async def test_the_stream_is_never_a_way_around_ownership(wired: tuple[str, str]) -> None:
    cid, _ = wired
    stranger = await _login("Bo")
    status, _frames = await _wire(f"/v1/conversations/{cid}/events?since=0&follow=false", stranger)
    assert status == 404  # existence is privileged, exactly as the row's own fetch path says
    status, _frames = await _wire(f"/v1/conversations/{cid}/events?since=0&follow=false")
    assert status == 401
