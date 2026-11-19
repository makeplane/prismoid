# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker proxy's bounds: a slow head ends, connections are capped,
and one frame's socket is made once however many callers race to make it. No daemon: the
upstream is never dialled by what these tests send."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from prismoid.device import docker_proxy
from prismoid.device.docker_journal import Journal
from prismoid.device.frames import FrameRegistry
from prismoid.schemas.device import FolderGrant, FrameSpecIn, GrantsIn
from prismoid.schemas.machines import FrameKind


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="pdl-", dir="/tmp"))  # a Unix socket path must be short
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _status(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    raw = await asyncio.wait_for(reader.read(), 5)
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


async def test_a_slow_head_ends_and_connections_are_capped(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_proxy, "_HEAD_TIMEOUT_S", 0.2)
    monkeypatch.setattr(docker_proxy, "_MAX_CONNECTIONS", 2)
    path = short_dir / "p.sock"
    journal = Journal(short_dir)
    proxy = docker_proxy._Proxy("device-t1", path, str(short_dir / "none.sock"), "t1", [], journal=journal)  # pyright: ignore[reportPrivateUsage]
    await proxy.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"GET /_ping HTTP/1.1\r\n")  # and never the rest
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b"", "the connection ends at the head timeout"
        writer.close()

        monkeypatch.setattr(docker_proxy, "_HEAD_TIMEOUT_S", 5.0)
        held = [await asyncio.open_unix_connection(str(path)) for _ in range(2)]
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_unix_connection(str(path))
        status, body = await _status(reader)
        assert status == 503 and "too many Docker connections" in body["message"]
        for _, w in (*held, (reader, writer)):
            w.close()
    finally:
        await proxy.close()
    assert not path.exists()


async def test_racing_ensures_make_one_socket(short_dir: Path) -> None:
    home = short_dir / "home"
    frame = FrameRegistry(home).create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, docker=True))
    proxies = docker_proxy.DockerProxies(home, upstream=lambda: str(short_dir / "none.sock"))
    await asyncio.gather(*(proxies.ensure(frame) for _ in range(5)))
    socket = docker_proxy.socket_path(home, frame.ref)
    reader, writer = await asyncio.open_unix_connection(str(socket))  # the one served is the one there
    writer.write(b"GET /secrets HTTP/1.1\r\nHost: docker\r\n\r\n")
    await writer.drain()
    status, _ = await _status(reader)
    assert status == 403
    writer.close()
    await proxies.close_all()
    assert not socket.exists()


async def test_a_revoked_folder_leaves_a_running_frames_proxy(short_dir: Path) -> None:
    """What `PUT /frames/{ref}/grants` does to a frame whose socket is being served. The
    proxy is built for the grants of its last `ensure`, so the change is one `ensure` away, and the
    helper's handler makes it: the next bind of that folder is refused at once."""
    home, folder = short_dir / "home", short_dir / "folder"
    folder.mkdir()
    registry = FrameRegistry(home)
    grant = FolderGrant(path=str(folder), mode="rw", name="/grants/folder")
    frame = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, docker=True, grants=[grant]))
    proxies = docker_proxy.DockerProxies(home, upstream=lambda: str(short_dir / "none.sock"))
    await proxies.ensure(frame)

    async def bind_it() -> bytes:
        body = json.dumps({"Image": "a", "HostConfig": {"Binds": [f"{folder}:/w"]}}).encode()
        socket = docker_proxy.socket_path(home, frame.ref)
        reader, writer = await asyncio.open_unix_connection(str(socket))
        head = f"POST /containers/create HTTP/1.1\r\nHost: docker\r\nContent-Length: {len(body)}\r\n\r\n"
        writer.write(head.encode() + body)
        await writer.drain()
        said = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return said

    refused = b"HTTP/1.1 403"
    assert not (await bind_it()).startswith(refused), "granted: on to a daemon that is not there"

    registry.regrant(frame, GrantsIn(grants=[], docker=True))
    await proxies.ensure(frame)

    assert (await bind_it()).startswith(refused)
    await proxies.close_all()


async def test_a_destroyed_frame_is_never_served_again(short_dir: Path) -> None:
    """Review N2: an `ensure` queued behind a frame's destroy (a command racing the DELETE) finds it
    destroyed under the same lock, and so does every later one, until the frame is made anew."""
    home = short_dir / "home"
    frame = FrameRegistry(home).create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, docker=True))
    proxies = docker_proxy.DockerProxies(home, upstream=lambda: str(short_dir / "none.sock"))
    socket = docker_proxy.socket_path(home, frame.ref)
    await proxies.ensure(frame)
    assert socket.exists()
    await asyncio.gather(proxies.destroy(frame.ref), proxies.ensure(frame), proxies.ensure(frame))
    assert not socket.exists(), "the queued ensures served nothing"
    await proxies.ensure(frame)
    assert not socket.exists(), "nor does a later one"
    proxies.revive(frame.ref)  # the frame is made again under its ref
    await proxies.ensure(frame)
    assert socket.exists()
    await proxies.close_all()
