# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the Prismoid Chrome tests, shared by the `test_device_chrome*`
files: a local HTTP server that two made-up hosts (`a.test`, `b.test`) resolve to, and a helper fixture
that runs the real device helper on a Unix socket with the user's installed Google Chrome. It is a plain
module, not a test file; a test is skipped without Chrome by `tests/support/markers.py`."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import socket
import ssl
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.client import unix_connect

from prismoid import egress_relay
from prismoid.ai.frames.cdp_ws import MAX_MESSAGE, WsCdp
from prismoid.conf import settings
from prismoid.device.chrome import Chrome
from prismoid.device.frames import force_rmtree
from prismoid.device.helper import KEY_HEADER
from prismoid.device.seatbelt import alias_root, cache_root
from prismoid.runner.device_door import create_app

CHROME = settings.local.chrome_path


BLOCKED = "net::ERR_BLOCKED_BY_CLIENT"


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


@dataclass
class Site:
    """Every made-up host's pages: each embeds an image from the OTHER host, and records each hit.
    `/link-<tag>` is a page that is one big link to the other host's `/clicked-<tag>`; `/styled` draws
    its style, image, font and script from the other host, each a real file of its type; `/hop-<n>`
    redirects to what `hops` says; `/204` and `/dl` are navigations that never commit; a path in `slow`
    answers that many seconds late and is never cached; `/stay` asks before it lets a person leave; a
    path in `pages` has that for its body; a path in `no_content` answers 204."""

    port: int = 0
    hits: list[tuple[str, str]] = field(default_factory=list)
    hops: dict[str, str] = field(default_factory=dict[str, str])
    slow: dict[str, float] = field(default_factory=dict[str, float])
    pages: dict[str, str] = field(default_factory=dict[str, str])
    no_content: set[str] = field(default_factory=set[str])

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            path = head.split()[1]
            host = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in head.split("\r\n")
                    if line.lower().startswith("host:")
                ),
                "",
            ).split(":")[0]
            self.hits.append((host, path))
            other = "b.test" if host == "a.test" else "a.test"
            for line in head.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    await reader.readexactly(int(line.partition(":")[2]))
            if path in _NO_COMMIT or path in self.no_content:
                await asyncio.sleep(self.slow.get(path, 0))
                writer.write(_NO_COMMIT.get(path, _NO_COMMIT["/204"]))
                await writer.drain()
                writer.close()
                return
            if path in self.hops:
                writer.write(
                    f"HTTP/1.1 302 Found\r\nLocation: {self.hops[path]}\r\nContent-Length: 0\r\n\r\n".encode()
                )
                await writer.drain()
                writer.close()
                return
            if (asset := _ASSETS.get(path.rpartition(".")[2])) is not None:
                kind, data = asset
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nAccess-Control-Allow-Origin: *\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n".encode()
                    + data
                )
                await writer.drain()
                writer.close()
                return
            if path in self.pages:
                inner = self.pages[path]
            elif path == "/framed":
                inner = f"<img src='http://b.test:{self.port}/in-frame.png'>"
            elif path == "/stay":
                inner = "<script>onbeforeunload = e => { e.preventDefault(); e.returnValue = 'x' }</script>"
            elif path.startswith("/styled"):
                base, tag = f"http://{other}:{self.port}", path.removeprefix("/styled")
                inner = (
                    f"<link rel=stylesheet href='{base}/s{tag}.css'><img id=i src='{base}/i{tag}.png'>"
                    f"<style>@font-face {{font-family: F; src: url({base}/f{tag}.woff2)}}</style>"
                    f"<span style='font-family: F'>x</span><script src='{base}/s{tag}.js'></script>"
                )
            elif path.startswith("/link-"):
                inner = (
                    "<a style='position:fixed;left:0;top:0;width:200px;height:200px;display:block' "
                    f"href='http://{other}:{self.port}/clicked-{path.removeprefix('/link-')}'>go</a>"
                )
            else:
                inner = f"<img src='http://{other}:{self.port}/from-{host}.png'>"
            body = f"<html><head><title>{host}{path}</title></head><body>{inner}</body></html>".encode()
            await asyncio.sleep(self.slow.get(path, 0))
            cache = b"no-store" if path in self.slow else b"max-age=3600"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nCache-Control: %b\r\n"
                b"Content-Length: %d\r\n\r\n" % (cache, len(body)) + body
            )
            await writer.drain()
        writer.close()

    def saw(self, host: str, path: str) -> bool:
        return (host, path) in self.hits


