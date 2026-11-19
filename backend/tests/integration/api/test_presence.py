# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Presence (ADR-062): who is on this task, right now - over the real stream routes.

Every frame here is Lane 2: it rides the task stream with no `id:`, never lands in
`task_events`, and is state, not content. Watchers see each other `join`, re-announce `here`
on the reauth wake (and once, debounced, when a newcomer joins), and `leave` when a stream
closes - including when the client is torn down mid-flight. A client-passed
`?conn=` is validated and replaced with a minted id when it is hostile.

The streams run as background requests on an ASGITransport client, which buffers a response
until it ENDS - so each watcher's text is read after its stream is severed (its credential
logged out, or its Project membership removed), and every stream is cancelled in `finally` so a
failed assertion never hangs the suite.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.conf import settings
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import presence as presence_ops

pytestmark = pytest.mark.integration

_REAUTH_S = 0.5
_WAIT_S = 5.0  # a deadline for a frame that should land in milliseconds, so a lost one fails fast
_MINTED = re.compile(r"^c[0-9a-f]{16}$")


@pytest.fixture
async def client(silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.api, "stream_reauth_seconds", _REAUTH_S)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    # A detached `leave` may still be in flight; let it land before the hub and the engine go.
    await asyncio.gather(*presence_ops._pending, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    await auth_ops.flush_outbound_email()
    await hub.stop()  # the routes registered on the hub; stop its LISTEN before the engine goes


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Person:
    token: str
    email: str
    id: str


@dataclass
class Room:
    ada: Person
    bob: Person
    carol: Person  # in the workspace, not in the Project
    project_id: str
    task_id: str


async def _signup(client: AsyncClient, name: str) -> Person:
    email = f"pres{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/v1/auth/signup", json={"email": email, "name": name, "password": "a-long-enough-password"}
    )
    assert r.status_code == 201, r.text
    token = str(r.json()["token"])
    me = (await client.get("/v1/auth/me", headers=_h(token))).json()
    return Person(token=token, email=email, id=str(me["id"]))


async def _room(client: AsyncClient, *, bob_role: str) -> Room:
    ada, bob, carol = [await _signup(client, n) for n in ("Ada", "Bob", "Carol")]
    ada_me = (await client.get("/v1/auth/me", headers=_h(ada.token))).json()
    workspace_id = uuid.UUID(ada_me["workspace"]["id"])
    async with session_scope(workspace_id) as s:
        for other in (bob, carol):
            await WorkspaceMember.add(s, workspace_id, uuid.UUID(other.id), WorkspaceRole.MEMBER)
        await s.commit()
    for other in (bob, carol):
        switched = await client.put(
            "/v1/auth/session/workspace", headers=_h(other.token), json={"workspace_id": str(workspace_id)}
        )
        assert switched.status_code == 200, switched.text

    project = await client.post(
        "/v1/projects", headers=_h(ada.token), json={"name": f"pres-{uuid.uuid4().hex[:6]}"}
    )
    assert project.status_code == 201, project.text
    project_id = str(project.json()["id"])
    shared = await client.post(
        f"/v1/projects/{project_id}/members",
        headers=_h(ada.token),
        json={"email": bob.email, "role": bob_role},
    )
    assert shared.status_code == 201, shared.text
    created = await client.post(
        "/v1/tasks",
        headers=_h(ada.token),
        json={
            "goal": "plan the launch",
            "clarify": "never",
            "budget": {"usd": "1"},
            "project_id": project_id,
        },
    )
    assert created.status_code == 202, created.text
    return Room(ada=ada, bob=bob, carol=carol, project_id=project_id, task_id=str(created.json()["task_id"]))


def _open(client: AsyncClient, task_id: str, token: str, conn: str) -> asyncio.Task[Response]:
    """A task stream as a browser's EventSource opens it (a ticket minted with the session, then
    `?ticket=`), following live."""

    async def opened() -> Response:
        minted = await client.post(f"/v1/tasks/{task_id}/events/ticket", headers=_h(token))
        assert minted.status_code == 200, minted.text
        ticket = quote(str(minted.json()["ticket"]), safe="")
        return await client.get(
            f"/v1/tasks/{task_id}/events?since=0&ticket={ticket}&conn={quote(conn, safe='')}"
        )

    return asyncio.create_task(opened())


