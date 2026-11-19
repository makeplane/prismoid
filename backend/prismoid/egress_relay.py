# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The forward relay both egress proxies run: parse, vet, dial, splice.

Two proxies say yes or no per frame with the same rules: the Docker one (`ai/frames/egress.py`)
keys a frame by its source IP on the internal frame network; the device helper's
(`device/egress.py`) by the frame's own proxy credential, since every device frame is 127.0.0.1.
Each passes three hooks - who is asking, what that principal may reach, and where a refusal is
recorded - and everything else, the resolve-and-vet and the forward, is this one copy.

Moved out of `ai/frames/egress.py` (device-machine B3): `prismoid.device` may not import `prismoid.ai`,
and two forward proxies that drift apart is two fences. Like its caller, stdlib-only plus the one
shared vet (`prismoid.schemas.egress`), so the Docker proxy's container mounts this file beside it.

Four things only the device's proxy asks for (ADR-078), each off unless its
caller passes it, so the Docker proxy's behaviour is as it was: `loopback`, which gives the
addresses of a loopback port that are the principal's own (every other loopback request is a
private address, refused); `tunnel_gate`, which reads a tunnel's first request before anything is
dialed; `forward`, a real plain-http forward - the request's method, headers and body, its `Host`
rewritten to the URL the vet passed, the response streamed back - in place of the bare `METHOD path`
replay, and a tunnel that carries a half-close across (`_tunnel`); and `asset_pass`, a host off the
principal's list that a page's asset may still reach, past the same resolve-and-vet, a tunnel of it
only if it opens as a browser's own https does.

A client that stops is the one thing a proxy cannot wait out, so both proxies hold it to a deadline
for its request's head (`CONNECT_TIMEOUT`, before any credential is read), and neither lets the
upstream outlive it. Nothing is timed once a tunnel runs: it may sit silent for as long as both ends
are there. One FIN is all that is timed, on the device's proxy, where a half-closed client may still
be waiting: TCP cannot tell that from one that has gone, so silence after it ends things
(`FIN_GRACE_S`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import struct
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Final

from prismoid.schemas.egress import (  # the ONE vet, shared with the web door
    domain_allowed,
    refuse_ip,
)

LOG = logging.getLogger("egress")

CONNECT_TIMEOUT: Final = 15.0
FIN_GRACE_S: Final = 30.0  # the silence after a client's FIN that says it is gone, not half-closed

# Who is asking: the connection and its `Proxy-Authorization` value ("" when absent). None asks
# the client for a credential (a 407): clients like git's libcurl send one only when challenged.
PrincipalOf = Callable[[asyncio.StreamWriter, str], str | None]
# What that principal may reach (empty: nothing is registered for it).
AllowedFor = Callable[[str], Collection[str]]
# Record a refusal: (principal, host, reason). Called before the 403 is written.
OnDeny = Callable[[str, str, str], None]
# The addresses to dial for a loopback host and port, of those the principal itself listens on
# (principal, host, port). Empty: none is its own.
LoopbackFor = Callable[[str, str, int], Awaitable[list[str]]]
# Whose tunnels to loopback must show their first request (None: nobody's), and the check of one:
# (host, port, its head). The target hears nothing before it passes.
TunnelGate = Callable[[str, int, bytes], Awaitable[bool]]
TunnelGateFor = Callable[[str], TunnelGate | None]
# May this principal reach a host off its list, as a page's asset: (principal, host, port, a CONNECT).
AssetPass = Callable[[str, str, int, bool], bool]

LOOPBACK_REFUSED: Final = "loopback, on a port this task's commands did not open"
TUNNEL_REFUSED: Final = "a loopback tunnel that is no WebSocket from a page on this frame's own ports"
ASSET_TUNNEL_REFUSED: Final = "a tunnel that is not a browser's own https"
_HEAD_CAP: Final = 64 * 1024  # a forwarded request's header bytes
_FIRST_CAP: Final = 16 * 1024  # a tunnel's first request
_FIRST_S: Final = 5.0
_TLS_HANDSHAKE: Final = 0x16
_CLIENT_HELLO: Final = 0x01
_ALPN: Final = 16
_REASONS: Final = {400: "Bad Request", 414: "URI Too Long", 431: "Request Header Fields Too Large"}
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
# Never forwarded: the hop's own headers, and `Host`, which is the vetted URL's.
_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "host",
    }
)


