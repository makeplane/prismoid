# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The SSE wire itself - the bytes a browser's EventSource actually sees (promoted by ADR-008).

`test_stream.py` proves replay is exact at the operations layer. This proves the *framing*: every
`data:` payload parses through the discriminated union, the event name IS the discriminator, and
only durable Lane-1 frames carry `id:` - the rule that makes the browser's free reconnect
(`Last-Event-ID`) a cursor instead of a guess. If a delta ever set an `id`, a reconnect after it
would silently skip durable events; that is the failure mode, pinned here against the real
route, in-process.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.session import AuthSession
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import TaskEventAdapter, TaskStatusChanged, is_durable
from prismoid.schemas.ids import TaskId
from prismoid.schemas.plan import TaskStatus

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def quiet_hub() -> AsyncIterator[None]:
    """The route registers on the hub; the hub opens one process-wide LISTEN. Stop it before the
    engine it borrowed is disposed (same reason `test_stream.py` stops it in `finally`)."""
    yield
    await hub.stop()


@pytest.fixture
async def wired() -> tuple[str, str]:
    """A real login, a real task, three durable events - and the session token the CLI sends in its
    Authorization header (a browser opens the same route with a ticket: test_stream_tickets.py)."""
    async with session_scope() as session:
        result = await auth_ops.signup(
            session,
            email=f"u{uuid.uuid4().hex[:12]}@example.com",
            name="Ana",
            password="a-long-enough-password",
        )
        principal = await AuthSession.resolve(session, result.token)
        assert principal is not None and principal.workspace_id is not None
        tenant.bind(session, principal.workspace_id, principal.role)  # what the door would bind
        task = await task_ops.create_task(session, principal.actor, goal="write a memo")
        for status in (TaskStatus.PLANNING, TaskStatus.RUNNING):
            seq = await task.next_seq(session)
            await TaskEvent.append(
                session,
                TaskStatusChanged(seq=seq, task_id=TaskId(task.id), at=utcnow(), status=status),
            )
        await session.commit()
        return str(task.id), result.token


async def _wire(path: str, token: str, headers: dict[str, str] | None = None) -> list[dict[str, str | None]]:
    """GET an SSE route in-process and split the raw bytes into frames."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers={"Authorization": f"Bearer {token}", **(headers or {})})
        assert response.status_code == 200
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
    return frames


async def test_every_frame_parses_and_only_durable_frames_carry_an_id(wired: tuple[str, str]) -> None:
    task_id, token = wired
    frames = await _wire(f"/v1/tasks/{task_id}/events?since=0&follow=false", token)
    assert len(frames) == 3  # task.created + two task.status

    for frame in frames:
        assert frame["data"] is not None
        event = TaskEventAdapter.validate_json(frame["data"])
        assert frame["event"] == event.type, "the SSE event name must be the union discriminator"
        assert (frame["id"] is not None) == is_durable(event), "the §5.5 id rule"
        if is_durable(event):
            assert frame["id"] is not None and int(frame["id"]) == event.seq


async def test_since_is_an_exact_cursor_on_the_wire(wired: tuple[str, str]) -> None:
    task_id, token = wired
    frames = await _wire(f"/v1/tasks/{task_id}/events?since=1&follow=false", token)
    assert [int(f["id"]) for f in frames if f["id"]] == [2, 3], "seq 1 must NOT be redelivered"


async def test_last_event_id_is_the_same_cursor_and_beats_since(wired: tuple[str, str]) -> None:
    """The browser's reconnect header, treated exactly as `?since=` - those five lines in
    `api/stream.py` ARE the reconnect implementation, so they get their own test."""
    task_id, token = wired
    frames = await _wire(
        f"/v1/tasks/{task_id}/events?since=0&follow=false",
        token,
        headers={"Last-Event-ID": "2"},
    )
    assert [int(f["id"]) for f in frames if f["id"]] == [3], "the header is where the client got to"


async def test_from_the_head_nothing_is_left_to_replay(wired: tuple[str, str]) -> None:
    task_id, token = wired
    frames = await _wire(f"/v1/tasks/{task_id}/events?since=3&follow=false", token)
    assert frames == []
