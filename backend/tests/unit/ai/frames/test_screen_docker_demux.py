# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker screen stream's bytes, with no Docker: the demultiplexer split anywhere
with stderr interleaved, the stream's framing both ways over a socket pair, and
`open_docker_screen` against a fake Engine API on a unix socket - the docker SDK's real hijack,
the bytes its HTTP client reads ahead, and a missing container."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator

import docker
import pytest
from docker.errors import DockerException

from prismoid.ai.frames import docker_driver
from prismoid.ai.frames.screen import BRIDGE_CMD, DockerDemux, DockerScreen, ScreenClosed, open_docker_screen
from prismoid.schemas.screen import encode


def _mux(stream: int, data: bytes) -> bytes:
    """One frame of Docker's multiplexed stdio."""
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def _joined(pieces: list[tuple[int, bytes]], stream: int) -> bytes:
    return b"".join(chunk for s, chunk in pieces if s == stream)


RAW = _mux(1, b"hello ") + _mux(2, b"Traceback (most recent call last)") + _mux(1, b"") + _mux(1, b"world")
JPEG = b"\xff\xd8\xff\xe0 a frame"


def test_every_split_point_demuxes_to_the_same_streams() -> None:
    for cut in range(len(RAW) + 1):
        demux = DockerDemux()
        pieces = demux.feed(RAW[:cut]) + demux.feed(RAW[cut:])
        assert _joined(pieces, 1) == b"hello world", cut
        assert _joined(pieces, 2) == b"Traceback (most recent call last)", cut


def test_byte_by_byte_keeps_stderr_between_the_stdout_frames() -> None:
    demux = DockerDemux()
    pieces: list[tuple[int, bytes]] = []
    for i in range(len(RAW)):
        pieces += demux.feed(RAW[i : i + 1])
    first_err = next(i for i, (s, _) in enumerate(pieces) if s == 2)
    assert _joined(pieces[:first_err], 1) == b"hello "
    assert _joined(pieces, 1) == b"hello world"


def test_bytes_that_are_not_dockers_framing_are_refused() -> None:
    with pytest.raises(ValueError):
        DockerDemux().feed(b"\x07\x00\x00\x00\x00\x00\x00\x01x")  # no such stream
    with pytest.raises(ValueError):
        DockerDemux().feed(b"\x01\x00\x09\x00\x00\x00\x00\x01x")  # padding that is not zero


async def test_a_docker_screen_reads_framed_stdout_drops_stderr_and_writes_raw_stdin() -> None:
    """Stdout carries the wire, split across Docker frames, its first bytes read ahead by the
    SDK's HTTP client; stderr is dropped; stdin is not multiplexed."""
    ours, theirs = socket.socketpair()
    theirs.setblocking(False)
    loop = asyncio.get_running_loop()
    hello = encode({"t": "hello", "v": 1})
    frame = encode({"t": "frame", "seq": 1, "ts": 1727.5, "w": 1280, "h": 900, "fmt": "jpeg"}, JPEG)
    stream = await DockerScreen.attach(ours, early=_mux(1, hello[:3]))
    try:
        await loop.sock_sendall(
            theirs, _mux(1, hello[3:]) + _mux(2, b"warning: x") + _mux(1, frame[:10]) + _mux(1, frame[10:])
        )
        first, _ = await asyncio.wait_for(stream.recv(), 2)
        second, payload = await asyncio.wait_for(stream.recv(), 2)
        assert first["t"] == "hello" and first["v"] == 1
        assert second["t"] == "frame" and second["seq"] == 1 and payload == JPEG
        await stream.send({"t": "hint", "fps": 5})
        assert await asyncio.wait_for(loop.sock_recv(theirs, 4096), 2) == encode({"t": "hint", "fps": 5})
        theirs.close()  # the bridge exited
        with pytest.raises(ScreenClosed):
            await asyncio.wait_for(stream.recv(), 2)
    finally:
        await stream.close()
        theirs.close()


async def test_bytes_that_break_the_wire_close_the_stream() -> None:
    ours, theirs = socket.socketpair()
    theirs.setblocking(False)
    stream = await DockerScreen.attach(ours)
    try:
        await asyncio.get_running_loop().sock_sendall(theirs, _mux(1, (5000).to_bytes(4, "big") + b"{}"))
        with pytest.raises(ScreenClosed):  # a header over 4096 bytes
            await asyncio.wait_for(stream.recv(), 2)
    finally:
        await stream.close()
        theirs.close()


