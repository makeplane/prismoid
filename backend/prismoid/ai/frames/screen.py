# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's upstream: one framed stream, both ways, between the API's relay
and a Machine's browser sidecar. Persistence-free like the rest of `ai/`: the caller
(`operations/machines/screen.py`) reads the Machine's row under its workspace fence and hands over the
frame reference; this module never sees a row, a lease or a viewer.

**Docker (dev).** A hijacked exec of the bridge (`python3 -m sidecar.screen`) as the `browser`
user: `exec_create` with stdin attached and no TTY, then `exec_start(socket=True)`. Docker
multiplexes the exec's stdout and stderr over that one socket in 8-byte-headed frames
(`DockerDemux`); stdin is not multiplexed, so `send` writes the wire's bytes as they are. The
Docker socket in the API process is a dev-only concession.

**sandbar** (`open_sandbar_screen`, the display route). sandbar serves
`GET /v1/sandboxes/{id}/display`, a plain HTTP upgrade (`Upgrade: sandbar-display`, not a
WebSocket) admitted only by a display ticket the caller mints; after `101` the connection is a raw
pipe to the same bridge, already demultiplexed, so each header's `n` delimits the messages. The
master token never rides it: this module takes the ticket and the URL as arguments.

**The doors**: `DOORS` maps each driver to its way in, and each door
obtains its own upstream credential - Docker's needs none, sandbar's mints a `dt_` per open through
the minter its caller hands it. A door may also serve the lease holder on a control stream of its own
and end a person's hold at its provider (`ScreenDoor.kinds`, `hand_back`); a distribution adds its
driver's door as one more row."""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import ssl
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from urllib.parse import quote, urlsplit

import logfire

from prismoid.ai import config
from prismoid.ai.frames.layout import BROWSER_UID
from prismoid.schemas.screen import StreamDecoder, WireError, encode

BRIDGE_CMD: Final = ["python3", "-m", "sidecar.screen"]
BRIDGE_ENV: Final = {"PYTHONPATH": "/opt/plane/browser", "HOME": "/home/browser"}
BRIDGE_WORKDIR: Final = "/home/browser"

STDOUT: Final = 1
STDERR: Final = 2
_MUX_HEADER: Final = 8
READ_CHUNK: Final = 1 << 18
SEND_TIMEOUT_S: Final = 5.0


class ScreenClosed(Exception):
    """The upstream ended, or never opened: EOF, bytes the codec refused, a frame that is missing
    or not running. The relay re-reads the Machine's row and decides."""


class ScreenStream(Protocol):
    """A framed stream to one Machine's screen: whole `(header, payload)` messages both ways."""

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        """The next message down; `ScreenClosed` at EOF or on any error."""
        ...

    async def send(self, header: Mapping[str, Any], payload: bytes = b"") -> None:
        """One message up; `ScreenClosed` when the upstream is gone or will not take it in 5 s."""
        ...

    async def close(self) -> None:
        """Idempotent; a pending `recv` ends in `ScreenClosed`."""
        ...


class DockerDemux:
    """Docker's multiplexed stdio (`tty=False`): `[stream][0][0][0][u32 BE size][size bytes]`,
    repeated. Fed the socket's bytes however they were split; returns `(stream, chunk)` pieces in
    order (one Docker frame may come back as several pieces). A first byte that is not stdin,
    stdout or stderr, or non-zero padding, is not Docker's framing: `ValueError`."""

    def __init__(self) -> None:
        self._head = bytearray()
        self._stream = 0
        self._left = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        out: list[tuple[int, bytes]] = []
        view = memoryview(data)
        while view:
            if self._left:
                piece = view[: self._left]
                self._left -= len(piece)
                view = view[len(piece) :]
                out.append((self._stream, bytes(piece)))
                continue
            need = _MUX_HEADER - len(self._head)
            self._head += view[:need]
            view = view[need:]
            if len(self._head) < _MUX_HEADER:
                break
            if self._head[0] > STDERR or any(self._head[1:4]):
                raise ValueError("not Docker's stream framing")
            self._stream, self._left = self._head[0], int.from_bytes(self._head[4:8], "big")
            self._head.clear()
        return out


