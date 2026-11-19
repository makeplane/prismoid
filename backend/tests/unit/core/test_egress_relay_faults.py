# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the forward relay does when a client or an upstream misbehaves (the device
helper): real sockets on loopback, both proxies' ways of calling `relay`.

`cloud` is the Docker proxy's call, `device` the helper's. The cloud sidecar shares this file, and
only its error paths may move, so `test_a_working_*_is_byte_for_byte_*` pins a working request's
bytes on that path: they pass unchanged against the relay as it stood before these fixes."""

from __future__ import annotations

import asyncio
import base64
import socket
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from prismoid import egress_relay
from prismoid.device.egress import DeviceEgress
from prismoid.egress_relay import relay

ALLOWED = "up.test"


@dataclass
class Upstream:
    """A server the relay may reach, doing what `act` says with each connection."""

    act: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
    got: bytearray = field(default_factory=bytearray)
    live: int = 0
    total: int = 0
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    port: int = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.live += 1
        self.total += 1
        try:
            await self.act(reader, writer)
        except OSError:
            pass
        finally:
            self.live -= 1
            self.closed.set()
            writer.close()


async def _until_eof(up: Upstream, reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        up.got += data


@pytest.fixture
async def resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    async def vetted(host: str, _port: int) -> tuple[list[str], str | None]:
        return (["127.0.0.1"], None) if host.endswith(".test") else ([], "unresolvable")

    monkeypatch.setattr(egress_relay, "_resolve_and_vet", vetted)


async def _upstream(
    act: Callable[[Upstream, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[Upstream, asyncio.Server]:
    up = Upstream(lambda r, w: act(up, r, w))
    server = await asyncio.start_server(up.handle, "127.0.0.1", 0)
    up.port = server.sockets[0].getsockname()[1]
    return up, server


class Proxy:
    """`relay` on a port, called as one proxy calls it."""

    def __init__(self, *, forward: bool) -> None:
        self.forward = forward
        self.denied: list[tuple[str, str, str]] = []
        self.ended = asyncio.Event()
        self.raised: list[BaseException] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.forward:

                async def own(_principal: str, _host: str, _port: int) -> list[str]:
                    return []

                await relay(
                    reader,
                    writer,
                    principal_of=lambda _w, _a: "frame",
                    allowed_for=lambda _p: {ALLOWED},
                    on_deny=lambda *a: self.denied.append(a),
                    loopback=own,
                    forward=True,
                )
            else:
                await relay(
                    reader,
                    writer,
                    principal_of=lambda _w, _a: "frame",
                    allowed_for=lambda _p: {ALLOWED},
                    on_deny=lambda *a: self.denied.append(a),
                )
        except BaseException as e:
            self.raised.append(e)
            raise
        finally:
            self.ended.set()

    async def __aenter__(self) -> Proxy:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.close()

    async def dial(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", self.port)


def _reset(writer: asyncio.StreamWriter) -> None:
    """A client that goes all at once: a close with SO_LINGER 0 is a RST."""
    sock = writer.get_extra_info("socket")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


async def _read_to_end(reader: asyncio.StreamReader, seconds: float = 3.0) -> bytes:
    return await asyncio.wait_for(reader.read(-1), seconds)


modes = pytest.mark.parametrize("forward", [False, True], ids=["cloud", "device"])


# ── the head of a request ────────────────────────────────────────────────────────────────────────


async def test_a_request_line_past_the_limit_is_answered_on_the_device() -> None:
    async with Proxy(forward=True) as proxy:
        reader, writer = await proxy.dial()
        writer.write(b"GET http://up.test/" + b"a" * 70_000 + b" HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert (await _read_to_end(reader)).startswith(b"HTTP/1.1 414 ")
        assert not proxy.raised


async def test_a_request_line_past_the_limit_ends_quietly_in_the_cloud() -> None:
    async with Proxy(forward=False) as proxy:
        reader, writer = await proxy.dial()
        writer.write(b"GET http://up.test/" + b"a" * 70_000 + b" HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert await _read_to_end(reader) == b""
        await proxy.ended.wait()
        assert not proxy.raised


async def test_a_header_line_past_the_limit_ends_quietly_in_the_cloud() -> None:
    async with Proxy(forward=False) as proxy:
        reader, writer = await proxy.dial()
        writer.write(b"GET http://up.test/ HTTP/1.1\r\nX: " + b"a" * 70_000 + b"\r\n\r\n")
        await writer.drain()
        assert await _read_to_end(reader) == b""
        await proxy.ended.wait()
        assert not proxy.raised


@modes
async def test_a_client_that_stops_inside_its_head_is_ended(
    forward: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request line, then silence, before any credential. The whole head counts: a header a
    second would have bought a client as long as it liked."""
    monkeypatch.setattr(egress_relay, "CONNECT_TIMEOUT", 0.2)
    async with Proxy(forward=forward) as proxy:
        reader, writer = await proxy.dial()
        writer.write(b"GET http://up.test/ HTTP/1.1\r\n")
        await writer.drain()

        async def trickle() -> None:
            for n in range(15):
                await asyncio.sleep(0.07)
                with suppress(OSError):
                    writer.write(b"X-%d: y\r\n" % n)

        trickling = asyncio.create_task(trickle())
        started = time.monotonic()
        with suppress(ConnectionError):
            assert await _read_to_end(reader) == b""
        assert time.monotonic() - started < 0.7, "the deadline is the head's, not each line's"
        trickling.cancel()
        await proxy.ended.wait()
        assert not proxy.raised


