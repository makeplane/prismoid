# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen socket, end to end: a ticket minted
through the API, the WebSocket driven at the ASGI level, this process's relay, and a real browser
frame behind a real Machine row - the hijacked Docker exec of the bridge, the sidecar's screencast
and its input.

The page is the witness: a big button counts its clicks into a heading, and the sidecar's own
snapshot reads the count, so "dropped" and "landed" are facts of the remote page, not of the
relay's bookkeeping. A snapshot holds the verb lock (and the sidecar drops screen input under it),
so a click is given `_LAND_S` to land before the count is read.

Tickets are minted with this process's start pinned a minute back (`tickets.PROCESS_STARTED`), so a
ticket minted in the suite's first seconds is never refused as stale.

Needs Docker and the stack; never with a dev instance's worker up (`PRISMOID_WORKER_INSTANCE` set).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import screen as screen_ops
from prismoid.operations import tickets
from prismoid.operations.machines.common import record
from prismoid.schemas.events import MachineControlEnded, MachineWoke
from prismoid.schemas.machines import MachineState, frame_name
from prismoid.schemas.screen import decode, encode

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-d")]

_REAUTH_S = 0.5
_LAND_S = 1.0  # how long a click is given to cross the relay, the bridge and the sidecar
_PAGE = """<!doctype html><title>Screen</title><script>window.n = 0</script>
<button style="position:fixed;left:0;top:0;width:600px;height:400px;font-size:40px"
 onclick="document.getElementById('out').textContent = 'clicks ' + (++window.n)">Hit me</button>
<h1 id=out style="position:fixed;left:0;top:480px;margin:0">clicks 0</h1>"""


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Person:
    token: str
    email: str
    id: str


@dataclass
class World:
    """Ada owns the Project, Bob edits, Cy views; Ada's task has a warm browser Machine on a real frame."""

    ada: Person
    bob: Person
    cy: Person
    workspace_id: uuid.UUID
    project_id: str
    task_id: str
    machine_id: uuid.UUID
    frame: DockerFrame


async def _until(ready: Callable[[], object], within: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not ready():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


class Sock:
    """One browser tab's screen socket at the ASGI level: binary messages up, every server message
    kept. A server close is answered with the disconnect a real server would deliver."""

    def __init__(self, app: FastAPI, task_id: str, ticket: str, *, pane: bool = True) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[MutableMapping[str, Any]] = []
        self.inbox.put_nowait({"type": "websocket.connect"})
        path = f"/v1/tasks/{task_id}/screen"
        query = f"ticket={ticket}" + ("&pane=1" if pane else "")
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "subprotocols": [],
        }
        self.task = asyncio.create_task(app(scope, self.inbox.get, self._send))

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "websocket.close":
            self.inbox.put_nowait({"type": "websocket.disconnect", "code": message.get("code", 1000)})

    @property
    def close_code(self) -> int | None:
        return next((int(m.get("code") or 1000) for m in self.sent if m["type"] == "websocket.close"), None)

    def kinds(self) -> list[str]:
        """What the server sent, in order: `accept`, each binary message's `t`, `close`."""
        out: list[str] = []
        for message in self.sent:
            if message["type"] == "websocket.send" and message.get("bytes") is not None:
                out.append(decode(message["bytes"])[0]["t"])
            else:
                out.append(message["type"].removeprefix("websocket."))
        return out

    def got(self, kind: str) -> list[tuple[dict[str, Any], bytes]]:
        out: list[tuple[dict[str, Any], bytes]] = []
        for message in self.sent:
            if message["type"] == "websocket.send" and message.get("bytes") is not None:
                header, payload = decode(message["bytes"])
                if header["t"] == kind:
                    out.append((header, payload))
        return out

    def words(self) -> list[str]:
        return [h["upstream"] for h, _ in self.got("status") if "upstream" in h]

    def up(self, header: dict[str, Any]) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": encode(header)})

    def up_text(self, text: str) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": text})

    def click(self, x: int = 100, y: int = 100) -> None:
        press = {"t": "pointer", "x": x, "y": y, "button": "left", "clicks": 1, "modifiers": 0}
        self.up({**press, "type": "down", "buttons": 1})
        self.up({**press, "type": "up", "buttons": 0})

    async def hang_up(self) -> None:
        if self.close_code is None:
            self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait({self.task}, timeout=5)
        if not self.task.done():
            self.task.cancel()