def loopback_host(host: str) -> bool:
    """`localhost`, a `*.localhost` name, or a loopback address (IPv4-mapped included)."""
    host = host.strip("[]").rstrip(".").lower().partition("%")[0]
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _loopback_literals(host: str) -> list[str]:
    """Where a loopback name is dialed: a literal as itself, a name on both families' loopback."""
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return ["127.0.0.1", "::1"]
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return [str(addr)]


def own_literals(host: str, port: int, own: Collection[tuple[str, int]]) -> list[str]:
    """The addresses `host` names that are listened on in `own`, each (address, port): ownership is
    both, since a port on `::1` says nothing of the same number on `127.0.0.1`."""
    return [literal for literal in _loopback_literals(host) if (literal, port) in own]


def _host_port(hostport: str, default: int) -> tuple[str, int]:
    """`[::1]:80`, `[::1]`, `host:80`, `host`: brackets first, since an IPv6 literal is all colons."""
    if hostport.startswith("["):
        host, sep, rest = hostport[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            raise ValueError(hostport)
        return host, int(rest[1:] or default)
    host, _, port = hostport.partition(":")
    return host, int(port or default)


class _Quiet:
    """When a connection's bytes last moved."""

    def __init__(self) -> None:
        self.since = time.monotonic()

    def moved(self) -> None:
        self.since = time.monotonic()

    def left(self) -> float:
        """How much of the grace after a FIN is left."""
        return self.since + FIN_GRACE_S - time.monotonic()


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, quiet: _Quiet | None = None
) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
            if quiet is not None:
                quiet.moved()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _getaddrinfo(host: str, port: int, **kw: int):  # a seam for tests
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, **kw)


async def _resolve_and_vet(host: str, port: int) -> tuple[list[str], str | None]:
    """Every answer classified, none trusted: one inward address poisons the name (a mixed record
    is the classic rebinding trick). Returns every vetted literal, in resolver order, so a caller
    can try more than the first (an unrouted first AAAA should not fail a dial the next answer
    would have completed) - or an empty list with the reason nothing may be dialed: an unresolved
    name is not the same failure as a refused one, so a DNS failure is never journaled as if the
    resolver had *found* something inward."""
    try:
        answers = await _getaddrinfo(host, port)
    except (OSError, UnicodeError):  # UnicodeError: a label empty or too long to be a name at all
        return [], "unresolvable"
    literals: list[str] = [str(a[4][0]) for a in answers]
    if not literals:
        return [], "unresolvable"
    for lit in literals:
        if refuse_ip(ipaddress.ip_address(lit)) is not None:
            return [], "private address"
    return literals, None


async def resolve_pinned(host: str, port: int = 443) -> tuple[str | None, str | None]:
    """The single-literal convenience wrapper `relay`'s dial retry does not use (it calls
    `_resolve_and_vet` directly to try every vetted answer): `(pinned, None)` with the first
    vetted literal, or `(None, reason)` - `"unresolvable"` for a DNS failure or an empty answer,
    `"private address"` for a refused (or mixed) one."""
    literals, reason = await _resolve_and_vet(host, port)
    return (literals[0] if literals else None, reason)


async def _deny(
    writer: asyncio.StreamWriter, on_deny: OnDeny, principal: str, host: str, reason: str
) -> None:
    on_deny(principal, host, reason)
    body = json.dumps({"error": "egress_denied", "host": host, "reason": reason}).encode()
    writer.write(
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    await writer.drain()
    writer.close()


async def relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    principal_of: PrincipalOf,
    allowed_for: AllowedFor,
    on_deny: OnDeny,
    loopback: LoopbackFor | None = None,
    forward: bool = False,
    tunnel_gate: TunnelGateFor | None = None,
    asset_pass: AssetPass | None = None,
) -> None:
    """One client connection. Supports CONNECT (https) and absolute-URI GET/POST (http). A client
    that goes while it is answered is the end of this, and no error."""
    hooks = _Hooks(principal_of, allowed_for, on_deny, loopback, forward, tunnel_gate, asset_pass)
    try:
        await _serve(reader, writer, hooks)
    except ConnectionError:
        writer.close()


@dataclass(frozen=True, slots=True)
class _Hooks:
    """What the proxy on the other side of `relay` passes (the module's docstring says each)."""

    principal_of: PrincipalOf
    allowed_for: AllowedFor
    on_deny: OnDeny
    loopback: LoopbackFor | None
    forward: bool
    tunnel_gate: TunnelGateFor | None
    asset_pass: AssetPass | None


