# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device helper's egress proxy: two doors, a list per frame.

The Docker proxy keys a frame by its source IP; on one device every frame is 127.0.0.1, so here
the key is the frame's own proxy credential, `Proxy-Authorization: Basic <ref>:<token>`, which
its commands carry in `HTTP(S)_PROXY` (the token is per frame, in memory only); a request with
none is challenged (407), since git's libcurl sends it only when asked. One more
principal, `chrome`, is the Prismoid Chrome: it is held to the union of the live frames' lists plus
`CHROME_BASE`, the outer layer under each task's own request vetting. A wrong
credential reaches nothing.

The parse, the list check, the resolve-and-vet and the forward are the Docker proxy's own
(`prismoid.egress_relay`); this file is only who is asking, what they may reach, and where a refusal
is kept (the frame's `denials`, which the worker journals as `tool.called ok=false`). Two things
only the device asks of it: loopback, for the addresses a frame itself listens on only
(`ports.FramePorts`; the `chrome` principal, any live frame's - its window's own request vetting
is the inner layer) and never this proxy's own; and a real plain-http forward, so a dev server
reached through it keeps its cookies and request bodies.

A WebSocket from a page is the one request the inner layer never sees: `Fetch` does not pause a
handshake, and a proxy cannot tell Chrome's windows apart. Its `Origin` can: a `chrome` tunnel to
loopback must open with a WebSocket upgrade from a page on a loopback port of the SAME frame as
its target (HMR: a port of its own, or a sibling). Everything else is refused, which means `wss:`
and `https:` to loopback too: their first bytes are TLS, and say nothing of who is asking.

**Two doors** (ADR-078). A page's asset may come from a public host on no list: the inner
layer judges it by `Fetch`'s resource type, which only it can see, and gives the proxy a short pass
for that host and port. Only the `chrome` principal ever reads a pass, so a frame's own commands
never benefit, and it reaches no list, grant or journal. A WebSocket never meets the inner layer, so
it must never meet a pass, and no check on a host can promise that: Chrome sends a `wss:` down an
open HTTP/2 session to its host, through a tunnel made for an image, if the server advertises
extended CONNECT (probed on Chrome 156). So Chrome is given two doors by URL scheme. `https:` comes to
`port`; `ws:`, `wss:` and `http:` come to `socket_port`, where no tunnel takes a pass. A page's
`wss:` then has no session to share, since Chrome keys a session by its proxy. `http:` is at that
door because a WebSocket's 407 reaches no CDP event, so its credential has to be in Chrome's cache
already, and only a plain request can put it there. A cross-origin image's 407 reaches no event
either, so each door is seeded before a page runs (`SEED_URLS`, `chrome._seed_doors`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import secrets
import socket
import time
from collections.abc import Iterable, Mapping
from functools import partial
from typing import Final
from urllib.parse import urlsplit

from prismoid.device.fence_data import CHROME_BASE, REGISTRIES
from prismoid.device.frames import GUARD, DeviceFrameState
from prismoid.device.ports import FramePorts
from prismoid.egress_relay import TunnelGate, loopback_host, relay

LOG = logging.getLogger(__name__)

CHROME = "chrome"
# One request to each door puts `chrome`'s credential in Chrome's cache: this host is on no list, so
# the proxy refuses it, and nothing ever dials it.
SEED_HOST: Final = "proxy-seed.prismoid.invalid"
SEED_URLS: Final = (f"http://{SEED_HOST}/", f"https://{SEED_HOST}/")
ASSET_PASS_S: Final = 60.0  # long enough for a page's queued requests to get a connection
_ASSET_PASSES: Final = 4096


def apply_allow(
    frame: DeviceFrameState, domains: list[str], version: int | None
) -> tuple[int, dict[str, object]]:
    """Replace a frame's list unless `version` is stale, as the Docker proxy does: a push
    not newer than the last applied one is a 409, an unversioned one always applies. The package
    registries stay on every device frame's list."""
    if version is not None and version <= frame.egress_version:
        return 409, {"ok": False, "error": "stale_version", "version": frame.egress_version}
    frame.allow = {*REGISTRIES, *domains}
    guards = [d for d in frame.denials if d.get("kind") == GUARD]
    frame.denials.clear()  # widening forgets what the old list refused, as the Docker proxy does
    frame.denials.extend(guards)  # a plant moved, a server stopped: no list ever allowed those
    if version is not None:
        frame.egress_version = version
    return 200, {"ok": True, "domains": sorted(frame.allow), "version": version}


class DeviceEgress:
    def __init__(
        self,
        frames: Mapping[str, DeviceFrameState],
        base: Iterable[str] = CHROME_BASE,
        ports: FramePorts | None = None,
    ) -> None:
        self._frames = frames
        self.base = frozenset(base)
        self.ports = ports  # None: no loopback port is anyone's
        self.chrome_token = secrets.token_hex(16)
        self._sock: socket.socket | None = None
        self._socket_door: socket.socket | None = None
        self._servers: list[asyncio.Server] = []
        self._passes: dict[tuple[str, int], float] = {}  # (host, port) -> when its pass ends

    @staticmethod
    def _bind() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        return sock

    @property
    def port(self) -> int:
        """The proxy's loopback port, bound on first use: a URL is known before the loop serves."""
        if self._sock is None:
            self._sock = self._bind()
        return self._sock.getsockname()[1]

    @property
    def socket_port(self) -> int:
        """The Prismoid Chrome's second door, for `http:` and every WebSocket. No frame is told it, and
        it is a denied port in every frame's fence: never dialled, never squatted."""
        if self._socket_door is None:
            self._socket_door = self._bind()
        return self._socket_door.getsockname()[1]

    async def serve(self) -> None:
        """Start serving (idempotent)."""
        if not self._servers:
            self.port  # noqa: B018 - binds the socket
            self.socket_port  # noqa: B018 - binds the socket
            self._servers = [
                await asyncio.start_server(self._handle, sock=self._sock),
                await asyncio.start_server(partial(self._handle, tunnels=False), sock=self._socket_door),
            ]

    async def close(self) -> None:
        for server in self._servers:
            server.close()
            with contextlib.suppress(TimeoutError):  # a tunnel still open ends with the process
                await asyncio.wait_for(server.wait_closed(), timeout=1)
        if not self._servers:
            for sock in (self._sock, self._socket_door):
                if sock is not None:
                    sock.close()
        self._servers = []
        self._sock = self._socket_door = None

    def url_for(self, frame: DeviceFrameState) -> str:
        return f"http://{frame.ref}:{frame.token}@127.0.0.1:{self.port}"

    @property
    def chrome_url(self) -> str:
        """The proxy as the Prismoid Chrome is given it (`chrome.Chrome`): only the origin reaches
        Chrome's argv; the `chrome` credential answers the proxy's challenges over CDP."""
        return f"http://{CHROME}:{self.chrome_token}@127.0.0.1:{self.port}"

    @property
    def chrome_socket_origin(self) -> str:
        """The second door, as the Prismoid Chrome is given it (`chrome.Chrome`)."""
        return f"http://127.0.0.1:{self.socket_port}"

    def principal_of(self, _writer: asyncio.StreamWriter, auth: str) -> str | None:
        """The frame ref (or `chrome`) whose credential `auth` carries; "" for a wrong one (it
        reaches nothing), and None for none at all, which challenges the client for one."""
        if not auth:
            return None
        scheme, _, encoded = auth.partition(" ")
        if scheme.lower() != "basic":
            return ""
        try:
            user, sep, token = base64.b64decode(encoded.strip(), validate=True).decode().partition(":")
        except ValueError:  # bad base64, bytes that are no text, or a header that is not ASCII
            return ""
        if not sep or not token:
            return ""
        if user == CHROME:
            expected = self.chrome_token
        else:
            frame = self._frames.get(user)
            expected = frame.token if frame else ""
        return user if expected and hmac.compare_digest(token.encode(), expected.encode()) else ""

    def allowed_for(self, principal: str) -> set[str]:
        if principal == CHROME:
            return {host for frame in self._frames.values() for host in frame.allow} | self.base
        frame = self._frames.get(principal) if principal else None
        return frame.allow if frame else set()

    async def loopback_for(self, principal: str, host: str, port: int) -> list[str]:
        """The addresses to dial for `host:port`, of those the principal's commands (or containers)
        listen on. For the Prismoid Chrome, any live frame's: which window may reach which is the
        request fence's call, and for a WebSocket `_websocket_from_its_frame`'s."""
        if self.ports is None or not principal or port in (self.port, self.socket_port):
            return []
        if principal == CHROME:
            return await self.ports.literals(list(self._frames.values()), host, port)
        frame = self._frames.get(principal)
        return await self.ports.literals([frame], host, port) if frame is not None else []

    def pass_asset(self, host: str, port: int) -> None:
        """The inner layer judged a page's request for `host:port` an asset: the `chrome` principal
        may reach it for a little while. Held here and never in a frame, so it widens nothing."""
        key = host.lower().rstrip("."), port
        self._passes.pop(key, None)
        self._passes[key] = time.monotonic() + ASSET_PASS_S
        if len(self._passes) > _ASSET_PASSES:  # oldest first: a renewed pass has moved to the end
            del self._passes[next(iter(self._passes))]

    def _passed(self, tunnels: bool, principal: str, host: str, port: int, is_connect: bool) -> bool:
        """Only Chrome, only the host and port the inner layer named (never its subdomains), and at
        each door only what it is for: an `https:` tunnel at `port`, an `http:` forward at the other."""
        if principal != CHROME or is_connect is not tunnels:
            return False
        ends = self._passes.get((host.lower().rstrip("."), port))
        return ends is not None and time.monotonic() < ends

    def gate_for(self, principal: str) -> TunnelGate | None:
        return self._websocket_from_its_frame if principal == CHROME else None

    async def _websocket_from_its_frame(self, host: str, port: int, head: bytes) -> bool:
        """`_origin` says which page; the frame that page's port belongs to must hold the target."""
        origin = _origin(head)
        if origin is None or self.ports is None:
            return False
        frames = list(self._frames.values())
        if await self.ports.same_frame(frames, origin, host, port):
            return True
        if (frame := self.ports.owner(frames, *origin)) is not None:
            frame.deny(host, f"a WebSocket from a page of this task to a port that is not its own: {port}")
        return False

    def on_deny(self, principal: str, host: str, reason: str) -> None:
        if host == SEED_HOST:
            return  # refused by design: it asked for nothing, and nothing was dialed
        LOG.warning("DENY %s → %s (%s)", principal or "(no credential)", host, reason)
        frame = self._frames.get(principal) if principal else None
        if frame is not None:
            frame.denials.append({"ref": frame.ref, "host": host, "reason": reason})

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, tunnels: bool = True
    ) -> None:
        await relay(
            reader,
            writer,
            principal_of=self.principal_of,
            allowed_for=self.allowed_for,
            on_deny=self.on_deny,
            loopback=self.loopback_for,
            forward=True,
            tunnel_gate=self.gate_for,
            asset_pass=partial(self._passed, tunnels),
        )


def _origin(head: bytes) -> tuple[str, int] | None:
    """The page a WebSocket handshake came from, if `head` is one - `GET`, `Upgrade: websocket`, one
    `Origin` - and that page is on a loopback host over http."""
    lines = head.decode("latin-1").split("\r\n")
    if not lines[0].startswith("GET "):
        return None
    headers = [line.partition(":") for line in lines[1:] if line]
    values = {name.strip().lower(): [] for name, _, _ in headers}  # pyright: ignore[reportUnknownVariableType]
    for name, _, value in headers:
        values[name.strip().lower()].append(value.strip())  # pyright: ignore[reportUnknownMemberType]
    origins: list[str] = values.get("origin", [])  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if [v.lower() for v in values.get("upgrade", [])] != ["websocket"] or len(origins) != 1:  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        return None
    try:
        parts = urlsplit(origins[0])
        port = parts.port or 80
    except ValueError:
        return None
    host = parts.hostname or ""
    return (host, port) if parts.scheme == "http" and loopback_host(host) else None