@pytest.fixture
def app(silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(settings.api, "stream_reauth_seconds", _REAUTH_S)
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    monkeypatch.setattr(tickets, "PROCESS_STARTED", time.time() - 60)  # never a fresh process's stale ticket
    monkeypatch.setattr(screen_ops, "relay", screen_ops.Relay())  # this test's relay, fresh
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()
    await hub.stop()  # the sockets registered on the hub; stop its LISTEN before the engine goes


async def _signup(client: AsyncClient, name: str) -> Person:
    email = f"screen{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/v1/auth/signup", json={"email": email, "name": name, "password": "a-long-enough-password"}
    )
    assert r.status_code == 201, r.text
    token = str(r.json()["token"])
    me = (await client.get("/v1/auth/me", headers=_h(token))).json()
    return Person(token=token, email=email, id=str(me["id"]))


async def _boot(machine_id: uuid.UUID, epoch: int, task_id: str) -> DockerFrame:
    """A browser frame named as the Machine's for this epoch, with the page open - which starts the
    sidecar daemon and Chromium (the screen's bridge never starts them)."""
    ensure_image("browser")
    frame = DockerFrame(task_id=task_id, profile="browser", name=frame_name(machine_id, epoch))
    await frame.create()
    try:
        await frame.write_file("/work/site/index.html", _PAGE)
        await frame.exec(
            "cd /work/site && (python3 -m http.server 8088 >/dev/null 2>&1 &) && "
            "for i in $(seq 1 40); do curl -sf http://127.0.0.1:8088/index.html >/dev/null && break; sleep 0.1; done"
        )
        opened = await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
        assert opened["ok"], opened
    except BaseException:
        await frame.destroy()
        raise
    return frame


@pytest.fixture
async def world(client: AsyncClient) -> AsyncIterator[World]:
    ada, bob, cy = [await _signup(client, name) for name in ("Ada", "Bob", "Cy")]
    workspace_id = uuid.UUID(
        (await client.get("/v1/auth/me", headers=_h(ada.token))).json()["workspace"]["id"]
    )
    async with session_scope(workspace_id) as s:
        for other in (bob, cy):
            await WorkspaceMember.add(s, workspace_id, uuid.UUID(other.id), WorkspaceRole.MEMBER)
        await s.commit()
    for other in (bob, cy):
        switched = await client.put(
            "/v1/auth/session/workspace", headers=_h(other.token), json={"workspace_id": str(workspace_id)}
        )
        assert switched.status_code == 200, switched.text
    project = await client.post(
        "/v1/projects", headers=_h(ada.token), json={"name": f"screen-{uuid.uuid4().hex[:6]}"}
    )
    assert project.status_code == 201, project.text
    project_id = str(project.json()["id"])
    for other, role in ((bob, "editor"), (cy, "viewer")):
        shared = await client.post(
            f"/v1/projects/{project_id}/members",
            headers=_h(ada.token),
            json={"email": other.email, "role": role},
        )
        assert shared.status_code == 201, shared.text
    created = await client.post(
        "/v1/tasks",
        headers=_h(ada.token),
        json={
            "goal": "update the admin app",
            "clarify": "never",
            "budget": {"usd": "1"},
            "project_id": project_id,
        },
    )
    assert created.status_code == 202, created.text
    task_id = str(created.json()["task_id"])
    machine_id = uuid.uuid4()
    frame = await _boot(machine_id, 1, task_id)
    try:
        async with session_scope(workspace_id) as s:
            now = utcnow()
            s.add(
                Machine(
                    id=machine_id,
                    workspace_id=workspace_id,
                    task_id=uuid.UUID(task_id),
                    driver="docker",
                    profile="browser",
                    state=MachineState.IDLE,
                    state_since=now,
                    last_active_at=now,
                    epoch=1,
                    frame_ref=frame.ref,
                )
            )
            await s.commit()
        yield World(ada, bob, cy, workspace_id, project_id, task_id, machine_id, frame)
    finally:
        await screen_ops.relay.close_machine(machine_id, 1001, "test over")
        await frame.destroy()