async def test_a_bridge_that_never_reads_makes_send_screen_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck peer never holds the relay: each write is bounded (5 s; 0.2 s here). The message
    is bigger than the kernel's buffer and asyncio's high-water mark together, so the drain waits."""
    monkeypatch.setattr("prismoid.ai.frames.screen.SEND_TIMEOUT_S", 0.2)
    ours, theirs = socket.socketpair()
    stream = await DockerScreen.attach(ours)
    try:
        with pytest.raises(ScreenClosed):
            await asyncio.wait_for(stream.send({"t": "hint"}, b"\0" * (2 << 20)), 3)
    finally:
        await stream.close()
        theirs.close()


# ── open_docker_screen against a fake Engine API: the docker SDK's real hijack ──────────────────


def _content_length(lines: list[str]) -> int:
    for line in lines:
        name, _, value = line.partition(":")
        if name.lower() == "content-length":
            return int(value)
    return 0


class FakeEngine:
    """The Docker Engine API on a unix socket, for the two calls a screen makes: exec create (a
    404 when the container is `missing`) and exec start, hijacked with a `101` whose headers go out
    in the same write as the bridge's first bytes, so the SDK's HTTP client reads them ahead."""

    def __init__(self, sock_path: str, *, first: bytes = b"", missing: bool = False) -> None:
        self.first, self.missing = first, missing
        self.requests: list[tuple[str, bytes]] = []
        self.stdin = bytearray()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(sock_path)
        self._server.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = self._server.accept()
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, contextlib.suppress(OSError):
            buf = b""
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(1 << 16)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                lines = head.decode().split("\r\n")
                length = _content_length(lines)
                while len(buf) < length:
                    buf += conn.recv(1 << 16)
                body, buf = buf[:length], buf[length:]
                path = lines[0].split(" ")[1]
                self.requests.append((path, body))
                if path.endswith("/exec"):
                    status = "404 Not Found" if self.missing else "201 Created"
                    reply = b'{"message": "No such container"}' if self.missing else b'{"Id": "exec-1"}'
                    conn.sendall(
                        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(reply)}\r\n\r\n".encode()
                        + reply
                    )
                    continue
                conn.sendall(
                    b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.multiplexed-stream\r\n"
                    b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n" + self.first
                )
                while chunk := conn.recv(1 << 16):
                    self.stdin += chunk
                return


@pytest.fixture
def engine_dir() -> Iterator[str]:
    # AF_UNIX paths are capped at ~104 bytes on macOS: a short /tmp directory, as the sidecar suite does.
    path = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _engine_client(monkeypatch: pytest.MonkeyPatch, sock_path: str) -> None:
    client = docker.DockerClient(base_url=f"unix://{sock_path}", version="1.44")
    monkeypatch.setattr(docker_driver, "docker_client", lambda: client)


async def test_open_docker_screen_hijacks_the_exec_and_keeps_the_read_ahead_bytes(
    monkeypatch: pytest.MonkeyPatch, engine_dir: str
) -> None:
    sock_path = f"{engine_dir}/docker.sock"
    hello = encode({"t": "hello", "v": 1})
    status = encode({"t": "status", "chromium": "running", "busy": False})
    engine = FakeEngine(sock_path, first=_mux(1, hello) + _mux(1, status))
    _engine_client(monkeypatch, sock_path)
    stream = await open_docker_screen("machine-x-1")
    try:
        # Both messages arrived with the 101's headers: lost, without the read-ahead, and the stream
        # would start mid-frame.
        first, _ = await asyncio.wait_for(stream.recv(), 5)
        second, _ = await asyncio.wait_for(stream.recv(), 5)
        assert (first["t"], second["t"]) == ("hello", "status")
        await stream.send({"t": "hint", "fps": 5})
        for _ in range(200):
            if bytes(engine.stdin) == encode({"t": "hint", "fps": 5}):
                break
            await asyncio.sleep(0.01)
        assert bytes(engine.stdin) == encode({"t": "hint", "fps": 5})
    finally:
        await stream.close()
        engine.close()
    (create_path, create_body), (start_path, _) = engine.requests
    assert create_path.endswith("/containers/machine-x-1/exec") and start_path.endswith("/exec/exec-1/start")
    spec = json.loads(create_body)
    assert spec["Cmd"] == BRIDGE_CMD and spec["User"] == "1001" and spec["WorkingDir"] == "/home/browser"
    assert spec["AttachStdin"] is True and spec["Tty"] is False
    assert sorted(spec["Env"]) == ["HOME=/home/browser", "PYTHONPATH=/opt/plane/browser"]


async def test_a_missing_container_is_screen_closed(monkeypatch: pytest.MonkeyPatch, engine_dir: str) -> None:
    sock_path = f"{engine_dir}/docker.sock"
    engine = FakeEngine(sock_path, missing=True)
    _engine_client(monkeypatch, sock_path)
    try:
        with pytest.raises(ScreenClosed, match="gone"):
            await open_docker_screen("machine-gone-1")
    finally:
        engine.close()


async def test_an_unreachable_docker_daemon_is_screen_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No daemon to ask (the client's version negotiation fails): no screen, the relay retries."""

    def unreachable() -> docker.DockerClient:
        raise DockerException("Error while fetching server API version")

    monkeypatch.setattr(docker_driver, "docker_client", unreachable)
    with pytest.raises(ScreenClosed, match="refused"):
        await open_docker_screen("machine-x-1")
