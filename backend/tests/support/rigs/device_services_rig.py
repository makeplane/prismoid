# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the task-servers tests, shared by the
`test_device_services*` files: the helper on its Unix socket, a frame's fenced commands, and a page in
the task's own Prismoid Chrome window. A plain module, not a test file."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pwd
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import unix_connect

from prismoid.ai.frames.cdp_ws import MAX_MESSAGE, WsCdp
from prismoid.conf import settings
from prismoid.device import docker_proxy, frames, procs
from prismoid.device.helper import KEY_HEADER
from prismoid.device.ports import listeners
from prismoid.device.seatbelt import alias_root, cache_root
from prismoid.runner.device_door import create_app
from tests.support.markers import cannot_run

CHROME = settings.local.chrome_path


BLOCKED = "net::ERR_BLOCKED_BY_CLIENT"


USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


IMAGE = "alpine:3.20"


# A dev server of the frame's own: a POST is answered with the body and the header it arrived with.
ECHO = """\
import base64, hashlib, http.server, sys
class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(f"hits-{sys.argv[1]}.log", "a") as log:
            log.write(self.path + "\\n")
        if self.headers.get("Upgrade", "").lower() != "websocket":
            return self.reply(b"<title>echo</title>")
        key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
        self.end_headers()
        self.wfile.flush()
        self.rfile.read(1)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply(body + b"|" + self.headers.get("X-Probe", "").encode())
    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Echo).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Helper:
    home: Path
    key: str
    app: Any
    started: list[tuple[int, int]] = field(default_factory=list)  # (pid, its start time)
    tag: str = field(default_factory=lambda: uuid.uuid4().hex[:8])  # parallel helpers share the caches
    made: list[str] = field(default_factory=list)  # every frame's ref: its cache goes, by exact path

    @property
    def sock(self) -> str:
        return str(self.home / "run" / "helper.sock")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.sock),
            base_url="http://helper",
            headers={KEY_HEADER: self.key},
            timeout=120,
        )

    async def frame(self, task: str, **spec: Any) -> str:
        async with self.client() as c:
            r = await c.post("/frames", json={"task_id": f"{task}-{self.tag}", "kind": "machine", **spec})
        assert r.status_code == 200, r.text
        self.made.append(r.json()["ref"])
        return r.json()["ref"]

    async def run(self, ref: str, command: str, limit: float = 60) -> dict[str, Any]:
        body = {"exec_id": uuid.uuid4().hex, "command": command, "timeout": limit}
        async with self.client() as c:
            r = await c.post(f"/frames/{ref}/exec", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    async def spawn(self, ref: str, command: str) -> int:
        """`command` left running in the frame (`&`): its pid, remembered for teardown."""
        out = await self.run(ref, f"{command} > /dev/null 2>&1 & echo $!")
        pid = int(out["stdout"].split()[-1])
        if (at := procs.started(pid)) is not None:
            self.started.append((pid, at))
        return pid

    async def serve(self, ref: str, port: int, bind: str = "127.0.0.1") -> int:
        pid = await self.spawn(ref, f"exec /usr/bin/python3 -m http.server --bind {bind} {port}")
        await until(lambda: listening(port), f"a server on {port}")
        return pid

    def state(self, ref: str) -> Any:
        return self.app.state.registry.frames[ref]

    async def dial(self, ref: str) -> WsCdp:
        ws = await unix_connect(
            self.sock,
            f"ws://helper/frames/{ref}/cdp",
            additional_headers={KEY_HEADER: self.key},
            max_size=MAX_MESSAGE,
            compression=None,
        )
        return WsCdp(ws)

    def kill_ours(self) -> None:
        """What a test started and the frame did not already kill: SIGKILL by its group, only while
        its start time proves it the one we started."""
        for pid, at in self.started:
            if procs.started(pid) == at:
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)


def listening(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
        return True
    return False


def alive(pid: int, at: int | None = None) -> bool:
    now = procs.started(pid)
    return now is not None and (at is None or now == at)


async def until(check: Any, what: str, seconds: float = 15.0) -> Any:
    for _ in range(int(seconds / 0.05)):
        if found := check():
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"never happened: {what}")


SIX = """\
import http.server, socket, sys
class Six(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = False
class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<title>six</title>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
Six(("::1", int(sys.argv[1])), Page).serve_forever()
"""


STRANGER = """\
import http.server, sys
class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(sys.argv[2], "a") as log:
            log.write(self.path + "\\n")
        body = b"<title>stranger</title>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Page).serve_forever()
"""


def _short_home() -> Path:
    """A short, resolved folder: the helper's socket path must fit sun_path."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="ps", dir="/tmp")))


async def _start(home: Path) -> tuple[Any, uvicorn.Server, asyncio.Task[None]]:
    app = create_app(home, device_name=lambda: "Test Mac", chrome_path=CHROME)
    server = uvicorn.Server(
        uvicorn.Config(
            app, uds=str(home / "run" / "helper.sock"), log_level="critical", timeout_graceful_shutdown=1
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.02)
    return app, server, serving


@pytest.fixture
async def helper(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Helper]:
    home = _short_home()
    monkeypatch.setattr(settings.local, "home", home)
    app, server, serving = await _start(home)
    h = Helper(home, (home / "helper.key").read_text().strip(), app)
    try:
        yield h
    finally:
        with contextlib.suppress(Exception):
            async with h.client() as c:
                for ref in list(app.state.registry.frames):
                    await c.delete(f"/frames/{ref}")
        server.should_exit = True
        await serving
        h.kill_ours()
        for ref in h.made:
            frames.force_rmtree(Path(cache_root(ref)))
            frames.force_rmtree(Path(alias_root(ref)))
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def folder() -> Iterator[Path]:
    """A folder in the user's real home: a grant must be in the home (or on an external disk)."""
    path = Path(os.path.realpath(tempfile.mkdtemp(prefix="prismoid-e3-", dir=USER_HOME)))
    try:
        yield path
    finally:
        subprocess.run(["chmod", "-R", "u+rwx", str(path)], capture_output=True)  # a probe's `chmod 311`
        shutil.rmtree(path, ignore_errors=True)


async def page(cdp: WsCdp) -> str:
    pages = [t for t in (await cdp.call("Target.getTargets"))["targetInfos"] if t["type"] == "page"]
    session = (await cdp.call("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}))[
        "sessionId"
    ]
    await cdp.call("Page.enable", session_id=session)
    return session


def own(helper: Helper, ref: str) -> set[tuple[str, int]]:
    """What the frame listens on now: asked directly, with no cache."""
    pids = helper.state(ref).groups.pids()
    return {(f.address, f.port) for f in listeners(pids)}


@pytest.fixture
def stranger() -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    """A server on 0.0.0.0 that is no frame's, in a process group of its own."""
    port = free_port()
    process = subprocess.Popen(
        ["/usr/bin/python3", "-m", "http.server", "--bind", "0.0.0.0", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield process, port
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


@pytest.fixture
def stranger_on_loopback(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    """Someone else's server on `127.0.0.1`, keeping a log of what reaches it."""
    port, log = free_port(), tmp_path / "stranger.log"
    log.write_text("")
    process = subprocess.Popen(
        ["/usr/bin/python3", "-c", STRANGER, str(port), str(log)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        for _ in range(200):
            if listening(port):
                break
            time.sleep(0.05)
        yield port, log
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def services_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """The user's own Docker CLI, unfenced: only for checks and teardown of our own objects."""
    env = {k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "DOCKER_CONFIG")}
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=check, env=env
    )


@pytest.fixture
def daemon() -> None:
    if docker_proxy.find_upstream() is None or services_docker("info", check=False).returncode != 0:
        cannot_run("no Docker daemon")