async def _severed(client: AsyncClient, stream: asyncio.Task[Response], token: str) -> Response:
    """End a watcher's stream the way the product does: its credential stops proving (logout),
    and the response FINISHES on its own at the next re-proof."""
    out = await client.post("/v1/auth/logout", headers=_h(token))
    assert out.status_code < 300, out.text
    return await asyncio.wait_for(stream, timeout=_REAUTH_S + 2)


class _Tap:
    """The task's presence frames as the hub fans them out, read while every stream is still open.

    The hub hands each frame to every registered queue in one synchronous pass, so a frame this
    tap has heard has already reached each open watcher's queue. Waiting here for the frame a
    test is about replaces guessing how long it takes to arrive. A stream registers on the hub
    before it says `join`, so hearing a watcher's `join` also means that watcher can hear the room.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = uuid.UUID(task_id)
        self.heard: list[Any] = []
        self._queue: asyncio.Queue[Any] | None = None

    async def __aenter__(self) -> _Tap:
        self._queue = await hub.register(self.task_id)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._queue is not None:
            hub.unregister(self.task_id, self._queue)

    async def until(self, kind: str, *, conn: str | None = None, actor_id: str | None = None) -> Any:
        """The first frame of `kind` (heard earlier or arriving now) matching `conn` / `actor_id`."""

        def matches(frame: Any) -> bool:
            return (
                getattr(frame, "type", "") == f"presence.{kind}"
                and (conn is None or frame.conn == conn)
                and (actor_id is None or str(frame.actor.id) == actor_id)
            )

        async def scan() -> Any:
            assert self._queue is not None, "use the tap as `async with`"
            for frame in self.heard:
                if matches(frame):
                    return frame
            while True:
                frame = await self._queue.get()
                self.heard.append(frame)
                if matches(frame):
                    return frame

        return await asyncio.wait_for(scan(), _WAIT_S)


class _Inbox:
    """The text frames a WebSocket watcher received, awaitable as they arrive."""

    def __init__(self) -> None:
        self.frames: list[str] = []
        self._arrived = asyncio.Condition()

    async def put(self, frame: str) -> None:
        async with self._arrived:
            self.frames.append(frame)
            self._arrived.notify_all()

    async def until(self, wanted: Callable[[dict[str, Any]], bool]) -> None:
        def landed() -> bool:
            return any(wanted(json.loads(f)) for f in self.frames)

        async def wait() -> None:
            async with self._arrived:
                await self._arrived.wait_for(landed)

        await asyncio.wait_for(wait(), _WAIT_S)


async def _cancel(*streams: asyncio.Task[Any]) -> None:
    for stream in streams:
        if not stream.done():
            stream.cancel()
    await asyncio.gather(*streams, return_exceptions=True)


def _frames(body: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            key, sep, value = line.partition(":")
            if sep and key in ("event", "id", "data"):
                frame[key] = value.removeprefix(" ")
        if "event" in frame:
            out.append(frame)
    return out


def _presence(body: str) -> list[tuple[dict[str, str], dict[str, Any]]]:
    return [(f, json.loads(f["data"])) for f in _frames(body) if f["event"].startswith("presence.")]


def _of(
    body: str, kind: str, *, conn: str | None = None, actor_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        data
        for frame, data in _presence(body)
        if frame["event"] == f"presence.{kind}"
        and (conn is None or data["conn"] == conn)
        and (actor_id is None or data["actor"]["id"] == actor_id)
    ]


async def test_two_watchers_see_each_other_join_here_and_leave(client: AsyncClient) -> None:
    room = await _room(client, bob_role="viewer")
    ada_conn, bob_conn = "ada_tab_0001", "bob_tab_0001"
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, ada_conn)
        bob_stream: asyncio.Task[Response] | None = None
        try:
            await tap.until("join", conn=ada_conn)
            bob_stream = _open(client, room.task_id, room.bob.token, bob_conn)
            await tap.until("here", conn=bob_conn)  # Bob joined, and his reauth wake said `here`
            typed = await client.post(
                f"/v1/tasks/{room.task_id}/presence",
                headers=_h(room.ada.token),
                json={"conn": ada_conn, "typing": True, "mode": "steer"},
            )
            assert typed.status_code == 204, typed.text
            await tap.until("typing", conn=ada_conn)

            # A killed tab: the request is torn down mid-flight, and the publisher still says leave.
            await _cancel(bob_stream)
            await tap.until("leave", conn=bob_conn)
            ada = await _severed(client, ada_stream, room.ada.token)
        finally:
            await _cancel(ada_stream, *([bob_stream] if bob_stream else []))

    assert ada.status_code == 200
    joined = _of(ada.text, "join", conn=bob_conn)
    assert joined and joined[0]["actor"] == {"type": "user", "id": room.bob.id, "name": "Bob"}
    assert joined[0]["editor"] is False, "a viewer shows as watching"
    assert _of(ada.text, "here", conn=bob_conn), "Bob re-announced on his reauth wake"
    assert _of(ada.text, "leave", conn=bob_conn), "a torn-down stream still said leave"
    typing = _of(ada.text, "typing", conn=ada_conn)
    assert typing and typing[0]["on"] is True and typing[0]["mode"] == "steer"
    assert all("id" not in frame for frame, _ in _presence(ada.text)), "presence never carries an id:"

    async with session_scope(platform=True) as s:
        stored = await s.scalar(text("select count(*) from task_events where type like 'presence.%'"))
    assert stored == 0


async def test_a_newcomer_learns_the_room_within_a_debounce(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ada is already watching; when Bob joins, her stream answers with one `here` (~250 ms) so
    Bob's page shows her without waiting out a reauth wake. The wake is pushed past the window,
    so only the debounced reply can explain the frame. Bob watches over the WebSocket, whose
    frames the test reads while the socket is still open."""
    monkeypatch.setattr(settings.api, "stream_reauth_seconds", 30.0)
    room = await _room(client, bob_role="editor")
    ada_conn = "ada_tab_0002"
    seen = _Inbox()
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, ada_conn)
        bob_sock: asyncio.Task[None] | None = None
        try:
            await tap.until("join", conn=ada_conn)
            bob_sock = asyncio.create_task(_ws(room.task_id, room.bob.token, "bob_sock_0002", seen))
            # Far inside the 30 s wake: only the debounced reply can deliver this frame.
            await seen.until(
                lambda f: (
                    f["type"] == "presence.here" and f["conn"] == ada_conn and f["actor"]["id"] == room.ada.id
                )
            )
        finally:
            await _cancel(ada_stream, *([bob_sock] if bob_sock else []))