@dataclass(frozen=True, slots=True)
class _Asked:
    """A client's request, as far as its head says it: `head` is kept only for `forward`, and
    `auth` is the proxy credential, which is never replayed upstream."""

    method: str
    target: str
    auth: str
    head: list[bytes]


@dataclass(frozen=True, slots=True)
class _Aim:
    """Where a request is going, read: `framing` is how its body is framed, for a real forward."""

    is_connect: bool
    hostport: str
    host: str
    port: int
    origin_path: str
    framing: tuple[int | None, bool] | None


@dataclass(frozen=True, slots=True)
class _Way:
    """What may be dialed for a request that passed, each literal vetted. `first` is what its client
    already said (the target hears it first), and `passed` says it is here on an asset's pass."""

    literals: list[str]
    first: bytes = b""
    passed: bool = False


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hooks: _Hooks) -> None:
    """Parse, vet, dial, splice. Each step that says no has answered and closed the client."""
    asked = await _read_request(reader, writer, hooks.forward)
    if asked is None:
        return
    principal = hooks.principal_of(writer, asked.auth)
    if principal is None:
        await _challenge(writer)
        return
    allowed = hooks.allowed_for(principal)
    aim = await _aim_of(writer, asked, hooks)
    if aim is None:
        return
    if hooks.loopback is not None and loopback_host(aim.host):
        way = await _admit_loopback(reader, writer, hooks.loopback, hooks, aim, principal=principal)
    else:
        way = await _admit_site(reader, writer, hooks, aim, principal=principal, allowed=allowed)
    if way is None:
        return
    upstream = await _dial(writer, hooks.on_deny, principal, aim, way.literals)
    if upstream is None:
        return
    if not way.passed:  # an asset is no line per request, and no list ever allowed it
        LOG.info("ALLOW %s → %s:%s", principal, aim.host, aim.port)
    await _through(reader, writer, upstream, asked=asked, aim=aim, way=way, forward=hooks.forward)


async def _cut_off(writer: asyncio.StreamWriter, forward: bool, status: int, reason: str) -> None:
    """A request past the reader's limit: `forward` says so, and the other proxy only hangs up."""
    if forward:
        await _refuse(writer, status, reason)
    else:
        writer.close()


