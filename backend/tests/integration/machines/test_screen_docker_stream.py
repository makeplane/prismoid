# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker screen stream, end to end in a real browser frame: a hijacked exec of
the bridge brings the daemon's hello, its status (the page's origin with it) and real JPEG frames
down and takes a person's click up; a frame whose daemon never started answers `no_browser`;
a missing or stopped frame is `ScreenClosed`. Docker only, in the frames group,
never with the dev worker up."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.frames.screen import ScreenClosed, ScreenStream, open_docker_screen

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-d")]

URL = "http://127.0.0.1:8088/index.html"
PAGE = (
    "<!doctype html><title>Screen</title>"
    "<button style='position:fixed;inset:0;font-size:64px' onclick=\"document.title='Clicked'\">"
    "Click anywhere</button>"
)
CLICK: list[dict[str, Any]] = [
    {"t": "pointer", "type": "move", "x": 640, "y": 450, "button": "none", "buttons": 0},
    {"t": "pointer", "type": "down", "x": 640, "y": 450, "button": "left", "buttons": 1, "clicks": 1},
    {"t": "pointer", "type": "up", "x": 640, "y": 450, "button": "left", "buttons": 0, "clicks": 1},
]


@pytest.fixture
async def frame() -> AsyncIterator[DockerFrame]:
    ensure_image("browser")  # the bridge is in the image: its tag hashes every sidecar file
    f = DockerFrame(task_id=f"pytest-screen-{uuid.uuid4().hex[:8]}", profile="browser")
    await f.create()
    yield f
    await f.destroy()


async def _until(stream: ScreenStream, t: str, within: float = 20.0) -> tuple[dict[str, Any], bytes]:
    async def scan() -> tuple[dict[str, Any], bytes]:
        while True:
            header, payload = await stream.recv()
            if header["t"] == t:
                return header, payload

    return await asyncio.wait_for(scan(), within)


async def test_a_live_daemon_streams_jpeg_frames_down_and_takes_a_click_up(frame: DockerFrame) -> None:
    await frame.write_file("/work/site/index.html", PAGE)
    await frame.exec(
        "cd /work/site && (python3 -m http.server 8088 >/dev/null 2>&1 &) && "
        "for i in $(seq 1 40); do curl -sf http://127.0.0.1:8088/index.html >/dev/null && break; sleep 0.1; done"
    )
    # One verb starts the daemon, as a step's first browser call does.
    opened = await frame.browser({"verb": "open", "url": URL})
    assert opened["ok"], opened
    stream = await open_docker_screen(frame.ref)
    try:
        hello, _ = await asyncio.wait_for(stream.recv(), 20)
        status, _ = await asyncio.wait_for(stream.recv(), 20)
        assert hello["t"] == "hello" and hello["v"] == 1
        assert status["t"] == "status" and status["chromium"] == "running" and status["busy"] is False
        assert status["origin"] == "http://127.0.0.1:8088", "the page's origin, as Chromium reports its tab"
        await stream.send({"t": "hello", "v": 1})
        await stream.send({"t": "hint", "fps": 5, "quality": 60})
        first, jpeg = await _until(stream, "frame")
        assert jpeg.startswith(b"\xff\xd8") and first["fmt"] == "jpeg" and first["n"] == len(jpeg)
        assert 0 < first["w"] <= 1280 and 0 < first["h"] <= 900
        await stream.send({"t": "hint", "refresh": True})  # a still page sends no frame on its own
        second, _ = await _until(stream, "frame")
        assert second["seq"] > first["seq"]
        for event in CLICK:
            await stream.send(event)
        await asyncio.sleep(0.5)  # let the click land: input is dropped while a verb holds the lock
        snap = await frame.browser({"verb": "snapshot"})
        assert snap["ok"] and snap["title"] == "Clicked", snap
    finally:
        await stream.close()


async def test_a_frame_whose_daemon_never_started_says_no_browser(frame: DockerFrame) -> None:
    """The bridge's side, in the real image: a warm Machine before its step's first
    browser verb. The bridge says so and exits; it never starts the daemon."""
    stream = await open_docker_screen(frame.ref)
    try:
        bye, _ = await asyncio.wait_for(stream.recv(), 20)
        assert bye["t"] == "bye" and bye["reason"] == "no_browser"
        with pytest.raises(ScreenClosed):
            await asyncio.wait_for(stream.recv(), 20)
    finally:
        await stream.close()
    container = frame._container  # pyright: ignore[reportPrivateUsage]
    assert container is not None
    probe = await asyncio.to_thread(
        container.exec_run, ["test", "-S", "/run/browser/sidecar.sock"], user="1001"
    )
    assert probe.exit_code != 0  # still no daemon: a viewer never chose its environment


async def test_a_missing_or_stopped_frame_is_screen_closed(frame: DockerFrame) -> None:
    with pytest.raises(ScreenClosed):
        await open_docker_screen(f"pytest-screen-missing-{uuid.uuid4().hex[:8]}")
    container = frame._container  # pyright: ignore[reportPrivateUsage]
    assert container is not None
    await asyncio.to_thread(container.stop, timeout=0)
    with pytest.raises(ScreenClosed):
        await open_docker_screen(frame.ref)