async def test_typing_from_an_editor_is_accepted_and_from_a_viewer_refused(client: AsyncClient) -> None:
    room = await _room(client, bob_role="viewer")
    path = f"/v1/tasks/{room.task_id}/presence"
    body = {"conn": "typing_0001", "typing": True, "mode": "steer"}

    assert (await client.post(path, headers=_h(room.ada.token), json=body)).status_code == 204
    assert (await client.post(path, headers=_h(room.bob.token), json=body)).status_code == 403
    assert (await client.post(path, headers=_h(room.carol.token), json=body)).status_code == 404
    hostile = {**body, "conn": "<script>"}
    assert (await client.post(path, headers=_h(room.ada.token), json=hostile)).status_code == 422
    unknown = {**body, "mode": "shout"}
    assert (await client.post(path, headers=_h(room.ada.token), json=unknown)).status_code == 422
    # Over the rate limit the frame is dropped, not refused: still 204.
    for _ in range(3):
        assert (await client.post(path, headers=_h(room.ada.token), json=body)).status_code == 204


async def test_the_typing_cap_is_per_person_not_per_conn(client: AsyncClient) -> None:
    """The cap matters: a fresh `conn` per POST must not buy a fresh budget. Three
    frames inside one second from one editor, each under its own conn - the third is dropped
    (204, and no NOTIFY: the watcher sees exactly two)."""
    room = await _room(client, bob_role="editor")
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, "ada_tab_0006")
        try:
            await tap.until("join", conn="ada_tab_0006")
            path = f"/v1/tasks/{room.task_id}/presence"
            started = time.monotonic()
            codes = [
                (
                    await client.post(
                        path,
                        headers=_h(room.bob.token),
                        json={"conn": f"bob_tab_000{i}", "typing": True, "mode": "follow_up"},
                    )
                ).status_code
                for i in range(3)
            ]
            spent = time.monotonic() - started
            # The dropped third never reaches NOTIFY, so the second is the last frame to wait for.
            await tap.until("typing", conn="bob_tab_0001")
            ada = await _severed(client, ada_stream, room.ada.token)
        finally:
            await _cancel(ada_stream)

    assert spent < 1.0, f"the three POSTs took {spent:.2f}s - outside one window, the test proves nothing"
    assert codes == [204, 204, 204]
    typed = _of(ada.text, "typing", actor_id=room.bob.id)
    assert [t["conn"] for t in typed] == ["bob_tab_0000", "bob_tab_0001"]


