# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A frame's servers are its window's and its proxy credential's loopback, and nobody else's:
which ports a frame's processes listen on, the 2 s
cache and its look-again, the listener beyond loopback that is stopped, the proxy's loopback and
its plain-http forward. `lsof` and the frame's process groups are stood in for here; the real ones
run in `tests/integration/device/test_device_services.py`."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from prismoid import egress_relay
from prismoid.conf import settings
from prismoid.device import docker_proxy, ports
from prismoid.device.cdp_relay import RequestFence
from prismoid.device.docker_policy import FRAME_LABEL
from prismoid.device.egress import CHROME, DeviceEgress
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.device.ports import FramePorts, Listener, listening_ports, parse_lsof
from prismoid.device.procs import ProcessGroups
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind

LSOF_OUT = (
    "p4242\n"
    "f5\n"
    "n127.0.0.1:3000\n"
    "f6\n"
    "n[::1]:5173\n"
    "f7\n"
    "n*:8080\n"
    "p4343\n"
    "f3\n"
    "n192.168.1.5:9000\n"
    "f4\n"
    "n[fe80::1%lo0]:9001\n"
    "f8\n"
    "n127.0.0.1:4000->127.0.0.1:51000\n"  # a connection, not a listener
    "pnot-a-pid\n"
    "f9\n"
    "n127.0.0.1:1\n"  # under no readable pid: skipped
)


def test_parse_lsof_listen_lines() -> None:
    found = parse_lsof(LSOF_OUT)
    assert found == [
        Listener(4242, "127.0.0.1", 3000),
        Listener(4242, "::1", 5173),
        Listener(4242, "*", 8080),
        Listener(4343, "192.168.1.5", 9000),
        Listener(4343, "fe80::1%lo0", 9001),
    ]
    assert [f.loopback for f in found] == [True, True, False, False, False]
    assert [f.where for f in found][1:3] == ["[::1]:5173", "*:8080"]
    assert parse_lsof("") == [] and parse_lsof("garbage\nn:80\n") == []