async def _read_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, forward: bool
) -> _Asked | None:
    """The client's request line and headers, or None with the client answered and closed."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)
    except (TimeoutError, ConnectionResetError):
        writer.close()
        return None
    except ValueError:  # past the reader's limit
        await _cut_off(writer, forward, 414, "the request's URL is too long to forward")
        return None

    if not request_line:
        writer.close()
        return None

    parts = request_line.decode(errors="replace").split()
    if len(parts) < 3:
        writer.close()
        return None
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):  # all of it: a line at a time buys no more
            head = await _read_head(reader, writer, forward)
    except TimeoutError:
        writer.close()
        return None
    return None if head is None else _Asked(parts[0], parts[1], *head)


async def _read_head(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, forward: bool
) -> tuple[str, list[bytes]] | None:
    """Drain the headers; the client will not proceed until we have read them off the socket. Only
    the proxy credential is kept (nothing is replayed upstream, so it never leaves this process) -
    but for `forward`, which keeps every line to send on (the credential never among them)."""
    auth = ""
    head: list[bytes] = []
    head_bytes = 0
    while True:
        try:
            line = await reader.readline()
        except ValueError:  # one line past the reader's limit
            await _cut_off(writer, forward, 431, "the request's headers are too large to forward")
            return None
        if line in (b"\r\n", b"\n", b""):
            return auth, head
        name, _, value = line.decode(errors="replace").partition(":")
        if name.strip().lower() == "proxy-authorization" and not auth:
            auth = value.strip()
        if forward:
            head.append(line)
            head_bytes += len(line)
            if head_bytes > _HEAD_CAP:
                await _refuse(writer, 431, "the request's headers are too large to forward")
                return None


async def _challenge(writer: asyncio.StreamWriter) -> None:
    writer.write(
        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
        b'Proxy-Authenticate: Basic realm="prismoid"\r\n'
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n"
    )
    await writer.drain()
    writer.close()


async def _aim_of(writer: asyncio.StreamWriter, asked: _Asked, hooks: _Hooks) -> _Aim | None:
    """Where `asked` is going, or None with the client answered and closed."""
    is_connect = asked.method.upper() == "CONNECT"
    origin_path = "/"
    if is_connect:
        hostport = asked.target
    else:
        # Absolute URI: http://host[:port]/path - a proxy receives the whole URL, not just a path.
        without_scheme = asked.target.split("://", 1)[-1]
        hostport, _, rest = without_scheme.partition("/")
        origin_path = f"/{rest}"
    try:
        host, port = _host_port(hostport, 443 if is_connect else 80)
    except ValueError:
        if hooks.loopback is None:
            writer.close()
            return None
        await _refuse(writer, 400, "a target this proxy cannot read")
        return None

    framing: tuple[int | None, bool] | None = None
    if hooks.forward and not is_connect:
        framing = _framing(asked.head)
        if framing is None or not _TOKEN.fullmatch(asked.method) or not asked.target.isascii():
            await _refuse(writer, 400, "a request this proxy cannot forward as it is")
            return None
    return _Aim(is_connect, hostport, host, port, origin_path, framing)


async def _admit_loopback(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    loopback: LoopbackFor,
    hooks: _Hooks,
    aim: _Aim,
    *,
    principal: str,
) -> _Way | None:
    """A loopback target: the principal's own addresses of that port, or a refusal. A gated tunnel's
    client speaks first, and the target hears nothing until it passes."""
    host, port, on_deny = aim.host, aim.port, hooks.on_deny
    literals = await loopback(principal, host, port)
    if not literals:
        await _deny(writer, on_deny, principal, host, LOOPBACK_REFUSED)
        return None
    gate = hooks.tunnel_gate(principal) if hooks.tunnel_gate is not None and aim.is_connect else None
    if gate is None:
        return _Way(literals)
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    first = await _first_request(reader)
    if not first or not await gate(host, port, first):
        await _deny(writer, on_deny, principal, host, TUNNEL_REFUSED)
        return None
    return _Way(literals, first)


async def _admit_site(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    hooks: _Hooks,
    aim: _Aim,
    *,
    principal: str,
    allowed: Collection[str],
) -> _Way | None:
    """Any other target: on the principal's list or holding an asset's pass, and every answer for
    its name vetted. A tunnel on a pass must then open as a browser's own https."""
    host, port, on_deny = aim.host, aim.port, hooks.on_deny
    listed = bool(allowed) and domain_allowed(host, allowed)
    asset_pass = hooks.asset_pass
    passed = not listed and asset_pass is not None and asset_pass(principal, host, port, aim.is_connect)
    if not allowed and not passed:
        await _deny(writer, on_deny, principal, host, "no allowlist registered for this frame")
        return None
    if not listed and not passed:
        await _deny(writer, on_deny, principal, host, f"not in allowlist {sorted(allowed)}")
        return None

    literals, reason = await _resolve_and_vet(host, port)
    if not literals:
        await _deny(writer, on_deny, principal, host, reason or "private address")
        return None
    first = b""
    if passed and aim.is_connect:  # its client speaks first: the target hears nothing until it passes
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        first = await _client_hello(reader)
        if not offers_h2(first):
            await _deny(writer, on_deny, principal, host, ASSET_TUNNEL_REFUSED)
            return None
    return _Way(literals, first, passed)


async def _dial(
    writer: asyncio.StreamWriter, on_deny: OnDeny, principal: str, aim: _Aim, literals: list[str]
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """The upstream, or None with the client refused."""
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
    last_exc: Exception | None = None
    for literal in literals:  # try every vetted literal, not just the first (both proxies do)
        try:
            upstream = await asyncio.wait_for(
                asyncio.open_connection(literal, aim.port), timeout=CONNECT_TIMEOUT
            )
            break
        except (OSError, TimeoutError) as exc:
            last_exc = exc
            continue
    if upstream is None:
        exc_name = type(last_exc).__name__ if last_exc else "no vetted literal reachable"
        await _deny(writer, on_deny, principal, aim.host, f"upstream unreachable: {exc_name}")
    return upstream


async def _through(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    *,
    asked: _Asked,
    aim: _Aim,
    way: _Way,
    forward: bool,
) -> None:
    """The request on its way, and the bytes both ways until it ends."""
    upstream_reader, upstream_writer = upstream
    try:  # from here the upstream is ours to end, however this goes
        if way.first:
            upstream_writer.write(way.first)
            await upstream_writer.drain()
        elif aim.is_connect:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        elif aim.framing is not None:
            await _forward(
                reader,
                writer,
                upstream,
                request=f"{asked.method} {aim.origin_path}",
                hostport=aim.hostport,
                head=asked.head,
                framing=aim.framing,
            )
            return
        else:
            # Replay the request we consumed, in origin form - the upstream server wants a path, not
            # the absolute URI the client sent us.
            upstream_writer.write(
                f"{asked.method} {aim.origin_path} HTTP/1.1\r\nHost: {aim.host}\r\nConnection: close\r\n\r\n".encode()
            )
            await upstream_writer.drain()

        if forward:
            await _tunnel(reader, writer, upstream)
            return
        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
            return_exceptions=True,
        )
    finally:
        upstream_writer.close()