_NO_COMMIT: dict[str, bytes] = {
    "/204": b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n",
    "/dl": (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=x.bin\r\nContent-Length: 4\r\n\r\nfile"
    ),
}


_ASSETS: dict[str, tuple[str, bytes]] = {
    "css": ("text/css", b"body { color: rgb(1, 2, 3) }"),
    "png": ("image/png", PNG),
    "woff2": ("font/woff2", b"wOF2"),  # reaching the host is the proof: nothing here draws it
    "js": ("text/javascript", b"window.assetRan = true"),
}


@dataclass
class Helper:
    home: Path
    key: str
    site: Site
    app: Any
    tag: str = field(default_factory=lambda: os.urandom(4).hex())  # parallel helpers share the caches
    made: list[str] = field(default_factory=list[str])  # every frame's ref: its cache goes, by exact path

    @property
    def sock(self) -> str:
        return str(self.home / "run" / "helper.sock")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.sock),
            base_url="http://helper",
            headers={KEY_HEADER: self.key},
        )

    async def _made(self, task: str, spec: dict[str, Any]) -> str:
        async with self.client() as c:
            r = await c.post("/frames", json={"task_id": f"{task}-{self.tag}", "kind": "machine", **spec})
        assert r.status_code == 200, r.text
        self.made.append(r.json()["ref"])
        return r.json()["ref"]

    async def frame(self, task: str, *hosts: str) -> str:
        return await self._made(task, {"profile": "browser", "allow_domains": list(hosts)})

    async def plain_frame(self, task: str) -> str:
        return await self._made(task, {})

    async def dial(self, ref: str, key: str | None = None) -> WsCdp:
        ws = await unix_connect(
            self.sock,
            f"ws://helper/frames/{ref}/cdp",
            additional_headers={KEY_HEADER: self.key if key is None else key},
            max_size=MAX_MESSAGE,
            compression=None,
        )
        return WsCdp(ws)

    async def denials(self, ref: str) -> list[dict[str, str]]:
        async with self.client() as c:
            return (await c.get(f"/frames/{ref}/denials")).json()["denials"]

    def chrome_denials(self, ref: str) -> list[dict[str, str]]:
        """The frame's refusals as the helper holds them, read without a round trip (a poll)."""
        return list(self.app.state.registry.frames[ref].denials)

    @property
    def chrome(self) -> Chrome:
        return self.app.state.chrome