async def test_a_finite_replay_is_a_read_not_a_presence(client: AsyncClient) -> None:
    """`follow=false` (the scriptable flight recorder, SSE or socket) replays and closes: it
    announces nothing, so no other watcher sees it join or leave."""
    room = await _room(client, bob_role="editor")
    sock = _Inbox()
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, "ada_tab_0007")
        try:
            await tap.until("join", conn="ada_tab_0007")
            read = await client.get(
                f"/v1/tasks/{room.task_id}/events?since=0&follow=false&conn=bob_read_0007",
                headers=_h(room.bob.token),
            )
            await asyncio.wait_for(_ws(room.task_id, room.bob.token, "bob_sock_0007", sock, follow=False), 10)
            # A marker after both reads: NOTIFYs arrive in commit order, so once Ada's own frame
            # is heard, any presence the reads had (wrongly) sent has been heard before it.
            marked = await client.post(
                f"/v1/tasks/{room.task_id}/presence",
                headers=_h(room.ada.token),
                json={"conn": "ada_tab_0007", "typing": True, "mode": "steer"},
            )
            assert marked.status_code == 204, marked.text
            await tap.until("typing", conn="ada_tab_0007")
            ada = await _severed(client, ada_stream, room.ada.token)
        finally:
            await _cancel(ada_stream)

    assert read.status_code == 200 and "task.created" in read.text
    assert any('"task.created"' in frame for frame in sock.frames), "the socket replayed the record"
    assert not _presence(read.text)
    assert not any('"presence.' in frame for frame in sock.frames)
    assert not [f for f in tap.heard if str(f.actor.id) == room.bob.id]
    assert not [d for _, d in _presence(ada.text) if d["actor"]["id"] == room.bob.id]


@pytest.mark.parametrize("hostile", ["<script>alert(1)</script>", "x" * 5000], ids=["script", "5kb"])
async def test_a_hostile_conn_never_reaches_a_frame(client: AsyncClient, hostile: str) -> None:
    room = await _room(client, bob_role="viewer")
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, "ada_tab_0003")
        bob_stream: asyncio.Task[Response] | None = None
        try:
            await tap.until("join", conn="ada_tab_0003")
            bob_stream = _open(client, room.task_id, room.bob.token, hostile)
            await tap.until("here", actor_id=room.bob.id)  # a reauth wake re-announced Bob too
            bob = await _severed(client, bob_stream, room.bob.token)
            await tap.until("leave", actor_id=room.bob.id)
            ada = await _severed(client, ada_stream, room.ada.token)
        finally:
            await _cancel(ada_stream, *([bob_stream] if bob_stream else []))

    joined = _of(ada.text, "join", actor_id=room.bob.id)
    assert joined and _MINTED.fullmatch(joined[0]["conn"]), joined
    assert {d["conn"] for d in _of(ada.text, "here", actor_id=room.bob.id)} <= {joined[0]["conn"]}
    for body in (ada.text, bob.text):
        assert hostile not in body and "<script>" not in body


