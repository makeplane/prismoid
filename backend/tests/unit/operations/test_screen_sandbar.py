# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""sandbar's upstream: `open_sandbar_screen` against a played sandbar - an
asyncio server that answers the display upgrade with 101 and echoes framed bytes, or refuses it -
and `open_upstream`'s choice of door by the Machine's driver. No live sandbar, no stack."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from prismoid.ai.frames import screen as frames_screen
from prismoid.ai.frames.screen import ScreenClosed, ScreenStream, open_sandbar_screen
from prismoid.conf import ApiSettings, settings
from prismoid.operations import tickets
from prismoid.operations.machines import screen as machine_screen
from prismoid.schemas.screen import encode

TICKET = "dt_eyJzYW5kYm94Ijoic2ItMSJ9.c2ln"  # the shape only: the played sandbar checks nothing
UPGRADED = b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: sandbar-display\r\n\r\n"

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def bare(header: dict[str, Any]) -> dict[str, Any]:
    """A decoded header without the codec's own byte count."""
    return {k: v for k, v in header.items() if k != "n"}


@asynccontextmanager
async def played(handler: Handler) -> AsyncGenerator[tuple[str, list[bytes]]]:
    """A one-route sandbar on loopback; yields its base URL and the request heads it read."""
    heads: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        try:
            await handler(reader, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"http://127.0.0.1:{port}", heads


async def upgrade_and_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # The 101 and the first message in one write: bytes past the head must not be lost.
    writer.write(UPGRADED + encode({"t": "hello", "v": 1}))
    await writer.drain()
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()


async def test_the_upgrade_carries_only_the_ticket_and_frames_flow_both_ways() -> None:
    async with played(upgrade_and_echo) as (base, heads):
        stream = await open_sandbar_screen("sb-1", TICKET, base_url=base)
        try:
            header, payload = await stream.recv()
            assert bare(header) == {"t": "hello", "v": 1} and payload == b""
            await stream.send({"t": "hint", "fps": 5})
            await stream.send(
                {"t": "frame", "seq": 1, "ts": 0, "w": 2, "h": 2, "fmt": "jpeg"}, b"\xff\xd8\xff\xd9"
            )
            header, payload = await stream.recv()
            assert bare(header) == {"t": "hint", "fps": 5} and payload == b""
            header, payload = await stream.recv()
            assert header["t"] == "frame" and payload == b"\xff\xd8\xff\xd9"
        finally:
            await stream.close()
    lines = heads[0].decode().split("\r\n")
    assert lines[0] == "GET /v1/sandboxes/sb-1/display HTTP/1.1"
    assert f"Host: {base.removeprefix('http://')}" in lines
    assert f"Authorization: Ticket {TICKET}" in lines
    assert "Connection: Upgrade" in lines and "Upgrade: sandbar-display" in lines
    assert not any("Bearer" in line for line in lines)  # the master token never rides this request


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (
            b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
            "401",
        ),
        (b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n", "503"),
        (b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n", "101"),
        (b"garbage\r\n\r\n", "0"),
    ],
)
async def test_anything_but_a_display_upgrade_is_screen_closed_naming_the_status(
    answer: bytes, status: str
) -> None:
    async def refuse(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(answer)
        await writer.drain()

    async with played(refuse) as (base, _):
        with pytest.raises(ScreenClosed, match=f"answered {status}$"):
            await open_sandbar_screen("sb-1", TICKET, base_url=base)


async def test_the_upstream_ending_is_screen_closed() -> None:
    async def hang_up(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(UPGRADED)
        await writer.drain()

    async with played(hang_up) as (base, _):
        stream = await open_sandbar_screen("sb-1", TICKET, base_url=base)
        with pytest.raises(ScreenClosed):
            await stream.recv()
        await stream.close()


async def test_an_upstream_that_never_reads_is_screen_closed_within_the_write_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ScreenStream contract: a send the upstream will not take in
    SEND_TIMEOUT_S is ScreenClosed, so the relay is never stalled by a wedged bridge."""
    monkeypatch.setattr(frames_screen, "SEND_TIMEOUT_S", 0.2)
    stop = asyncio.Event()

    async def never_read(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(UPGRADED)
        await writer.drain()
        await stop.wait()

    async with played(never_read) as (base, _):
        stream = await open_sandbar_screen("sb-1", TICKET, base_url=base)
        try:
            with pytest.raises(ScreenClosed):
                async with asyncio.timeout(10):
                    for _ in range(32):  # loopback buffers hold a few MB at most
                        await stream.send({"t": "text"}, b"x" * (3 << 20))
        finally:
            stop.set()
            await stream.close()


async def test_a_bad_ticket_or_a_dead_daemon_is_screen_closed() -> None:
    with pytest.raises(ScreenClosed, match="not a display ticket"):
        await open_sandbar_screen("sb-1", "dt_x.y\r\nX-Evil: 1", base_url="http://127.0.0.1:9")
    with pytest.raises(ScreenClosed, match="not a display ticket"):
        await open_sandbar_screen("sb-1", "st_abc.def", base_url="http://127.0.0.1:9")
    with pytest.raises(ScreenClosed, match="sandbar url"):
        await open_sandbar_screen("sb-1", TICKET, base_url="")
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(ScreenClosed, match="sandbar display"):
        await open_sandbar_screen("sb-1", TICKET, base_url=f"http://127.0.0.1:{port}")


async def test_a_sandbar_machine_opens_the_display_route_with_its_own_display_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}
    stream = cast(ScreenStream, object())

    async def fake_open(sandbox_id: str, ticket: str, *, base_url: str) -> ScreenStream:
        seen.update(sandbox_id=sandbox_id, ticket=ticket, base_url=base_url)
        return stream

    monkeypatch.setattr(frames_screen, "open_sandbar_screen", fake_open)
    monkeypatch.setattr(settings.api, "sandbar_url", "http://sandbar.internal:7070")
    monkeypatch.setattr(tickets, "PROCESS_STARTED", 0.0)  # a fresh mint is stale for SKEW_S after import
    assert await machine_screen._open_stream("sandbar", "machine-ab-3") is stream  # pyright: ignore[reportPrivateUsage]
    assert seen["sandbox_id"] == "machine-ab-3"
    assert seen["base_url"] == "http://sandbar.internal:7070"
    claims = tickets.peek_display(seen["ticket"], sandbox="machine-ab-3")
    assert claims.sandbox == "machine-ab-3" and 0 < claims.exp - claims.iat <= 30
    with pytest.raises(tickets.TicketRefused):
        tickets.peek_display(seen["ticket"], sandbox="machine-cd-1")


async def test_the_api_falls_back_to_the_workers_sandbar_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    async def fake_open(sandbox_id: str, ticket: str, *, base_url: str) -> ScreenStream:
        seen["base_url"] = base_url
        return cast(ScreenStream, object())

    monkeypatch.setattr(frames_screen, "open_sandbar_screen", fake_open)
    monkeypatch.setattr(settings.api, "sandbar_url", "")
    monkeypatch.setattr(settings.worker, "sandbar_url", "http://sandbard:7070")
    await machine_screen._open_stream("sandbar", "machine-ab-3")  # pyright: ignore[reportPrivateUsage]
    assert seen["base_url"] == "http://sandbard:7070"


async def test_a_docker_machine_opens_the_hijacked_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    stream = cast(ScreenStream, object())

    async def fake_docker(frame_ref: str) -> ScreenStream:
        seen.append(frame_ref)
        return stream

    async def no_sandbar(*_: object, **__: object) -> ScreenStream:
        raise AssertionError("a docker Machine never dials sandbar")

    monkeypatch.setattr(frames_screen, "open_docker_screen", fake_docker)
    monkeypatch.setattr(frames_screen, "open_sandbar_screen", no_sandbar)
    assert await machine_screen._open_stream("docker", "machine-ab-3") is stream  # pyright: ignore[reportPrivateUsage]
    assert seen == ["machine-ab-3"]
    with pytest.raises(ScreenClosed, match="no screen door"):
        await machine_screen._open_stream("e2b", "x")  # pyright: ignore[reportPrivateUsage]


def test_the_api_reads_its_own_sandbar_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISMOID_API_SANDBAR_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ApiSettings().sandbar_url == ""
    (tmp_path / ".env").write_text("PRISMOID_API_SANDBAR_URL=http://sandbar.internal:7070\n")
    assert ApiSettings().sandbar_url == "http://sandbar.internal:7070"


async def test_close_ends_a_pending_recv_even_with_unsent_bytes_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay closes a stream whose send timed out while `_link` waits in `recv` (review lane 2):
    a plain close waits for the unsent bytes to drain and feeds no EOF, so the link would hang on a
    frozen frame. close() must end the pending recv at once, as DockerScreen's shutdown does."""
    monkeypatch.setattr(frames_screen, "SEND_TIMEOUT_S", 0.2)
    stop = asyncio.Event()

    async def never_read(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(UPGRADED)
        await writer.drain()
        await stop.wait()

    async with played(never_read) as (base, _):
        stream = await open_sandbar_screen("sb-1", TICKET, base_url=base)
        pending = asyncio.create_task(stream.recv())
        try:
            with pytest.raises(ScreenClosed):
                async with asyncio.timeout(10):
                    for _ in range(32):
                        await stream.send({"t": "text"}, b"x" * (3 << 20))
            async with asyncio.timeout(1):
                await stream.close()
                with pytest.raises(ScreenClosed):
                    await pending
        finally:
            stop.set()
            pending.cancel()


async def test_a_cancelled_open_leaves_no_connection_behind() -> None:
    asked = asyncio.Event()
    gone = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, _: asyncio.StreamWriter) -> None:
        asked.set()
        await reader.read()  # b"" once the client's side is gone
        gone.set()

    async with played(silent) as (base, _):
        opening = asyncio.create_task(open_sandbar_screen("sb-1", TICKET, base_url=base))
        await asyncio.wait_for(asked.wait(), 5)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.wait_for(gone.wait(), 1)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:99999", "http://127.0.0.1:port", "http://127.0.0.1:9/café"]
)
async def test_an_unusable_sandbar_url_is_screen_closed(url: str) -> None:
    with pytest.raises(ScreenClosed, match="sandbar url"):
        await open_sandbar_screen("sb-1", TICKET, base_url=url)


async def test_a_refused_sandbar_open_is_logged_with_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay keeps only "it closed"; the operator needs the status (no keys 503, firecracker
    400, a refused ticket 401) - never the ticket."""
    said: list[dict[str, Any]] = []

    async def refused(sandbox_id: str, ticket: str, *, base_url: str) -> ScreenStream:
        raise ScreenClosed("sandbar display answered 503")

    monkeypatch.setattr(frames_screen, "open_sandbar_screen", refused)
    monkeypatch.setattr(frames_screen.logfire, "warn", lambda msg, **kw: said.append({"msg": msg, **kw}))
    with pytest.raises(ScreenClosed):
        await machine_screen._open_stream("sandbar", "machine-ab-3")  # pyright: ignore[reportPrivateUsage]
    assert len(said) == 1 and said[0]["sandbox"] == "machine-ab-3" and "503" in said[0]["reason"]
    assert "dt_" not in repr(said)


# The ticket sandbar/internal/ticket/ticket_test.go pins as `pythonTicket` (this minter's output for
# sandbox "sb-1" at now=1800000000, kid "k1", seed 00 01 .. 1f). Both suites check the one vector,
# so neither side can drift from the format alone.
GO_VECTOR = (
    "dt_eyJleHAiOjE4MDAwMDAwMzAsImlhdCI6MTgwMDAwMDAwMCwianRpIjoiMGRjNGQxNDNkMmVjMjc0YzQ2MWVhYzMxNDUwMjk2"
    "NTgiLCJraWQiOiJrMSIsInNhbmRib3giOiJzYi0xIn0.0Tbki0WACA8B5We6_M_D60tV5OMfVlHSNP_Tqdut83RlpnTWoXo0e7Obr8cvHmy"
    "6X7nxR78IPbmzm4wCuX62AA"
)
GO_PUBLIC_KEY = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="


def test_the_go_verifiers_pinned_ticket_is_this_minters(monkeypatch: pytest.MonkeyPatch) -> None:
    seed = base64.b64encode(bytes(range(32))).decode()
    monkeypatch.setattr(settings.api, "screen_signing_key", SecretStr(f"k1:{seed}"))
    monkeypatch.setattr(tickets, "PROCESS_STARTED", 0.0)
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]
    try:
        assert tickets.public_keys() == {"k1": GO_PUBLIC_KEY}
        claims = tickets.peek_display(GO_VECTOR, sandbox="sb-1", now=1_800_000_001)
        assert (claims.sandbox, claims.iat, claims.exp, claims.kid) == (
            "sb-1",
            1_800_000_000,
            1_800_000_030,
            "k1",
        )
        fresh = tickets.mint_display("sb-1", now=1_800_000_000)
        assert fresh[:59] == GO_VECTOR[:59]  # same sorted, compact claims up to the jti
    finally:
        tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]


async def test_a_door_without_the_kind_asked_for_is_screen_closed() -> None:
    with pytest.raises(ScreenClosed, match="no control upstream"):
        await frames_screen.open_upstream("docker", "machine-ab-3", "control", display_ticket=lambda ref: "")


async def test_the_sandbar_door_mints_one_display_ticket_per_open_and_docker_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec A5: the upstream credential is the driver's business. sandbar's door is admitted by the
    API's own `dt_`, minted once per open through the minter it is handed; Docker's needs none."""
    minted: list[str] = []

    async def fake_open(sandbox_id: str, ticket: str, *, base_url: str) -> ScreenStream:
        return cast(ScreenStream, object())

    async def fake_docker(frame_ref: str) -> ScreenStream:
        return cast(ScreenStream, object())

    def mint(ref: str) -> str:
        minted.append(ref)
        return "dt_x.y"

    monkeypatch.setattr(frames_screen, "open_sandbar_screen", fake_open)
    monkeypatch.setattr(frames_screen, "open_docker_screen", fake_docker)
    await frames_screen.open_upstream("sandbar", "machine-ab-3", display_ticket=mint)
    await frames_screen.open_upstream("sandbar", "machine-ab-3", display_ticket=mint)
    await frames_screen.open_upstream("docker", "machine-ab-3", display_ticket=mint)
    assert minted == ["machine-ab-3", "machine-ab-3"]