async def test_a_credential_that_is_no_text_reaches_nothing() -> None:
    """Base64 that is not ASCII is a plain `ValueError`, past what the door caught."""
    egress = DeviceEgress({})
    assert egress.principal_of(None, "Basic \xff\xfe") == ""  # pyright: ignore[reportArgumentType]
    assert egress.principal_of(None, "Basic %%%") == ""  # pyright: ignore[reportArgumentType]
    assert egress.principal_of(None, "Basic " + base64.b64encode(b"\xff:\xfe").decode()) == ""  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("host", ["a..up.test", "x" * 70 + ".up.test"])
async def test_a_name_that_is_no_name_is_unresolvable(host: str) -> None:
    """A wildcard lets it past the list, and the resolver's encoder then raises `UnicodeError`."""
    assert await egress_relay._resolve_and_vet(host, 443) == ([], "unresolvable")  # pyright: ignore[reportPrivateUsage]


# ── a client that goes ───────────────────────────────────────────────────────────────────────────


@modes
async def test_a_client_reset_during_the_dial_takes_the_upstream_with_it(
    forward: bool, resolve: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    up, server = await _upstream(_until_eof)
    real = asyncio.open_connection

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.15)
        return await real(*args, **kwargs)

    async with Proxy(forward=forward) as proxy:
        _, writer = await proxy.dial()
        monkeypatch.setattr(asyncio, "open_connection", slow)
        writer.write(f"CONNECT up.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await asyncio.sleep(0.05)
        _reset(writer)
        await asyncio.wait_for(up.closed.wait(), 2.0)
        await proxy.ended.wait()
        assert up.live == 0 and not proxy.raised
    server.close()


@pytest.fixture
async def silent() -> AsyncIterator[Upstream]:
    """An upstream that reads and never says a word, as a dev server with no timeout does."""
    up, server = await _upstream(_until_eof)
    yield up
    server.close()


async def test_a_body_cut_short_is_not_waited_for(silent: Upstream, resolve: None) -> None:
    async with Proxy(forward=True) as proxy:
        _, writer = await proxy.dial()
        writer.write(
            f"POST http://up.test:{silent.port}/ HTTP/1.1\r\nContent-Length: 100\r\n\r\n0123456789".encode()
        )
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
        await asyncio.wait_for(silent.closed.wait(), 2.0)
        assert silent.live == 0


async def test_a_reset_while_the_upstream_is_silent_ends_it_at_once(silent: Upstream, resolve: None) -> None:
    async with Proxy(forward=True) as proxy:
        _, writer = await proxy.dial()
        writer.write(f"GET http://up.test:{silent.port}/ HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await asyncio.sleep(0.1)
        _reset(writer)
        await asyncio.wait_for(silent.closed.wait(), 1.0)


async def test_a_fin_while_the_upstream_is_silent_ends_it_after_the_grace(
    silent: Upstream, resolve: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chrome giving up a long poll. It held on until the upstream next spoke, which a dev server
    with no timeout may never do."""
    monkeypatch.setattr(egress_relay, "FIN_GRACE_S", 0.3)
    async with Proxy(forward=True) as proxy:
        _, writer = await proxy.dial()
        writer.write(f"GET http://up.test:{silent.port}/ HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
        await asyncio.sleep(0.1)
        assert silent.live == 1, "inside the grace it may still be half-closed"
        await asyncio.wait_for(silent.closed.wait(), 2.0)


async def test_a_half_closed_client_still_hears_its_answer(
    resolve: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sends, shuts its write side, reads: a FIN is all that says, so an answer inside the grace is
    still its own, and so is one that keeps coming past it."""
    monkeypatch.setattr(egress_relay, "FIN_GRACE_S", 0.3)

    async def slow_answer(up: Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.15)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n")
        for piece in (b"late-", b"but-", b"mine"[:3]):
            await asyncio.sleep(0.2)
            writer.write(piece)
            await writer.drain()

    up, server = await _upstream(slow_answer)
    async with Proxy(forward=True) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"GET http://up.test:{up.port}/ HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        writer.write_eof()
        got = await _read_to_end(reader)
        assert got.endswith(b"\r\n\r\nlate-but-min")
    server.close()


# ── a tunnel ─────────────────────────────────────────────────────────────────────────────────────


@modes
async def test_a_silent_tunnel_outlives_every_timeout(
    forward: bool, resolve: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long but live tunnel is nobody's to cut, in either proxy, whatever the head's deadline and
    the FIN's grace are."""
    monkeypatch.setattr(egress_relay, "CONNECT_TIMEOUT", 0.1)
    monkeypatch.setattr(egress_relay, "FIN_GRACE_S", 0.1)

    async def echo(_up: Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    up, server = await _upstream(echo)
    async with Proxy(forward=forward) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"CONNECT up.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.5)
        writer.write(b"still here")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(10), 1.0) == b"still here"
        writer.close()
    server.close()


async def test_a_device_tunnel_carries_a_half_close_across(
    resolve: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(egress_relay, "FIN_GRACE_S", 2.0)

    async def late(_up: Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.read(-1) == b"question", "its FIN arrived, as a FIN"
        await asyncio.sleep(0.2)
        writer.write(b"late answer")
        await writer.drain()

    up, server = await _upstream(late)
    async with Proxy(forward=True) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"CONNECT up.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"question")
        await writer.drain()
        writer.write_eof()
        assert await _read_to_end(reader) == b"late answer"
    server.close()


async def test_a_device_tunnel_ends_when_the_other_side_has_gone_silent(
    resolve: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(egress_relay, "FIN_GRACE_S", 0.2)

    async def hold(up: Upstream, reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        await reader.read(-1)
        await asyncio.sleep(5)

    up, server = await _upstream(hold)
    async with Proxy(forward=True) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"CONNECT up.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        writer.write_eof()
        assert await _read_to_end(reader) == b"", "ended by the grace, not by the upstream"
    server.close()


async def test_a_device_tunnel_ends_at_once_on_a_reset(silent: Upstream, resolve: None) -> None:
    async with Proxy(forward=True) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"CONNECT up.test:{silent.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        _reset(writer)
        await asyncio.wait_for(silent.closed.wait(), 1.0)


# ── what a working request does on the cloud path, which must not move ──────────────────────────


async def test_a_working_get_is_byte_for_byte_what_it_was(resolve: None) -> None:
    async def answer(up: Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up.got += await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()

    up, server = await _upstream(answer)
    async with Proxy(forward=False) as proxy:
        reader, writer = await proxy.dial()
        writer.write(
            f"GET http://up.test:{up.port}/a?b=1 HTTP/1.1\r\nHost: up.test\r\nCookie: dropped\r\n\r\n".encode()
        )
        await writer.drain()
        assert await _read_to_end(reader) == b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    assert bytes(up.got) == b"GET /a?b=1 HTTP/1.1\r\nHost: up.test\r\nConnection: close\r\n\r\n"
    server.close()


async def test_a_working_connect_is_byte_for_byte_what_it_was(resolve: None) -> None:
    async def echo(up: Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            up.got += data
            writer.write(data.upper())
            await writer.drain()

    up, server = await _upstream(echo)
    async with Proxy(forward=False) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"CONNECT up.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert await reader.readuntil(b"\r\n\r\n") == b"HTTP/1.1 200 Connection Established\r\n\r\n"
        writer.write(b"abc")
        await writer.drain()
        assert await reader.readexactly(3) == b"ABC"
        writer.close()
        await asyncio.wait_for(up.closed.wait(), 1.0)
    assert bytes(up.got) == b"abc"
    server.close()


async def test_a_client_fin_on_a_cloud_tunnel_is_still_the_end_of_it(resolve: None) -> None:
    """The half-close the device's tunnel carries across is the device's own: here a FIN closes."""

    async def late(_up: Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(-1)
        await asyncio.sleep(0.1)
        with suppress(OSError):
            writer.write(b"late answer")
            await writer.drain()

    up, server = await _upstream(late)
    async with Proxy(forward=False) as proxy:
        reader, writer = await proxy.dial()
        writer.write(f"CONNECT up.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"q")
        await writer.drain()
        writer.write_eof()
        assert await _read_to_end(reader) == b""
    server.close()


async def test_a_refused_request_is_refused_as_it_was_in_the_cloud(resolve: None) -> None:
    async with Proxy(forward=False) as proxy:
        reader, writer = await proxy.dial()
        writer.write(b"CONNECT off.example:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        said = await _read_to_end(reader)
    assert said.startswith(b"HTTP/1.1 403 Forbidden\r\n") and b'"host": "off.example"' in said
    assert proxy.denied == [("frame", "off.example", "not in allowlist ['up.test']")]