def short_home() -> Path:
    """A short, resolved folder: the helper's socket path must fit sun_path, and `/tmp` is `/private/tmp`."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="pc", dir="/tmp")))


@pytest.fixture
async def helper(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Helper]:
    async def resolve(host: str, port: int) -> tuple[list[str], str | None]:
        return (["127.0.0.1"], None) if host.endswith(".test") else ([], "unresolvable")

    monkeypatch.setattr(egress_relay, "_resolve_and_vet", resolve)
    site = Site()
    server = await asyncio.start_server(site.handle, "127.0.0.1", 0)
    site.port = server.sockets[0].getsockname()[1]
    home = short_home()
    app = create_app(home, device_name=lambda: "Test Mac", chrome_path=CHROME)
    uv = uvicorn.Server(
        uvicorn.Config(
            app, uds=str(home / "run" / "helper.sock"), log_level="critical", timeout_graceful_shutdown=1
        )
    )
    serving = asyncio.create_task(uv.serve())
    while not uv.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.02)
    made: Helper | None = None
    try:
        made = Helper(home, (home / "helper.key").read_text().strip(), site, app)
        yield made
    finally:
        uv.should_exit = True
        await serving
        server.close()
        await server.wait_closed()
        for ref in made.made if made is not None else ():
            force_rmtree(Path(cache_root(ref)))
            force_rmtree(Path(alias_root(ref)))
        shutil.rmtree(home, ignore_errors=True)


async def chrome_page(cdp: WsCdp) -> tuple[str, str]:
    """The task's window's tab, attached: (target, session)."""
    pages = [t for t in (await cdp.call("Target.getTargets"))["targetInfos"] if t["type"] == "page"]
    assert len(pages) == 1, pages
    session = (await cdp.call("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}))[
        "sessionId"
    ]
    await cdp.call("Page.enable", session_id=session)
    return pages[0]["targetId"], session


async def click(cdp: WsCdp | Chrome, session: str, *, button: str = "left", modifiers: int = 0) -> None:
    """A click at the link's corner, on a session of the agent's (`WsCdp`, through the relay) or
    straight through Chrome's own (as the person's hand would)."""
    # The pointer arrives first: a press alone after a navigation can be dropped against the last
    # document's surface.
    events: list[dict[str, Any]] = [{"type": "mouseMoved", "x": 20, "y": 20}]
    for kind in ("mousePressed", "mouseReleased"):
        events.append(
            {"type": kind, "x": 20, "y": 20, "button": button, "clickCount": 1, "modifiers": modifiers}
        )
    for event in events:
        if isinstance(cdp, Chrome):
            await cdp.call("Input.dispatchMouseEvent", event, session=session)
        else:
            await cdp.call("Input.dispatchMouseEvent", event, session_id=session)


async def chrome_laid_out(chrome: Chrome, session: str, path: str) -> None:
    """`path` is the page showing, its link has a box, a frame of it was presented and it has the
    focus. The server having answered says none of these, and a click before them lands on the page
    before, or on nothing. A frame is two animation frames on: the first may be the last document's.
    Focus is emulated: this Chrome may be behind the next one, and no window here is the person's."""
    await chrome.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session=session)
    laid_out = f"location.pathname === {path!r} && document.querySelector('a')?.getClientRects().length > 0"
    presented = (
        f"new Promise(done => {{ if (!({laid_out})) return done(false);"
        " requestAnimationFrame(() => requestAnimationFrame(() => done(document.hasFocus())));"
        " setTimeout(() => done(false), 1000); })"
    )
    for _ in range(200):
        said = await chrome.call(
            "Runtime.evaluate",
            {"expression": presented, "returnByValue": True, "awaitPromise": True},
            session=session,
        )
        if said["result"].get("value"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never laid out: {path}")


async def chrome_eval(cdp: WsCdp, session: str, expression: str) -> Any:
    said = await cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id=session,
    )
    return said["result"].get("value")


def settled(make: str) -> str:
    """A promise of what became of the WebSocket `make` builds: it opened, or it failed."""
    return f"new Promise(done => {{ const s = {make}; s.onopen = () => done('open'); s.onerror = () => done('failed'); }})"


class FenceWatch:
    """The fence's own traffic on Chrome's pipe: which requests it paused and which it failed. A
    refused request reaches no site, so this is the sign it was made at all."""

    def __init__(self, chrome: Chrome) -> None:
        self.paused: dict[str, str] = {}
        self.failed: set[str] = set()
        heard, sent = chrome._dispatch, chrome._fire  # pyright: ignore[reportPrivateUsage]

        def dispatch(msg: dict[str, Any]) -> None:
            if msg.get("method") == "Fetch.requestPaused":
                self.paused[msg["params"]["requestId"]] = msg["params"]["request"]["url"]
            heard(msg)

        def fire(method: str, params: dict[str, Any], *rest: Any) -> None:
            if method == "Fetch.failRequest":
                self.failed.add(params["requestId"])
            sent(method, params, *rest)

        chrome._dispatch = dispatch  # pyright: ignore[reportPrivateUsage]
        chrome._fire = fire  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    def refused(self, path: str) -> bool:
        return any(path in url and request in self.failed for request, url in self.paused.items())