def test_listening_ports_asks_lsof_about_those_pids_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lsof -nP -a -p <pids> -iTCP -sTCP:LISTEN`, and only the pids asked about count (lsof
    exits 1 when one of them is gone: what it did find stands)."""
    argv = tmp_path / "argv"
    fake = tmp_path / "lsof"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {argv}\nprintf '{LSOF_OUT}'\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ports, "LSOF", str(fake))
    assert listening_ports([4242, 1, 4242]) == {3000, 5173, 8080}
    assert argv.read_text().split() == ["-nP", "-a", "-p", "1,4242", "-iTCP", "-sTCP:LISTEN", "-F", "pn"]
    assert listening_ports([]) == set()  # nothing to ask about: lsof is never run
    monkeypatch.setattr(ports, "LSOF", str(tmp_path / "missing"))
    assert listening_ports([4242]) == set()  # no lsof: no port is anyone's


# ── a frame's ports ──────────────────────────────────────────────────────────────────────────


class Lsof:
    """`ports.listeners`, stood in for: each pid's listeners, and how often it was asked."""

    def __init__(self) -> None:
        self.by_pid: dict[int, list[tuple[str, int]]] = {}
        self.asked = 0

    def __call__(self, pids: list[int] | dict[int, int]) -> list[Listener]:
        self.asked += 1
        return [Listener(pid, a, p) for pid in pids for a, p in self.by_pid.get(pid, [])]


@pytest.fixture
def lsof(monkeypatch: pytest.MonkeyPatch) -> Lsof:
    fake = Lsof()
    monkeypatch.setattr(ports, "listeners", fake)
    return fake


class Groups(ProcessGroups):
    """A frame's process groups, stood in for: its processes (pid -> group), and the signals sent."""

    def __init__(self, path: Path, pids: dict[int, int]) -> None:
        super().__init__(path)
        self.now = pids
        self.sent: list[tuple[int, int | None]] = []

    def __len__(self) -> int:
        return len(set(self.now.values()))

    def pids(self) -> dict[int, int]:
        return dict(self.now)

    def signal(self, sig: int, only: int | None = None) -> None:
        self.sent.append((sig, only))


def _frame(registry: FrameRegistry, task: str, pids: dict[int, int]) -> DeviceFrameState:
    state = registry.create(FrameSpecIn(task_id=task, kind=FrameKind.MACHINE))
    state.groups = Groups(state.base / "fake-groups.json", pids)
    return state


def _sent(frame: DeviceFrameState) -> list[tuple[int, int | None]]:
    assert isinstance(frame.groups, Groups)
    return frame.groups.sent


async def test_a_frames_ports_are_cached_and_looked_at_again_for_an_unknown_one(
    tmp_path: Path, lsof: Lsof, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("127.0.0.1", 3000)]
    seen = FramePorts(registry.frames)
    assert await seen.of(a) == {("127.0.0.1", 3000)}
    lsof.by_pid[10].append(("127.0.0.1", 3001))  # a server started since
    assert await seen.of(a) == {("127.0.0.1", 3000)} and lsof.asked == 1  # within CACHE_S
    monkeypatch.setattr(ports, "_FRESH_S", 0.0)
    assert await seen.literals([a], "127.0.0.1", 3001) == ["127.0.0.1"]  # looked at again, not refused
    assert lsof.asked == 2
    assert await seen.literals([a], "127.0.0.1", 3002) == []  # looked at again, and still not its own
    monkeypatch.setattr(ports, "CACHE_S", 0.0)
    lsof.by_pid[10] = []
    assert await seen.of(a) == frozenset()  # past CACHE_S: looked at afresh


async def test_lookups_of_one_frame_are_one_look(tmp_path: Path, lsof: Lsof) -> None:
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("127.0.0.1", 3000)]
    seen = FramePorts(registry.frames)
    assert await asyncio.gather(*(seen.look(a) for _ in range(5))) == [frozenset({("127.0.0.1", 3000)})] * 5
    assert lsof.asked == 1


async def test_another_frames_and_prismoids_own_ports_are_never_a_frames(tmp_path: Path, lsof: Lsof) -> None:
    registry = FrameRegistry(tmp_path / "home")
    a, b = _frame(registry, "a", {10: 10}), _frame(registry, "b", {20: 20})
    lsof.by_pid[10] = [("127.0.0.1", 3000), ("127.0.0.1", 8100)]
    lsof.by_pid[20] = [("127.0.0.1", 4000)]
    seen = FramePorts(registry.frames, never=lambda: (8100,))
    assert await seen.literals([a], "localhost", 3000) == ["127.0.0.1"]
    assert await seen.literals([a], "localhost", 4000) == []
    assert await seen.literals([a], "127.0.0.1", 8100) == []  # Prismoid's API port, whoever listens on it
    assert await seen.of(a) == {("127.0.0.1", 3000)}
    assert await seen.literals([a, b], "localhost", 4000) == ["127.0.0.1"]  # the Prismoid Chrome's union


async def test_ownership_is_an_address_and_a_port(tmp_path: Path, lsof: Lsof) -> None:
    """C5: macOS lets a frame bind `[::1]:Y` beside a stranger's `127.0.0.1:Y`."""
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("::1", 3000)]
    seen = FramePorts(registry.frames)
    assert await seen.literals([a], "127.0.0.1", 3000) == []
    assert await seen.literals([a], "[::1]", 3000) == ["::1"]
    assert await seen.literals([a], "localhost", 3000) == ["::1"]
    assert await seen.literals([a], "app.localhost", 3000) == ["::1"]
    lsof.by_pid[10].append(("127.0.0.1", 3000))
    seen.forget(a.ref)
    assert await seen.literals([a], "localhost", 3000) == ["127.0.0.1", "::1"]


async def test_a_containers_published_port_is_its_frames(tmp_path: Path, lsof: Lsof) -> None:
    registry = FrameRegistry(tmp_path / "home")
    a, b = _frame(registry, "a", {}), _frame(registry, "b", {})
    asked: list[str] = []

    async def published(ref: str) -> set[tuple[str, int]]:
        asked.append(ref)
        return {("127.0.0.1", 8080)} if ref == a.ref else set()

    seen = FramePorts(registry.frames, published)
    assert await seen.literals([a], "localhost", 8080) == ["127.0.0.1"]
    assert await seen.literals([b], "localhost", 8080) == []
    assert lsof.asked == 0  # no process groups: lsof is never asked
    assert a.ref in asked


async def test_a_server_beyond_loopback_is_stopped_and_the_agent_told(tmp_path: Path, lsof: Lsof) -> None:
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10, 11: 10, 30: 30})
    lsof.by_pid[10] = [("*", 8080), ("127.0.0.1", 3000)]
    lsof.by_pid[11] = [("192.168.1.5", 9000)]  # another of the same group's
    lsof.by_pid[30] = [("::1", 5173)]
    assert await FramePorts(registry.frames).look(a) == {("127.0.0.1", 3000), ("::1", 5173)}
    assert _sent(a) == [(signal.SIGKILL, 10)]  # its group, once, and no other
    assert [d["host"] for d in a.denials] == ["*:8080", "192.168.1.5:9000"]
    assert all(d["reason"] == ports.WIDE_REASON for d in a.denials)
    assert len(a.notices) == 2 and "127.0.0.1" in a.notices[0] and "*:8080" in a.notices[0]


async def test_the_watch_runs_while_a_frame_has_groups(
    tmp_path: Path, lsof: Lsof, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ports, "WATCH_S", 0.01)
    monkeypatch.setattr(ports, "CACHE_S", 0.0)
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("*", 8080)]
    seen = FramePorts(registry.frames)
    seen.watch()
    for _ in range(200):
        if _sent(a):
            break
        await asyncio.sleep(0.01)
    assert _sent(a)[0] == (signal.SIGKILL, 10)  # stopped between its commands
    assert isinstance(a.groups, Groups)
    a.groups.now = {}  # nothing runs on: the watch ends by itself
    for _ in range(200):
        if seen._watch is None or seen._watch.done():  # pyright: ignore[reportPrivateUsage]
            break
        await asyncio.sleep(0.01)
    assert seen._watch is not None and seen._watch.done()  # pyright: ignore[reportPrivateUsage]
    await seen.close()


async def _walked_by_the_watch(
    state: DeviceFrameState, seen: FramePorts, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    swept: list[str] = []
    monkeypatch.setattr(state, "sweep_plants", lambda: swept.append(state.ref))
    seen.watch()
    for _ in range(40):
        if swept:
            break
        await asyncio.sleep(0.01)
    await seen.close()
    return swept


async def test_a_frames_folders_are_walked_while_what_it_left_runs_on(
    tmp_path: Path, lsof: Lsof, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a parked step no command comes along to walk them, for up to a day."""
    monkeypatch.setattr(ports, "WATCH_S", 0.005)
    monkeypatch.setattr(ports, "WALK_EVERY", 2)
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10})
    assert await _walked_by_the_watch(a, FramePorts(registry.frames), monkeypatch) == [a.ref]


async def test_no_walk_runs_under_a_running_command(
    tmp_path: Path, lsof: Lsof, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build's own short-lived link is the walk after it's to judge: moved under it, it fails."""
    monkeypatch.setattr(ports, "WATCH_S", 0.005)
    monkeypatch.setattr(ports, "WALK_EVERY", 2)
    registry = FrameRegistry(tmp_path / "home")
    a = _frame(registry, "a", {10: 10})
    a.journal.start("e1", "make")
    assert await _walked_by_the_watch(a, FramePorts(registry.frames), monkeypatch) == []


# ── the window's request fence ───────────────────────────────────────────────────────────────


async def test_a_window_reaches_its_own_loopback_ports_only(tmp_path: Path, lsof: Lsof) -> None:
    registry = FrameRegistry(tmp_path / "home")
    a, b = _frame(registry, "a", {10: 10}), _frame(registry, "b", {20: 20})
    lsof.by_pid[10] = [("127.0.0.1", 3000), ("::1", 3000), ("127.0.0.1", 8100)]
    lsof.by_pid[20] = [("127.0.0.1", 4000)]
    fence = RequestFence(registry.frames, FramePorts(registry.frames, never=lambda: (8100,)))
    for url in (
        "http://localhost:3000/",
        "http://127.0.0.1:3000/x",
        "http://[::1]:3000/",
        "ws://app.localhost:3000/",
    ):
        assert await fence.allows(a.ref, url), url
    for url in ("http://localhost:4000/", "http://127.0.0.1:8100/v1/tasks", "http://localhost/"):
        assert not await fence.allows(a.ref, url), url
    assert await fence.allows(b.ref, "http://127.0.0.1:4000/")
    assert not await fence.allows(None, "http://127.0.0.1:3000/")  # a service worker: no loopback
    assert [d["host"] for d in a.denials] == ["localhost", "127.0.0.1", "localhost"]
    assert not await RequestFence(registry.frames).allows(a.ref, "http://localhost:3000/")  # no ports


# ── the proxy: loopback, and the plain-http forward ──────────────────────────────────────────


class Upstream:
    """A plain HTTP server on loopback that keeps every request's raw bytes and answers chunked (a
    WebSocket's upgrade is answered 101)."""

    def __init__(self) -> None:
        self.raw: list[bytes] = []
        self.server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        length = next(
            (int(v) for k, _, v in (h.partition(": ") for h in lines) if k.lower() == "content-length"), 0
        )
        body = b""
        if any(h.lower() == "transfer-encoding: chunked" for h in lines):
            while (size := int((await reader.readline()).strip(), 16)) > 0:
                body += (await reader.readexactly(size + 2))[:-2]
            await reader.readline()
        elif length:
            body = await reader.readexactly(length)
        self.raw.append(head + body)
        if b"upgrade: websocket" in head.lower():
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(
            b"HTTP/1.1 201 Created\r\nSet-Cookie: s=1\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    up = Upstream()
    up.server = await asyncio.start_server(up.handle, "127.0.0.1", 0)
    try:
        yield up
    finally:
        up.server.close()
        await up.server.wait_closed()


@pytest.fixture
async def device(tmp_path: Path, lsof: Lsof) -> AsyncIterator[tuple[DeviceEgress, FrameRegistry]]:
    registry = FrameRegistry(tmp_path / "home")
    proxy = DeviceEgress(registry.frames)
    proxy.ports = FramePorts(registry.frames, never=lambda: (8100, proxy.port))
    await proxy.serve()
    try:
        yield proxy, registry
    finally:
        await proxy.close()


def _auth(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


async def _connect(port: int, target: str, auth: str) -> bytes:
    """A CONNECT through the proxy: its answer's head (a tunnel stays open; this closes it)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode())
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    except asyncio.IncompleteReadError as e:
        return e.partial
    finally:
        writer.close()


async def _send(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()


async def test_the_proxy_reaches_a_frames_own_loopback_port_only(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream
) -> None:
    proxy, registry = device
    a, b = _frame(registry, "a", {10: 10}), _frame(registry, "b", {20: 20})
    lsof.by_pid[10] = [("127.0.0.1", upstream.port)]
    for target in (f"127.0.0.1:{upstream.port}", f"localhost:{upstream.port}"):
        answer = await _connect(proxy.port, target, _auth(a.ref, a.token))
        assert answer.startswith(b"HTTP/1.1 200 Connection Established"), answer
    refused = (
        f"CONNECT 127.0.0.1:{upstream.port} HTTP/1.1\r\nProxy-Authorization: {_auth(b.ref, b.token)}\r\n\r\n"
    )
    answer = await _send(proxy.port, refused.encode())
    assert answer.startswith(b"HTTP/1.1 403") and egress_relay.LOOPBACK_REFUSED.encode() in answer
    assert b.denials[-1] == {"ref": b.ref, "host": "127.0.0.1", "reason": egress_relay.LOOPBACK_REFUSED}
    # The Prismoid Chrome: any live frame's port (which window may reach which is its request fence's).
    chrome = _auth(CHROME, proxy.chrome_token)
    answer = await _connect(proxy.port, f"127.0.0.1:{upstream.port}", chrome)
    assert answer.startswith(b"HTTP/1.1 200 Connection Established"), answer
    lsof.by_pid[10].append(("127.0.0.1", 8100))
    for port in (8100, proxy.port):  # the API's, and the proxy's own: never, whoever listens
        answer = await _connect(proxy.port, f"127.0.0.1:{port}", chrome)
        assert answer.startswith(b"HTTP/1.1 403"), port


async def test_plain_http_is_forwarded_whole(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream
) -> None:
    """Method, headers and body go on; `Host` is the vetted URL's; the proxy's credential and the
    hop's own headers never leave; the answer streams back as the server sent it."""
    proxy, registry = device
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("127.0.0.1", upstream.port)]
    body = b'{"x": 1}'
    request = (
        f"POST http://localhost:{upstream.port}/api/items?q=1 HTTP/1.1\r\n"
        f"Host: evil.example\r\n"
        f"Proxy-Authorization: {_auth(a.ref, a.token)}\r\n"
        "Proxy-Connection: keep-alive\r\n"
        "Connection: keep-alive, X-Hop\r\n"
        "X-Hop: gone\r\n"
        "Cookie: session=abc\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    answer = await _send(proxy.port, request)
    assert answer.startswith(b"HTTP/1.1 201 Created") and b"Set-Cookie: s=1" in answer
    assert answer.endswith(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
    sent = upstream.raw[0].decode()
    head, _, got = sent.partition("\r\n\r\n")
    lines = head.split("\r\n")
    assert lines[0] == "POST /api/items?q=1 HTTP/1.1"
    assert f"Host: localhost:{upstream.port}" in lines and "Host: evil.example" not in lines
    assert "Cookie: session=abc" in lines and "Content-Type: application/json" in lines
    assert "Connection: close" in lines
    assert not any(h.lower().startswith(("proxy-", "x-hop", "connection: keep")) for h in lines)
    assert got == body.decode()


async def test_a_chunked_body_is_forwarded_and_ambiguous_framing_refused(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream
) -> None:
    proxy, registry = device
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("127.0.0.1", upstream.port)]
    auth = f"Proxy-Authorization: {_auth(a.ref, a.token)}\r\n"
    chunked = (
        f"PUT http://127.0.0.1:{upstream.port}/up HTTP/1.1\r\n{auth}Transfer-Encoding: chunked\r\n\r\n"
        "4\r\nwiki\r\n5\r\npedia\r\n0\r\n\r\n"
    ).encode()
    assert (await _send(proxy.port, chunked)).startswith(b"HTTP/1.1 201")
    assert upstream.raw[-1].endswith(b"wikipedia")
    for bad in (
        "Transfer-Encoding: chunked\r\nContent-Length: 4\r\n\r\n0\r\n\r\nGET /smuggled HTTP/1.1\r\n\r\n",
        "Content-Length: 4\r\nContent-Length: 5\r\n\r\nabcd",
        "Transfer-Encoding: gzip\r\n\r\n",
        "X-Bad: a\rHost: evil\r\n\r\n",
        " folded\r\n\r\n",
    ):
        answer = await _send(
            proxy.port, f"POST http://127.0.0.1:{upstream.port}/x HTTP/1.1\r\n{auth}{bad}".encode()
        )
        assert answer.startswith(b"HTTP/1.1 400"), bad
    huge = f"GET http://127.0.0.1:{upstream.port}/ HTTP/1.1\r\n{auth}" + "X-Pad: " + "a" * 60_000 + "\r\n"
    answer = await _send(proxy.port, (huge + "X-More: " + "b" * 8000 + "\r\n\r\n").encode())
    assert answer.startswith(b"HTTP/1.1 431")
    assert len(upstream.raw) == 1  # nothing ambiguous reached the server


@pytest.fixture
async def upstream6() -> AsyncIterator[Upstream]:
    up = Upstream()
    up.server = await asyncio.start_server(up.handle, "::1", 0)
    try:
        yield up
    finally:
        up.server.close()
        await up.server.wait_closed()


async def test_the_proxy_dials_only_the_address_the_frame_listens_on(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream, upstream6: Upstream
) -> None:
    """C5's probe: a stranger on `127.0.0.1:Y`, the frame on `[::1]:Y` (the two ports differ here
    only because the test cannot hold both: the frame claims the stranger's port on `::1`)."""
    proxy, registry = device
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("::1", upstream.port), ("::1", upstream6.port)]
    auth = _auth(a.ref, a.token)
    answer = await _connect(proxy.port, f"127.0.0.1:{upstream.port}", auth)
    assert answer.startswith(b"HTTP/1.1 403")
    plain = f"GET http://127.0.0.1:{upstream.port}/stolen HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n"
    assert (await _send(proxy.port, plain.encode())).startswith(b"HTTP/1.1 403")
    named = f"GET http://localhost:{upstream.port}/named HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n"
    assert (await _send(proxy.port, named.encode())).startswith(b"HTTP/1.1 403")  # `::1` only: nothing there
    assert upstream.raw == []  # the stranger heard nothing
    named = f"GET http://localhost:{upstream6.port}/mine HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n"
    assert (await _send(proxy.port, named.encode())).startswith(b"HTTP/1.1 201")
    assert upstream6.raw[0].startswith(b"GET /mine ")


async def test_ipv6_literals_are_parsed_for_both_methods(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream6: Upstream
) -> None:
    """I8: brackets are read before any port."""
    proxy, registry = device
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("::1", upstream6.port)]
    auth = _auth(a.ref, a.token)
    answer = await _connect(proxy.port, f"[::1]:{upstream6.port}", auth)
    assert answer.startswith(b"HTTP/1.1 200 Connection Established")
    plain = f"GET http://[::1]:{upstream6.port}/six HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n"
    assert (await _send(proxy.port, plain.encode())).startswith(b"HTTP/1.1 201")
    assert f"Host: [::1]:{upstream6.port}".encode() in upstream6.raw[-1]
    for bad in ("http://[::1/x", "http://[::1]:nope/x", "http://127.0.0.1:nope/x"):
        answer = await _send(
            proxy.port, f"GET {bad} HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode()
        )
        assert answer.startswith(b"HTTP/1.1 400"), bad
    answer = await _send(
        proxy.port, f"CONNECT [::1:80 HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode()
    )
    assert answer.startswith(b"HTTP/1.1 400")


def _upgrade(origin: str | None, method: str = "GET", upgrade: str = "websocket") -> bytes:
    lines = [f"{method} /hmr HTTP/1.1", "Host: x", "Connection: Upgrade", f"Upgrade: {upgrade}"]
    lines += [f"Origin: {origin}"] if origin is not None else []
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _tunnel(port: int, target: str, auth: str, first: bytes) -> bytes:
    """A CONNECT, then `first` in the tunnel, and everything that comes back."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert head.startswith(b"HTTP/1.1 200"), head
        writer.write(first)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()


async def test_a_loopback_websocket_is_decided_by_its_pages_origin(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream
) -> None:
    """C6: Fetch never pauses a handshake and a proxy cannot tell windows apart: the Origin can.
    A page on one of a frame's own ports may open a socket to that port or a sibling; nothing else."""
    proxy, registry = device
    a = _frame(registry, "a", {10: 10})
    _frame(registry, "b", {20: 20})
    lsof.by_pid[10] = [("127.0.0.1", 5000), ("127.0.0.1", upstream.port)]
    lsof.by_pid[20] = [("127.0.0.1", 6000)]
    chrome = _auth(CHROME, proxy.chrome_token)
    target = f"127.0.0.1:{upstream.port}"
    for origin in (f"http://127.0.0.1:{upstream.port}", "http://127.0.0.1:5000", "http://localhost:5000"):
        answer = await _tunnel(proxy.port, target, chrome, _upgrade(origin))
        assert answer.startswith(b"HTTP/1.1 101"), origin
    seen = len(upstream.raw)
    assert seen == 3
    refused: list[bytes] = [
        _upgrade("http://127.0.0.1:6000"),  # the review's two-window probe: another frame's page
        _upgrade("https://example.com"),
        _upgrade("null"),
        _upgrade(None),
        _upgrade("http://127.0.0.1:5000", method="POST"),
        _upgrade("http://127.0.0.1:5000", upgrade="h2c"),
        b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03",  # TLS: a wss or https handshake
        b"GET / HTTP/1.1\r\nHost: x\r\nOrigin: http://127.0.0.1:5000\r\n\r\n",  # no upgrade
        _upgrade("http://127.0.0.1:5000").replace(b"Host: x", b"Origin: http://127.0.0.1:6000"),  # two
    ]
    for first in refused:
        assert b"101" not in await _tunnel(proxy.port, target, chrome, first), first
    assert len(upstream.raw) == seen  # nothing reached the server
    frame_auth = _auth(a.ref, a.token)
    assert (await _connect(proxy.port, target, frame_auth)).startswith(
        b"HTTP/1.1 200"
    )  # a command's is its own


async def test_a_refused_tunnel_is_recorded_on_the_origins_frame(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream
) -> None:
    proxy, registry = device
    a, b = _frame(registry, "a", {10: 10}), _frame(registry, "b", {20: 20})
    lsof.by_pid[10] = [("127.0.0.1", upstream.port)]
    lsof.by_pid[20] = [("127.0.0.1", 6000)]
    chrome = _auth(CHROME, proxy.chrome_token)
    await _tunnel(proxy.port, f"127.0.0.1:{upstream.port}", chrome, _upgrade("http://127.0.0.1:6000"))
    assert b.denials[-1]["host"] == "127.0.0.1" and "WebSocket" in b.denials[-1]["reason"]
    assert not a.denials


async def test_requests_a_forward_cannot_read_are_refused_not_crashed(
    device: tuple[DeviceEgress, FrameRegistry], lsof: Lsof, upstream: Upstream
) -> None:
    """M1, M2: each of these raised (or passed a non-strict parser) on agent input."""
    proxy, registry = device
    a = _frame(registry, "a", {10: 10})
    lsof.by_pid[10] = [("127.0.0.1", upstream.port)]
    auth = f"Proxy-Authorization: {_auth(a.ref, a.token)}\r\n".encode()
    url = f"http://127.0.0.1:{upstream.port}".encode()
    bad = b"POST " + url + b"/x HTTP/1.1\r\n" + auth + "Content-Length: ²\r\n\r\n".encode()
    assert (await _send(proxy.port, bad)).startswith(b"HTTP/1.1 400")
    bad = b"GET " + url + b"/\xff HTTP/1.1\r\n" + auth + b"\r\n"
    assert (await _send(proxy.port, bad)).startswith(b"HTTP/1.1 400")
    bad = b"GET " + url + b"/ HTTP/1.1\r\n" + auth + b"X-Long: " + b"a" * 70_000 + b"\r\n\r\n"
    assert (await _send(proxy.port, bad)).startswith(b"HTTP/1.1 431")
    for size in (b"0x5", b"+5", b"5_0"):
        chunked = b"POST " + url + b"/c HTTP/1.1\r\n" + auth + b"Transfer-Encoding: chunked\r\n\r\n"
        with contextlib.suppress(TimeoutError, ConnectionResetError):
            await asyncio.wait_for(_send(proxy.port, chunked + size + b"\r\nhello\r\n0\r\n\r\n"), 2)
    assert not any(b"hello" in raw for raw in upstream.raw)


def test_an_unchanged_group_is_not_written_again(tmp_path: Path) -> None:
    """M7: a look is on the event loop, up to ten a second."""
    groups = ProcessGroups(tmp_path / "groups.json")
    groups.pids()
    assert not (tmp_path / "groups.json").exists()


async def test_the_cloud_relay_still_refuses_loopback(
    upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the device's hooks, loopback is a private address as it always was, and a plain
    http request is the bare replay (the Docker proxy's behaviour is unchanged)."""
    denied: list[tuple[str, str, str]] = []

    async def vetted(_host: str, _port: int) -> tuple[list[str], str | None]:
        return ["127.0.0.1"], None

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await egress_relay.relay(
            reader,
            writer,
            principal_of=lambda _w, _a: "frame",
            allowed_for=lambda _p: {"localhost", "127.0.0.1"},
            on_deny=lambda *d: denied.append(d),
        )

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        for target in (f"localhost:{upstream.port}", f"127.0.0.1:{upstream.port}"):
            answer = await _send(port, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
            assert answer.startswith(b"HTTP/1.1 403")
        assert [d[2] for d in denied] == ["private address", "private address"]
        monkeypatch.setattr(egress_relay, "_resolve_and_vet", vetted)
        await _send(port, f"POST http://localhost:{upstream.port}/x HTTP/1.1\r\nCookie: a=1\r\n\r\n".encode())
    assert upstream.raw == [b"POST /x HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"]


# ── the ports a frame's containers publish ───────────────────────────────────────────────────


class FakeDaemon:
    def __init__(self, path: Path, listing: list[dict[str, object]]) -> None:
        self.path, self.listing, self.asked = path, listing, 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        self.asked += 1
        body = json.dumps(self.listing).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()


@pytest.fixture
def short_home() -> Iterator[Path]:
    home = Path(tempfile.mkdtemp(prefix="pp-", dir="/tmp"))
    yield home
    shutil.rmtree(home, ignore_errors=True)


async def test_published_ports_are_the_journaled_running_containers_only(short_home: Path) -> None:
    ref = "device-t1"
    listing: list[dict[str, object]] = [
        {
            "Id": "c1",
            "Labels": {FRAME_LABEL: ref},
            "Ports": [
                {"IP": "127.0.0.1", "PublicPort": 8080, "PrivatePort": 80, "Type": "tcp"},
                {"IP": "::1", "PublicPort": 8081, "PrivatePort": 80, "Type": "tcp"},
                {"PrivatePort": 9, "Type": "tcp"},
                {"PublicPort": 5353, "Type": "udp"},
            ],
        },
        {"Id": "forged", "Labels": {FRAME_LABEL: ref}, "Ports": [{"PublicPort": 2222, "Type": "tcp"}]},
        {"Id": "c2", "Labels": {FRAME_LABEL: "device-other"}, "Ports": [{"PublicPort": 3333, "Type": "tcp"}]},
    ]
    sock = short_home / "docker.sock"
    daemon = FakeDaemon(sock, listing)
    server = await asyncio.start_unix_server(daemon.handle, str(sock))
    dockers = docker_proxy.DockerProxies(short_home, upstream=lambda: str(sock))
    async with server:
        assert await dockers.published(ref) == set() and daemon.asked == 0  # made nothing: Docker not woken
        dockers.journal.record(ref, "containers", "c1")
        dockers.journal.record(ref, "containers", "c2")  # journaled, but wearing another frame's label
        assert await dockers.published(ref) == {("127.0.0.1", 8080), ("::1", 8081)}
    assert await dockers.published(ref) == set()  # Docker gone: no ports
    await dockers.close_all()


async def test_neither_proxy_door_can_be_a_frames_port(tmp_path: Path) -> None:
    """The fence denies a frame the socket door's bind as well, so only this sees the list itself."""
    app = create_app(tmp_path / "home", device_name=lambda: "Test Mac", chrome_path="/unused")
    egress = app.state.egress
    try:
        never = set(app.state.ports._never())  # pyright: ignore[reportPrivateUsage]
        assert {settings.local.api_port, egress.port, egress.socket_port} <= never
    finally:
        await egress.close()
