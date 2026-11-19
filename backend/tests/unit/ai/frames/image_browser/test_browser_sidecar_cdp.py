# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The pipe transport: framing, id matching, events, and a closed pipe."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.cdp import CdpClosed, CdpError, PipeTransport  # noqa: E402


async def _fake_chromium(
    to_sidecar_w: int, from_sidecar_r: int, script: Callable[[dict[str, Any]], list[dict[Any, Any]]]
) -> None:
    """Read requests from the sidecar, answer per `script(request) -> list[dict]`."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(from_sidecar_r, "rb", 0)
    )
    while True:
        raw = await reader.readuntil(b"\0")
        req: dict[str, Any] = json.loads(raw[:-1])
        for msg in script(req):
            data = json.dumps(msg).encode() + b"\0"
            await loop.run_in_executor(None, os.write, to_sidecar_w, data)


@pytest.fixture
def pipes() -> Iterator[tuple[int, int, int, int]]:
    a_r, a_w = os.pipe()  # chromium -> sidecar
    b_r, b_w = os.pipe()  # sidecar -> chromium
    yield (a_r, a_w, b_r, b_w)
    for fd in [a_r, a_w, b_r, b_w]:
        with contextlib.suppress(OSError):
            os.close(fd)


async def test_call_matches_response_by_id(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        return [{"id": req["id"], "result": {"echo": req["method"]}}]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    assert await t.call("Target.getTargets") == {"echo": "Target.getTargets"}
    assert await t.call("Page.enable", session_id="S1") == {"echo": "Page.enable"}
    await t.close()
    fake.cancel()


async def test_error_response_raises_cdp_error(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        return [{"id": req["id"], "error": {"code": -32601, "message": "'Nope.x' wasn't found"}}]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    with pytest.raises(CdpError) as exc:
        await t.call("Nope.x")
    assert exc.value.code == -32601
    await t.close()
    fake.cancel()


async def test_events_dispatch_to_handlers_and_do_not_block_calls(
    pipes: tuple[int, int, int, int],
) -> None:
    a_r, a_w, b_r, b_w = pipes
    seen: list[dict[Any, Any]] = []

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        return [
            {"method": "Page.loadEventFired", "params": {"timestamp": 1.0}, "sessionId": "S1"},
            {"id": req["id"], "result": {}},
        ]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    t.on("Page.loadEventFired", seen.append)
    await t.start(read_fd=a_r, write_fd=b_w)
    await t.call("Page.navigate", {"url": "about:blank"}, session_id="S1")
    assert seen and seen[0]["params"]["timestamp"] == 1.0
    await t.close()
    fake.cancel()


async def test_closed_pipe_fails_pending_calls(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, _b_r, b_w = pipes
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    os.close(a_w)  # chromium went away
    with pytest.raises(CdpClosed):
        await t.call("Page.enable", timeout=2.0)
    await t.close()


async def test_large_message_returns_result(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        large_data = "x" * (200 * 1024)
        return [{"id": req["id"], "result": {"data": large_data}}]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    result = await t.call("Test.largeMessage")
    assert len(result["data"]) == 200 * 1024
    await t.close()
    fake.cancel()


async def test_handler_exception_does_not_kill_pump(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes
    seen: list[str] = []

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        return [
            {"method": "Page.eventFired", "params": {"n": 1}},
            {"id": req["id"], "result": {}},
        ]

    def handler_raises(msg: dict[str, Any]) -> None:
        raise ValueError("handler error")

    def handler_logs(msg: dict[str, Any]) -> None:
        seen.append("logged")

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    t.on("Page.eventFired", handler_raises)
    t.on("Page.eventFired", handler_logs)
    await t.start(read_fd=a_r, write_fd=b_w)
    await t.call("Page.navigate", session_id="S1")
    assert seen == ["logged"]
    await t.close()
    fake.cancel()


async def test_timeout_does_not_close_transport(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes
    call_count: int = 0

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []  # Never reply to first call
        return [{"id": req["id"], "result": {}}]  # Reply to second call

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    with pytest.raises(TimeoutError):
        await t.call("Test.timeout", timeout=0.2)
    result = await t.call("Test.afterTimeout", timeout=1.0)
    assert result == {}
    await t.close()
    fake.cancel()


async def test_concurrent_writes_are_serialized(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        return [{"id": req["id"], "result": {"id": req["id"]}}]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    large_payload = "x" * (200 * 1024)
    results = await asyncio.gather(
        t.call("Test.call1", {"payload": large_payload}),
        t.call("Test.call2", {"payload": large_payload}),
    )
    assert results[0]["id"] == 1
    assert results[1]["id"] == 2
    await t.close()
    fake.cancel()


async def test_a_call_on_a_tab_that_closes_fails_at_once(pipes: tuple[int, int, int, int]) -> None:
    """Chromium never answers a command in flight on a tab that closes (measured: a popup's
    `window.close()` left the sidecar waiting the full 30 s with the verb lock held). Its session's
    detach fails that session's calls at once; another session's calls are untouched."""
    a_r, a_w, b_r, b_w = pipes

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        if req.get("sessionId") == "S2":  # the tab closes under this call: no answer, a detach
            return [{"method": "Target.detachedFromTarget", "params": {"sessionId": "S2", "targetId": "T2"}}]
        return [{"id": req["id"], "result": {"ok": True}}]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(CdpError):
        await t.call("Page.getNavigationHistory", session_id="S2", timeout=10.0)
    assert loop.time() - started < 2.0
    assert await t.call("Page.getNavigationHistory", session_id="S1", timeout=2.0) == {"ok": True}
    await t.close()
    fake.cancel()


async def test_a_tabs_detach_leaves_another_tabs_call_in_flight(pipes: tuple[int, int, int, int]) -> None:
    a_r, a_w, b_r, b_w = pipes
    held: list[int] = []
    s1_sent = asyncio.Event()

    def script(req: dict[str, Any]) -> list[dict[Any, Any]]:
        if req.get("sessionId") == "S1":  # still in flight when S2 detaches
            held.append(req["id"])
            s1_sent.set()
            return []
        return [
            {"method": "Target.detachedFromTarget", "params": {"sessionId": "S2", "targetId": "T2"}},
            {"id": held[0], "result": {"ok": True}},
        ]

    fake = asyncio.create_task(_fake_chromium(a_w, b_r, script))
    t = PipeTransport()
    await t.start(read_fd=a_r, write_fd=b_w)
    s1 = asyncio.create_task(t.call("Page.getNavigationHistory", session_id="S1", timeout=5.0))
    await asyncio.wait_for(s1_sent.wait(), 5.0)
    with pytest.raises(CdpError):
        await t.call("Page.getNavigationHistory", session_id="S2", timeout=5.0)
    assert await s1 == {"ok": True}
    await t.close()
    fake.cancel()