async def _carry(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, quiet: _Quiet) -> bool:
    """One way of a tunnel, its FIN carried across: True if it ended in one, False if it broke."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
            quiet.moved()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        return False
    return True


async def _tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    """Both ways until both end. One side's FIN says only that it has finished sending, so the other
    way runs on until it ends too, breaks, or falls silent for `FIN_GRACE_S`. A broken way ends both."""
    upstream_reader, upstream_writer = upstream
    quiet = _Quiet()
    ways = {
        asyncio.create_task(_carry(reader, upstream_writer, quiet)),
        asyncio.create_task(_carry(upstream_reader, writer, quiet)),
    }
    try:
        done, waiting = await asyncio.wait(ways, return_when=asyncio.FIRST_COMPLETED)
        if all(way.result() for way in done):
            quiet.moved()
            while waiting and (left := quiet.left()) > 0:
                _, waiting = await asyncio.wait(waiting, timeout=left)
    finally:
        for way in ways:
            way.cancel()
        await asyncio.gather(*ways, return_exceptions=True)
        writer.close()


async def _first_request(reader: asyncio.StreamReader) -> bytes:
    """The head of the first request in a tunnel, or b"" for anything else: a TLS handshake never
    ends in a blank line, so it is told by its first bytes, not by a timeout."""
    try:
        start = await asyncio.wait_for(reader.readexactly(4), timeout=_FIRST_S)
        if start != b"GET ":
            return b""
        rest = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_FIRST_S)
    except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return b""
    return start + rest if len(rest) <= _FIRST_CAP else b""


async def _client_hello(reader: asyncio.StreamReader) -> bytes:
    """The TLS record a tunnel opens with, whole, or b"" for anything else."""
    try:
        header = await asyncio.wait_for(reader.readexactly(5), timeout=_FIRST_S)
        if header[0] != _TLS_HANDSHAKE:
            return b""
        size = struct.unpack(">H", header[3:])[0]
        return header + await asyncio.wait_for(reader.readexactly(size), timeout=_FIRST_S)
    except (TimeoutError, OSError, asyncio.IncompleteReadError):
        return b""


def _alpn_has_h2(record: bytes, names: int, stop: int) -> bool:
    while names < stop:
        if record[names + 1 : names + 1 + record[names]] == b"h2":
            return True
        names += 1 + record[names]
    return False


def offers_h2(record: bytes) -> bool:
    """Does this TLS record hold a ClientHello whose ALPN offers `h2`? Chrome's own https always
    does. Its WebSocket offers only `http/1.1` and a TURN relay's TLS neither, so what cannot show
    this is no request the browser's fence judged. Anything unreadable is a no."""
    try:
        if record[0] != _TLS_HANDSHAKE or record[5] != _CLIENT_HELLO:
            return False
        at = 5 + 4 + 2 + 32  # the record's and the handshake's headers, the version, the random
        at += 1 + record[at]  # the session id
        at += 2 + struct.unpack_from(">H", record, at)[0]  # the cipher suites
        at += 1 + record[at]  # the compression methods
        end = at + 2 + struct.unpack_from(">H", record, at)[0]
        at += 2
        while at < end:
            kind, size = struct.unpack_from(">HH", record, at)
            at += 4
            if kind == _ALPN:
                return _alpn_has_h2(record, at + 2, at + size)
            at += size
    except (IndexError, struct.error):
        return False
    return False