async def test_removing_a_project_member_cuts_their_task_stream(client: AsyncClient) -> None:
    """Removal takes effect at the member's open stream's next re-proof."""
    room = await _room(client, bob_role="editor")
    bob_conn = "bob_tab_0004"
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, "ada_tab_0004")
        bob_stream: asyncio.Task[Response] | None = None
        try:
            await tap.until("join", conn="ada_tab_0004")
            bob_stream = _open(client, room.task_id, room.bob.token, bob_conn)
            # Staying open is the claim, so this window is the assertion, not a wait for an event.
            done, _ = await asyncio.wait({bob_stream}, timeout=_REAUTH_S * 3)
            assert not done, "a member's stream must stay open across passing re-proofs"

            removed = await client.delete(
                f"/v1/projects/{room.project_id}/members/{room.bob.id}", headers=_h(room.ada.token)
            )
            assert removed.status_code < 300, removed.text
            removed_at = time.monotonic()
            # No client disconnect, no cancel: the response must FINISH on its own. wait_for is the guard.
            bob = await asyncio.wait_for(bob_stream, timeout=_REAUTH_S + 2)
            elapsed = time.monotonic() - removed_at
            await tap.until("leave", conn=bob_conn)
            ada = await _severed(client, ada_stream, room.ada.token)
        finally:
            await _cancel(ada_stream, *([bob_stream] if bob_stream else []))

    assert bob.status_code == 200
    assert "task.created" in bob.text, "the replayed record arrived before the severance"
    assert elapsed <= _REAUTH_S + 2, f"severed after {elapsed:.2f}s"
    assert _of(ada.text, "join", conn=bob_conn)
    assert _of(ada.text, "leave", conn=bob_conn), "Ada's stream carried Bob's leave"


# ── the WebSocket framing says the same things ───────────────────────────────────────────────


async def _ws(task_id: str, token: str, conn: str, sent: _Inbox, *, follow: bool = True) -> None:
    """Drive the ASGI app as a WebSocket client that never speaks after connecting, putting
    each text frame the server sends into `sent` as it arrives. Runs until the server closes (a
    `follow=false` replay closes on its own) or the caller cancels it (a socket torn down)."""
    app = create_app()
    connected = False
    silence = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await silence.wait()  # never set: the client says nothing more
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.send":
            await sent.put(str(message.get("text")))

    path = f"/v1/tasks/{task_id}/stream"
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": f"conn={quote(conn, safe='')}&follow={str(follow).lower()}".encode(),
        "headers": [(b"host", b"test"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
    }
    await app(scope, receive, send)


async def test_a_websocket_watcher_joins_and_leaves_too(client: AsyncClient) -> None:
    room = await _room(client, bob_role="editor")
    bob_conn = "bob_sock_0005"
    async with _Tap(room.task_id) as tap:
        ada_stream = _open(client, room.task_id, room.ada.token, "ada_tab_0005")
        bob_sock: asyncio.Task[None] | None = None
        try:
            await tap.until("join", conn="ada_tab_0005")
            bob_sock = asyncio.create_task(_ws(room.task_id, room.bob.token, bob_conn, _Inbox()))
            await tap.until("here", conn=bob_conn)
            await _cancel(bob_sock)  # the socket torn down mid-flight
            await tap.until("leave", conn=bob_conn)
            ada = await _severed(client, ada_stream, room.ada.token)
        finally:
            await _cancel(ada_stream, *([bob_sock] if bob_sock else []))

    joined = _of(ada.text, "join", conn=bob_conn)
    assert joined and joined[0]["editor"] is True
    assert _of(ada.text, "here", conn=bob_conn)
    assert _of(ada.text, "leave", conn=bob_conn)