async def _ticket(client: AsyncClient, person: Person, world: World, conn: str) -> str:
    r = await client.post(
        f"/v1/tasks/{world.task_id}/screen/ticket", headers=_h(person.token), json={"conn": conn}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert str(body["ticket"]).startswith("st_") and body["expires_in"] == 30
    return str(body["ticket"])


async def _open(app: FastAPI, client: AsyncClient, person: Person, world: World, conn: str) -> Sock:
    """A tab's screen: mint, connect, and wait for the relay's hello (it comes after the door)."""
    sock = Sock(app, world.task_id, await _ticket(client, person, world, conn))
    await _until(lambda: sock.got("hello") or sock.close_code is not None)
    assert sock.close_code is None, f"refused with {sock.close_code}"
    return sock


class SlowSock(Sock):
    """A tab on a link too slow for what it is sent: once `stall` is set, nothing the server sends
    it lands, so the relay's write times out and drops it 4408 (a close still goes through)."""

    def __init__(self, app: FastAPI, task_id: str, ticket: str) -> None:
        self.stall = False
        super().__init__(app, task_id, ticket)

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        if self.stall and message["type"] == "websocket.send":
            await asyncio.Event().wait()
        await super()._send(message)


async def _hang_up(*socks: Sock) -> None:
    for sock in socks:
        await sock.hang_up()


async def _clicks(frame: DockerFrame) -> int:
    snap = await frame.browser({"verb": "snapshot"})
    assert snap["ok"], snap
    found = re.search(r"clicks (\d+)", snap["text"])
    assert found, snap["text"][:400]
    return int(found.group(1))


async def _click_and_count(sock: Sock, frame: DockerFrame) -> int:
    sock.click()
    await asyncio.sleep(_LAND_S)
    return await _clicks(frame)


async def _claim(
    client: AsyncClient, person: Person, world: World, conn: str, *, private: bool
) -> dict[str, Any]:
    r = await client.post(
        f"/v1/tasks/{world.task_id}/screen/control",
        headers=_h(person.token),
        json={"conn": conn, "private": private},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _control(client: AsyncClient, world: World) -> Any:
    body = (await client.get(f"/v1/tasks/{world.task_id}", headers=_h(world.ada.token))).json()
    return body["machine"]["control"]


async def _endings(world: World) -> list[MachineControlEnded]:
    async with session_scope(world.workspace_id) as s:
        events = await TaskEvent.replay(s, uuid.UUID(world.task_id), since=0)
    return [e for e in events if isinstance(e, MachineControlEnded)]


async def _set_machine(world: World, **values: Any) -> None:
    async with session_scope(world.workspace_id) as s:
        row = await Machine.locked(s, world.machine_id)
        assert row is not None
        for name, value in values.items():
            setattr(row, name, value)
        await s.commit()


async def _woke(world: World) -> None:
    """`machine.woke` on the record: what the hub carries to the socket, and the relay's nudge."""
    async with session_scope(world.workspace_id) as s:
        await record(s, uuid.UUID(world.task_id), MachineWoke, turn=2, profile="browser")
        await s.commit()


async def test_a_viewer_watches_and_the_switch_closes_the_screen(
    app: FastAPI, client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anyone who can read the task watches - Cy is a Project viewer. With computer use off, a mint
    is a 409, a ticket minted before the switch opens nothing (accepted, `bye`, 4410), and the open
    socket closes 4410 at its next proof."""
    cy = await _open(app, client, world.cy, world, "cy_tab_0001")
    socks = [cy]
    try:
        await _until(lambda: cy.got("frame"), within=20)
        header, jpeg = cy.got("frame")[0]
        assert header["fmt"] == "jpeg" and jpeg.startswith(b"\xff\xd8")
        assert cy.got("hello")[0][0]["v"] == 1
        spare = await _ticket(client, world.cy, world, "cy_tab_0002")
        monkeypatch.setattr(settings.computer_use, "enabled", False)
        refused = await client.post(
            f"/v1/tasks/{world.task_id}/screen/ticket",
            headers=_h(world.cy.token),
            json={"conn": "cy_tab_0002"},
        )
        assert refused.status_code == 409 and refused.json()["error"]["message"] == "Computer use is off."
        late = Sock(app, world.task_id, spare)
        socks.append(late)
        await _until(lambda: late.close_code is not None)
        assert late.kinds() == ["accept", "bye", "close"] and late.close_code == 4410
        assert [h["reason"] for h, _ in late.got("bye")] == ["computer use is off"]
        await _until(lambda: cy.close_code is not None, within=_REAUTH_S + 3)
        assert cy.close_code == 4410
        assert [h["reason"] for h, _ in cy.got("bye")] == ["computer use is off"]
    finally:
        await _hang_up(*socks)


async def test_switching_off_ends_the_holders_lease_at_the_proof(
    app: FastAPI, client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill switch reaches a holder: the proof ends the lease `disabled`, then the
    socket closes 4410."""
    bob = await _open(app, client, world.bob, world, "bob_tab_0001")
    try:
        await _claim(client, world.bob, world, "bob_tab_0001", private=False)
        monkeypatch.setattr(settings.computer_use, "enabled", False)
        await _until(lambda: bob.close_code is not None, within=_REAUTH_S + 3)
        assert bob.close_code == 4410
        assert [e.reason for e in await _endings(world)] == ["disabled"]
        assert await _control(client, world) is None
    finally:
        await bob.hang_up()


async def test_a_viewers_input_is_dropped_and_the_holders_lands(
    app: FastAPI, client: AsyncClient, world: World
) -> None:
    bob = await _open(app, client, world.bob, world, "bob_tab_0001")
    cy = await _open(app, client, world.cy, world, "cy_tab_0001")
    try:
        await _claim(client, world.bob, world, "bob_tab_0001", private=False)
        assert await _click_and_count(cy, world.frame) == 0
        assert await _click_and_count(bob, world.frame) == 1
    finally:
        await _hang_up(bob, cy)


async def test_a_logout_closes_the_screen_within_a_proof(
    app: FastAPI, client: AsyncClient, world: World
) -> None:
    """The proof re-derives the principal from the credential row the ticket names:
    a logout revokes the row, and the socket closes 4401 within one interval."""
    cy = await _open(app, client, world.cy, world, "cy_tab_0001")
    try:
        out = await client.post("/v1/auth/logout", headers=_h(world.cy.token))
        assert out.status_code == 200, out.text
        await _until(lambda: cy.close_code is not None, within=_REAUTH_S + 3)
        assert cy.close_code == 4401
        assert [h["reason"] for h, _ in cy.got("bye")] == ["unauthorized"]
    finally:
        await cy.hang_up()


async def test_a_deleted_task_closes_its_screen_4404(app: FastAPI, client: AsyncClient, world: World) -> None:
    """The task is cancelled and deleted under an open screen - the next proof finds
    no task and closes the socket 4404, `bye{not found}` first."""
    cy = await _open(app, client, world.cy, world, "cy_tab_0001")
    try:
        cancelled = await client.post(f"/v1/tasks/{world.task_id}/cancel", headers=_h(world.ada.token))
        assert cancelled.status_code == 200, cancelled.text
        deleted = await client.delete(f"/v1/tasks/{world.task_id}", headers=_h(world.ada.token))
        assert deleted.status_code == 200, deleted.text
        await _until(lambda: cy.close_code is not None, within=_REAUTH_S + 3)
        assert cy.close_code == 4404
        assert [h["reason"] for h, _ in cy.got("bye")] == ["not found"]
    finally:
        await cy.hang_up()


async def test_a_holder_removed_from_the_project_loses_the_lease_before_the_close(
    app: FastAPI, client: AsyncClient, world: World
) -> None:
    """Bob holds privately and is removed from the Project: his next proof finds no task, ends his
    lease `demoted` and only then closes 4404 - so Cy is unmasked at once, never held behind the
    30 s grace of someone who can no longer see the screen."""
    bob = await _open(app, client, world.bob, world, "bob_tab_0001")
    cy = await _open(app, client, world.cy, world, "cy_tab_0001")
    try:
        await _claim(client, world.bob, world, "bob_tab_0001", private=True)
        await _until(lambda: [h for h, _ in cy.got("masked") if h["holder"] is not None])
        removed = await client.delete(
            f"/v1/projects/{world.project_id}/members/{world.bob.id}", headers=_h(world.ada.token)
        )
        assert removed.status_code == 200, removed.text
        await _until(lambda: bob.close_code is not None, within=_REAUTH_S + 3)
        assert bob.close_code == 4404 and [h["reason"] for h, _ in bob.got("bye")] == ["not found"]
        assert [e.reason for e in await _endings(world)] == ["demoted"]  # recorded before the close
        assert await _control(client, world) is None
        await _until(lambda: cy.got("masked")[-1][0]["holder"] is None, within=3)
        assert cy.close_code is None
    finally:
        await _hang_up(bob, cy)


async def test_a_demoted_holder_loses_the_lease_and_keeps_watching(
    app: FastAPI, client: AsyncClient, world: World
) -> None:
    bob = await _open(app, client, world.bob, world, "bob_tab_0001")
    try:
        await _claim(client, world.bob, world, "bob_tab_0001", private=True)
        assert (await _control(client, world))["holder"]["name"] == "Bob"
        demoted = await client.patch(
            f"/v1/projects/{world.project_id}/members/{world.bob.id}",
            headers=_h(world.ada.token),
            json={"role": "viewer"},
        )
        assert demoted.status_code == 200, demoted.text
        # The ending is recorded after the lease's own commit (`after_end`), so wait for the record,
        # not for the lease to read empty: the two land a few milliseconds apart.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _REAUTH_S + 3
        while not await _endings(world):
            assert loop.time() < deadline, "the demoted holder kept the lease"
            await asyncio.sleep(0.2)
        assert [e.reason for e in await _endings(world)] == ["demoted"]
        assert await _control(client, world) is None
        assert bob.close_code is None  # a viewer now, still watching
    finally:
        await bob.hang_up()


async def test_a_ticket_opens_one_socket_once(app: FastAPI, client: AsyncClient, world: World) -> None:
    """Single-use: the replay is accepted, told `bye{unauthorized}` and closed 4401 - and hears
    nothing else (no hello, no frame)."""
    ticket = await _ticket(client, world.cy, world, "cy_tab_0001")
    first = Sock(app, world.task_id, ticket)
    await _until(lambda: first.got("hello") or first.close_code is not None)
    second = Sock(app, world.task_id, ticket)
    try:
        await _until(lambda: second.close_code is not None)
        assert first.close_code is None and second.close_code == 4401
        assert second.kinds() == ["accept", "bye", "close"]
        assert [h["reason"] for h, _ in second.got("bye")] == ["unauthorized"]
    finally:
        await _hang_up(first, second)


async def test_the_holder_with_two_tabs_and_a_reload(app: FastAPI, client: AsyncClient, world: World) -> None:
    """Ada claims privately in tab 1 and opens tab 2: tab 2 is a viewer, masked, its
    input dropped. She reloads (tab 3, a new connection) and re-claims inside the grace: the same
    lease, rebound - tab 3 drives, and tab 1's input is dropped from then on."""
    tab1 = await _open(app, client, world.ada, world, "ada_tab_0001")
    tabs = [tab1]
    try:
        first = await _claim(client, world.ada, world, "ada_tab_0001", private=True)
        tab2 = await _open(app, client, world.ada, world, "ada_tab_0002")
        tabs.append(tab2)
        await _until(lambda: tab2.got("masked"))
        assert tab2.got("masked")[0][0]["holder"]["name"] == "Ada"
        await asyncio.sleep(_REAUTH_S)  # past the mask's settle (300 ms)
        assert await _click_and_count(tab2, world.frame) == 0
        assert await _click_and_count(tab1, world.frame) == 1

        tab3 = await _open(app, client, world.ada, world, "ada_tab_0003")
        tabs.append(tab3)
        await _until(lambda: tab3.got("masked"))
        again = await _claim(client, world.ada, world, "ada_tab_0003", private=True)
        assert again["since"] == first["since"]  # the same lease, rebound to the new connection
        await _until(lambda: tab3.got("masked")[-1][0]["holder"] is None)
        await _until(lambda: tab1.got("masked")[-1][0]["holder"] is not None)  # it attached unmasked
        assert tab1.got("masked")[-1][0]["holder"]["name"] == "Ada"
        assert await _click_and_count(tab1, world.frame) == 1  # the old connection no longer drives
        assert await _click_and_count(tab3, world.frame) == 2
        assert tab2.got("frame") == []  # tab 2 never saw a pixel of the private lease
    finally:
        await _hang_up(*tabs)


async def test_a_private_holders_4408_and_reconnect_keep_the_lease_past_the_grace(
    app: FastAPI, client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review I2, for real: Bob drives privately on a link too slow for the holder's frames.
    The relay drops him 4408, which starts the grace; his tab reconnects at once on the SAME
    connection, with no re-claim. The reconnect gives the grace back, so past it the lease is
    still his, running to its cap, and nothing ended."""
    monkeypatch.setattr(settings.computer_use, "screen_write_timeout_s", 0.5)
    monkeypatch.setattr(settings.computer_use, "control_grace_s", 2)
    bob = SlowSock(app, world.task_id, await _ticket(client, world.bob, world, "bob_tab_0001"))
    socks: list[Sock] = [bob]
    try:
        await _until(lambda: bob.got("hello"))
        claimed = await _claim(client, world.bob, world, "bob_tab_0001", private=True)
        bob.stall = True
        bob.up({"t": "hint", "refresh": True})  # a fresh frame, which his link cannot take
        await _until(lambda: bob.close_code is not None, within=20)
        assert bob.close_code == 4408
        back = await _open(app, client, world.bob, world, "bob_tab_0001")
        socks.append(back)
        await asyncio.sleep(2 + 1.5)  # past the grace the drop started
        control = await _control(client, world)
        assert control is not None and control["holder"]["name"] == "Bob"
        assert control["until"] == claimed["cap_at"] and control["since"] == claimed["since"]
        assert await _endings(world) == []
    finally:
        await _hang_up(*socks)


async def test_a_screen_open_across_a_sleep_and_a_wake_then_a_reset(
    app: FastAPI, client: AsyncClient, world: World
) -> None:
    """The Machine sleeps under an open socket, for real (its frame destroyed): the
    viewer hears `asleep` and stays connected. It wakes on a new epoch with a new frame, and
    `machine.woke` brings the screen back on the same socket, announced by `reset`. A reset then
    closes the socket 4404."""
    cy = await _open(app, client, world.cy, world, "cy_tab_0001")
    woke: DockerFrame | None = None
    try:
        await _until(lambda: cy.got("frame"), within=20)
        await _set_machine(world, state=MachineState.ASLEEP, frame_ref=None)
        await world.frame.destroy()
        await _until(lambda: "asleep" in cy.words(), within=20)
        seen = len(cy.got("frame"))

        woke = await _boot(world.machine_id, 2, world.task_id)
        await _set_machine(world, state=MachineState.IDLE, epoch=2, frame_ref=woke.ref)
        await _woke(world)
        await _until(lambda: len(cy.got("frame")) > seen, within=20)
        assert cy.close_code is None and cy.got("reset") and cy.words()[-1] == "live"

        reset = await client.post(f"/v1/tasks/{world.task_id}/machine/reset", headers=_h(world.ada.token))
        assert reset.status_code == 202, reset.text
        await _until(lambda: cy.close_code is not None, within=5)
        assert cy.close_code == 4404 and [h["reason"] for h, _ in cy.got("bye")] == ["reset"]
    finally:
        await cy.hang_up()
        if woke is not None:
            await woke.destroy()


async def test_a_malformed_message_closes_only_its_own_socket(
    app: FastAPI, client: AsyncClient, world: World
) -> None:
    """A text frame gets the policy close (1008), and the relay keeps serving the
    Machine's other viewer."""
    rude = await _open(app, client, world.cy, world, "cy_tab_0001")
    calm = await _open(app, client, world.bob, world, "bob_tab_0001")
    try:
        await _until(lambda: calm.got("frame"), within=20)
        rude.up_text('{"t": "hint"}')
        await _until(lambda: rude.close_code is not None)
        assert rude.close_code == 1008 and [h["reason"] for h, _ in rude.got("bye")] == ["malformed"]
        seen = len(calm.got("frame"))
        calm.up({"t": "hint", "refresh": True})  # one fresh frame proves the upstream still serves
        await _until(lambda: len(calm.got("frame")) > seen)
        assert calm.close_code is None
    finally:
        await _hang_up(rude, calm)