async def until(check: Any, what: str, seconds: float = 10.0) -> Any:
    for _ in range(int(seconds / 0.05)):
        if found := check():
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"never happened: {what}")


_OWN_PAGE = (
    "<html><head><link rel=stylesheet href='http://b.test:{port}/own-{tag}.css'></head><body>"
    "<img src='http://b.test:{port}/own-{tag}.png?leak=SECRET'></body></html>"
)


async def listening(port: int) -> None:
    """A fenced command's server is up: it takes a connection on its port."""
    for _ in range(250):
        with contextlib.suppress(OSError):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"nothing listens on {port}")


async def serve_own_page(helper: Helper, ref: str, tag: str) -> tuple[int, str]:
    """A fenced command writes a page and serves it on the task's own port, the road out of Seatbelt
    that no list should open: (its port, a command that stops it)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        own = probe.getsockname()[1]
    page = _OWN_PAGE.format(port=helper.site.port, tag=tag)
    command = (
        f"printf %s {json.dumps(page)} > own.html;"
        f" nohup /usr/bin/python3 -m http.server --bind 127.0.0.1 {own} >/dev/null 2>&1 & echo $! > own.pid"
    )
    async with helper.client() as c:
        r = await c.post(
            f"/frames/{ref}/exec", json={"exec_id": f"s-{tag}", "command": command, "timeout": 10}
        )
    assert r.status_code == 200 and r.json()["exit_code"] == 0, r.text
    await listening(own)
    return own, "kill $(cat own.pid)"


async def stop_own_page(helper: Helper, ref: str, stop: str) -> None:
    async with helper.client() as c:
        await c.post(f"/frames/{ref}/exec", json={"exec_id": "stop", "command": stop, "timeout": 10})


def tls_context(home: Path, alpn: str = "h2") -> tuple[ssl.SSLContext, str]:
    """A server's TLS context, and the pin that has Chrome trust its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "h.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("h.test")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = home / "h2.pem"
    pem.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)
    context.set_alpn_protocols([alpn])
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return context, base64.b64encode(hashlib.sha256(spki).digest()).decode()


async def at(cdp: WsCdp, session: str, helper: Helper, host: str, path: str) -> None:
    """The page is on `path`, committed as Chrome's own session said."""
    await cdp.call("Page.navigate", {"url": f"http://{host}:{helper.site.port}{path}"}, session_id=session)
    await until(lambda: f"{host}:{helper.site.port}{path}" in str(helper.chrome.tops()), path)


async def hand_over(helper: Helper, ref: str) -> None:
    async with helper.client() as c:
        assert (await c.post(f"/frames/{ref}/hand-over")).status_code == 204


async def person_goes(helper: Helper, ref: str, url: str, page: str | None = None) -> None:
    """A navigation in the person's place: a script's, with no gesture and on the fence's own session,
    since the agent's relay would end the hand-off. The pass cannot tell a person from a page, so
    this stands for either; `real_click` is a person's own."""
    chrome = helper.chrome
    await chrome.call(
        "Runtime.evaluate",
        {"expression": f"location = {url!r}"},
        session=chrome.fence_session(page or chrome.pages_of_task(ref)[0]),
    )


async def real_click(helper: Helper, ref: str, path: str) -> None:
    """A click of the person's, by input Chrome takes for a hand's, on the big link of a `/link-*` page."""
    chrome = helper.chrome
    session = chrome.fence_session(chrome.pages_of_task(ref)[0])
    await chrome_laid_out(chrome, session, path)
    await click(chrome, session)


async def agent_acts(helper: Helper, ref: str) -> None:
    """Anything of the agent's on the door: the pass ends, whatever it asked for."""
    async with helper.client() as c:
        act = {"exec_id": "act", "command": "true", "timeout": 10}
        assert (await c.post(f"/frames/{ref}/exec", json=act)).status_code == 200