def _framing(head: list[bytes]) -> tuple[int | None, bool] | None:
    """How a forwarded request's body is framed - (its Content-Length, chunked) - or None for one
    that cannot be forwarded safely: a header line holding a bare CR or NUL, a folded line, both
    framings, or a length or a coding this proxy cannot read (two parsers disagreeing on where a
    body ends is how a second request is smuggled)."""
    lengths: set[str] = set()
    codings: list[str] = []
    for line in head:
        text = line.decode("latin-1").removesuffix("\n").removesuffix("\r")
        if "\r" in text or "\0" in text or text[:1] in (" ", "\t"):
            return None
        name, sep, value = text.partition(":")
        if not sep or not _TOKEN.fullmatch(name):
            return None
        if name.lower() == "content-length":
            lengths.add(value.strip())
        elif name.lower() == "transfer-encoding":
            codings += [c.strip().lower() for c in value.split(",") if c.strip()]
    if codings:
        return (None, True) if not lengths and codings == ["chunked"] else None
    if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]{1,18}", next(iter(lengths)))):
        return None
    return (int(next(iter(lengths))) if lengths else 0), False


async def _copy_exactly(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, left: int) -> None:
    while left > 0:
        data = await reader.read(min(left, 65536))
        if not data:
            raise asyncio.IncompleteReadError(b"", left)
        writer.write(data)
        await writer.drain()
        left -= len(data)


async def _copy_chunks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        size_line = await reader.readline()
        digits = size_line.split(b";")[0].strip()
        if not _CHUNK_SIZE.fullmatch(digits):
            raise ValueError(digits)
        size = int(digits, 16)
        writer.write(size_line)
        if size == 0:
            while (trailer := await reader.readline()) not in (b"\r\n", b"\n", b""):
                writer.write(trailer)
            writer.write(b"\r\n")
            await writer.drain()
            return
        # The chunk and its line end, in pieces: a size is only a claim.
        await _copy_exactly(reader, writer, size + 2)


async def _copy_body(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, framing: tuple[int | None, bool]
) -> None:
    """The request's body, exactly as framed, and nothing after it (a second request on the same
    connection is never sent on to this host: the client is closed once this one is answered). A
    body misframed or cut short ends the upstream connection: nothing is guessed."""
    length, chunked = framing
    try:
        if chunked:
            await _copy_chunks(reader, writer)
        else:
            await _copy_exactly(reader, writer, length or 0)
    except (ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()


async def _watch_client(
    reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    body: asyncio.Task[None],
    quiet: _Quiet,
) -> None:
    """Once a request is sent nothing else reads its client, so this does, or the upstream of a
    client that left would hold on until it next spoke. A reset is gone. A FIN may be a client that
    has only finished sending, so it ends the upstream once the upstream has stayed silent through
    the grace. What it reads is dropped: a second request on this connection is never sent on."""
    await body
    try:
        while await reader.read(65536):
            pass
    except OSError:
        upstream_writer.close()
        return
    quiet.moved()
    while (left := quiet.left()) > 0:  # noqa: ASYNC110 - a deadline the answer keeps moving
        await asyncio.sleep(left)
    upstream_writer.close()


async def _forward(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    *,
    request: str,
    hostport: str,
    head: list[bytes],
    framing: tuple[int | None, bool],
) -> None:
    """One plain-http request, whole: its headers but the hop's own and those its `Connection`
    names, `Host` as the vetted URL says, its body as framed; then the response, streamed back,
    to its end. One request per connection (`Connection: close` both ways)."""
    upstream_reader, upstream_writer = upstream
    named = {
        token.strip().lower()
        for line in head
        if line.split(b":", 1)[0].strip().lower() == b"connection"
        for token in line.decode("latin-1").partition(":")[2].split(",")
    }
    kept = [
        line
        for line in head
        if (name := line.decode("latin-1").partition(":")[0].strip().lower()) not in _HOP
        and name not in named
    ]
    upstream_writer.write(
        f"{request} HTTP/1.1\r\nHost: {hostport}\r\n".encode("latin-1")
        + b"".join(line.rstrip(b"\r\n") + b"\r\n" for line in kept)
        + b"Connection: close\r\n\r\n"
    )
    quiet = _Quiet()
    body = asyncio.create_task(_copy_body(reader, upstream_writer, framing))
    watch = asyncio.create_task(_watch_client(reader, upstream_writer, body, quiet))
    try:
        await upstream_writer.drain()
        await _pipe(upstream_reader, writer, quiet)  # closes the client once the answer has ended
    finally:
        body.cancel()
        watch.cancel()
        await asyncio.gather(body, watch, return_exceptions=True)
        upstream_writer.close()


async def _refuse(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    body = json.dumps({"error": "bad_request", "reason": reason}).encode()
    writer.write(
        f"HTTP/1.1 {status} {_REASONS[status]}\r\n".encode() + b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    await writer.drain()
    writer.close()