class DockerScreen:
    """`ScreenStream` over a hijacked exec's socket, which asyncio owns from `attach` on."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        raw: socket.socket,
        *,
        early: bytes = b"",
        hijacked: Any = None,
    ) -> None:
        self._reader, self._writer, self._raw = reader, writer, raw
        self._early = early
        # The SDK's socket object: the HTTP client's file object still holds a reference to the
        # fd, so closing it (in `close`) is what really closes the connection.
        self._hijacked = hijacked
        self._demux = DockerDemux()
        self._decoder = StreamDecoder()
        self._ready: deque[tuple[dict[str, Any], bytes]] = deque()
        self._closed = False

    @classmethod
    async def attach(cls, raw: socket.socket, *, early: bytes = b"", hijacked: Any = None) -> DockerScreen:
        try:
            reader, writer = await asyncio.open_connection(sock=raw, limit=READ_CHUNK * 4)
        except OSError as e:
            raise ScreenClosed(f"could not attach to the bridge: {e}") from e
        return cls(reader, writer, raw, early=early, hijacked=hijacked)

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        while not self._ready:
            if self._closed:
                raise ScreenClosed("closed")
            data, self._early = self._early, b""
            if not data:
                try:
                    data = await self._reader.read(READ_CHUNK)
                except OSError as e:
                    raise ScreenClosed(f"the bridge's socket failed: {e}") from e
                if not data:
                    raise ScreenClosed("the bridge ended")
            try:
                for stream, chunk in self._demux.feed(data):
                    if stream == STDOUT:
                        self._ready.extend(self._decoder.feed(chunk))
                    else:  # the bridge never writes stderr on purpose: a Python traceback, at worst
                        logfire.debug("screen bridge wrote {n} bytes to stderr", n=len(chunk))
            except ValueError as e:  # Docker's framing broken, or a WireError (one of these)
                raise ScreenClosed(f"refused the bridge's bytes: {e}") from e
        return self._ready.popleft()

    async def send(self, header: Mapping[str, Any], payload: bytes = b"") -> None:
        """`WireError` (a caller's oversize message) propagates; a dead upstream is `ScreenClosed`."""
        if self._closed:
            raise ScreenClosed("closed")
        self._writer.write(encode(header, payload))  # stdin is not multiplexed
        try:
            await asyncio.wait_for(self._writer.drain(), SEND_TIMEOUT_S)
        except OSError as e:  # a reset, or the 5 s timeout (a TimeoutError is an OSError)
            raise ScreenClosed(f"the bridge would not take a message: {e}") from e

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self._raw.shutdown(socket.SHUT_RDWR)  # the bridge sees EOF now, whoever else holds the fd
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()
        if self._hijacked is not None:
            with contextlib.suppress(Exception):
                self._hijacked.close()


def _raw_socket(hijacked: Any) -> socket.socket:
    """`exec_start(socket=True)` answers with the HTTP connection's `SocketIO` when the Docker API
    is a unix socket (Linux, and Docker Desktop or OrbStack on macOS) or plain TCP; its `_sock` is
    the connected socket asyncio can adopt. TLS (an `ssl.SSLSocket`) is refused: asyncio cannot
    adopt one, and dev Docker never speaks it."""
    raw = hijacked if isinstance(hijacked, socket.socket) else getattr(hijacked, "_sock", None)
    if not isinstance(raw, socket.socket) or isinstance(raw, ssl.SSLSocket):
        raise ScreenClosed(f"the screen needs the Docker API on a unix socket, not {type(hijacked).__name__}")
    return raw


def _read_ahead(hijacked: Any) -> bytes:
    """Bytes the SDK's HTTP client buffered past the `101 UPGRADED` headers. Reading the status
    line can pull the bridge's first bytes into that buffer, and asyncio reads the socket, never
    the buffer, so they are taken here and fed first. The socket is non-blocking by now, so `peek`
    never waits: it returns what is buffered, one read's worth, or nothing."""
    response = getattr(hijacked, "_response", None)
    buffered = getattr(getattr(getattr(response, "raw", None), "_fp", None), "fp", None)
    if buffered is None or not hasattr(buffered, "peek"):
        return b""
    try:
        early = buffered.peek(READ_CHUNK)
        return bytes(buffered.read(len(early))) if early else b""
    except (OSError, ValueError):
        return b""


def _hijack_sync(frame_ref: str) -> tuple[Any, socket.socket, bytes]:
    """Blocking (the SDK is synchronous; run in a thread). The SDK's socket object, the connected
    socket under it (non-blocking from here on), and any bytes read ahead."""
    from docker.errors import NotFound

    from prismoid.ai.frames.docker_driver import docker_client

    try:
        api = docker_client().api  # inside: an unreachable daemon is no screen, like a refusal
        exec_id = api.exec_create(
            frame_ref,
            list(BRIDGE_CMD),
            stdout=True,
            stderr=True,
            stdin=True,
            tty=False,
            user=str(BROWSER_UID),
            environment=dict(BRIDGE_ENV),
            workdir=BRIDGE_WORKDIR,
        )["Id"]
        hijacked = api.exec_start(exec_id, socket=True)
    except NotFound as e:
        raise ScreenClosed(f"frame {frame_ref} is gone") from e
    except Exception as e:  # a stopped container (409), a daemon hiccup: no screen, the relay retries
        raise ScreenClosed(f"frame {frame_ref} refused the screen: {type(e).__name__}") from e
    try:
        raw = _raw_socket(hijacked)
        raw.setblocking(False)  # before the peek, so it can never block
    except (ScreenClosed, OSError):
        with contextlib.suppress(Exception):
            hijacked.close()
        raise
    return hijacked, raw, _read_ahead(hijacked)


async def open_docker_screen(frame_ref: str) -> ScreenStream:
    """The dev upstream. `ScreenClosed` when the frame is missing or not running. A
    frame whose sidecar daemon has not started yet answers with the bridge's own
    `bye{reason: "no_browser"}` as its first message, then ends."""
    hijacked, raw, early = await asyncio.to_thread(_hijack_sync, frame_ref)
    try:
        return await DockerScreen.attach(raw, early=early, hijacked=hijacked)
    except ScreenClosed:
        with contextlib.suppress(Exception):
            hijacked.close()
        raise


# ── sandbar's display route ──────────────────────────────────────────────────────────────────

DISPLAY_PROTOCOL: Final = "sandbar-display"
# Connecting, sending the upgrade and reading sandbar's answer must finish within this; after the
# 101 the pipe has no deadline (a screen lasts as long as someone watches).
_HANDSHAKE_S: Final = 10.0
# A display ticket is `dt_<b64url>.<b64url>`: nothing else may reach a request line we write.
_DISPLAY_TICKET: Final = re.compile(r"dt_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class _UpgradedScreen:
    """A `ScreenStream` over one upgraded HTTP connection: framed messages both ways, delimited
    by each header's `n`. No Docker multiplexing - sandbar demultiplexed the exec's stdout."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._decoder = StreamDecoder()
        self._ready: deque[tuple[dict[str, Any], bytes]] = deque()
        self._closed = False

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        while not self._ready:
            if self._closed:
                raise ScreenClosed("closed")
            try:
                chunk = await self._reader.read(READ_CHUNK)
            except OSError as e:
                raise ScreenClosed(f"sandbar's display pipe failed: {type(e).__name__}") from e
            if not chunk:
                raise ScreenClosed("sandbar's display pipe ended")
            try:
                self._ready.extend(self._decoder.feed(chunk))
            except WireError as e:
                raise ScreenClosed(f"refused the bridge's bytes: {e}") from e
        return self._ready.popleft()

    async def send(self, header: Mapping[str, Any], payload: bytes = b"") -> None:
        """`WireError` (a caller's oversize message) propagates; a dead upstream, or one that will
        not take the message in SEND_TIMEOUT_S, is `ScreenClosed`."""
        if self._closed:
            raise ScreenClosed("closed")
        self._writer.write(encode(header, payload))
        try:
            await asyncio.wait_for(self._writer.drain(), SEND_TIMEOUT_S)
        except OSError as e:  # a reset, or the timeout (a TimeoutError is an OSError)
            raise ScreenClosed(f"the bridge would not take a message: {type(e).__name__}") from e

    async def close(self) -> None:
        """Abort, never a plain close: with unsent bytes buffered (the send that timed out) a
        plain close waits for them to drain and feeds no EOF, so a pending `recv` would hang -
        DockerScreen's `shutdown` is the same rule."""
        if not self._closed:
            self._closed = True
            self._writer.transport.abort()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()


async def open_sandbar_screen(sandbox_id: str, ticket: str, *, base_url: str) -> ScreenStream:
    """sandbar's upstream: `GET /v1/sandboxes/{id}/display`, a plain HTTP upgrade
    admitted by a display ticket (`Authorization: Ticket dt_...`). The master token never rides
    this request - this function never sees it. After `101 Switching Protocols` the connection is
    a raw pipe to the sidecar's screen bridge. Any other answer is `ScreenClosed` naming the
    status: a refused ticket (401), a gone sandbox (404), a backend that cannot stream (400, 501),
    a daemon without display keys (503)."""
    parts = urlsplit(base_url)
    if not _DISPLAY_TICKET.fullmatch(ticket):
        raise ScreenClosed("not a display ticket")
    tls = parts.scheme == "https"
    try:
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(base_url)
        port = parts.port or (443 if tls else 80)  # a bad port is a ValueError
        path = f"{parts.path.rstrip('/')}/v1/sandboxes/{quote(sandbox_id, safe='')}/display"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parts.netloc.rpartition('@')[2]}\r\n"
            f"Authorization: Ticket {ticket}\r\n"
            "Connection: Upgrade\r\n"
            f"Upgrade: {DISPLAY_PROTOCOL}\r\n"
            "\r\n"
        ).encode("ascii")  # a non-ASCII path is a UnicodeEncodeError, a ValueError
    except ValueError as e:
        raise ScreenClosed("the sandbar url is not a usable http(s) url") from e
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(_HANDSHAKE_S):
            reader, writer = await asyncio.open_connection(
                parts.hostname, port, ssl=True if tls else None, limit=READ_CHUNK * 4
            )
            writer.write(request)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
        status, headers = _parse_head(head)
        if status != 101 or headers.get("upgrade", "").lower() != DISPLAY_PROTOCOL:
            raise ScreenClosed(f"sandbar display answered {status}")
        return _UpgradedScreen(reader, writer)
    except BaseException as e:
        if writer is not None:
            writer.transport.abort()  # a refused, failed or cancelled open leaves nothing open
        if isinstance(
            e, (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError)
        ):  # a timeout is an OSError
            raise ScreenClosed(f"sandbar display: {type(e).__name__}") from e
        raise


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    """The status code (0 when the status line is not HTTP) and the headers, names lowercased."""
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        status = 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers


# ── the doors ────────────────────────────────────────────────────────────────────────────────

ScreenKind = Literal["watch", "control"]
"""`watch`: the frames everyone sees - on Docker and sandbar the one stream also carries the lease
holder's input. `control`: a separate input stream for the holder alone (a driver's VNC)."""

DisplayTicket = Callable[[str], str]
"""The API's own single-use display ticket for one frame reference (`operations/tickets.mint_display`,
`dt_`). The upstream credential is the driver's business: a door admitted by the API's ticket
mints one per open through this; a door whose provider issues its own ignores it. `ai/` never holds
the signing key, so the caller hands the minter in."""

WATCH_ONLY: Final[frozenset[ScreenKind]] = frozenset({"watch"})


@dataclass(frozen=True, slots=True)
class ScreenDoor:
    """One driver's way to a Machine's screen: the upstream kinds it serves, whether it is open in
    this deployment, how to open one, and - for a door whose holder drives through a `control`
    stream - how to end a person's hold on the screen at its provider."""

    kinds: frozenset[ScreenKind]
    available: Callable[[], bool]
    open: Callable[[str, ScreenKind, DisplayTicket], Awaitable[ScreenStream]]
    hand_back: Callable[[str], Awaitable[None]] | None = None


async def _docker_door(ref: str, kind: ScreenKind, display_ticket: DisplayTicket) -> ScreenStream:
    """Docker's hijacked exec (dev): the API's own Docker socket, no ticket."""
    return await open_docker_screen(ref)


async def _sandbar_door(ref: str, kind: ScreenKind, display_ticket: DisplayTicket) -> ScreenStream:
    """sandbar's display route, admitted by a display ticket minted here, one per open; the API
    holds no sandbar master token. The relay keeps only "it closed"; the operator needs why (no
    display keys 503, a backend that cannot stream 400/501, a refused ticket 401) - never the
    ticket."""
    cfg = config.current()
    base_url = cfg.api.sandbar_url or cfg.worker.sandbar_url
    try:
        return await open_sandbar_screen(ref, display_ticket(ref), base_url=base_url)
    except ScreenClosed as e:
        logfire.warn("sandbar's display route refused {sandbox}: {reason}", sandbox=ref, reason=str(e))
        raise


DOORS: Final[Mapping[str, ScreenDoor]] = {
    "docker": ScreenDoor(WATCH_ONLY, lambda: True, _docker_door),
    # Open only once the operator says sandbar's side is configured (SANDBAR_DISPLAY_KEYS and the
    # API's route to it): `PRISMOID_COMPUTER_USE_SANDBAR_SCREEN`.
    "sandbar": ScreenDoor(WATCH_ONLY, lambda: config.current().computer_use.sandbar_screen, _sandbar_door),
}


def has_door(driver: str) -> bool:
    """A Machine on `driver` has a screen door open in this deployment."""
    door = DOORS.get(driver)
    return door is not None and door.available()


def has_control(driver: str) -> bool:
    """A Machine on `driver` is driven through a separate control stream (a driver's VNC), and
    that door is open here."""
    door = DOORS.get(driver)
    return door is not None and "control" in door.kinds and door.available()


async def hand_back(driver: str, ref: str) -> None:
    """End a person's hold on a Machine's screen, where its door has one (its provider's hand-back).
    Never raises: the door's own hand-back is best-effort."""
    door = DOORS.get(driver)
    if door is not None and door.hand_back is not None:
        await door.hand_back(ref)


async def open_upstream(
    driver: str, ref: str, kind: ScreenKind = "watch", *, display_ticket: DisplayTicket
) -> ScreenStream:
    """Open `kind` of a Machine's screen through its driver's door. `driver` is the Machine row's -
    the driver that made the frame - never this process's: in production only the worker carries
    PRISMOID_WORKER_FRAME_DRIVER. ScreenClosed when the driver has no door, or none of this kind.

    Not `operations/machines/screen.open_upstream(machine_id)`, which reads the row under its fence
    and calls this through `_open_stream`."""
    door = DOORS.get(driver)
    if door is None:
        raise ScreenClosed(f"no screen door for the {driver!r} driver")
    if kind not in door.kinds:
        raise ScreenClosed(f"the {driver!r} driver has no {kind} upstream")
    return await door.open(ref, kind, display_ticket)
