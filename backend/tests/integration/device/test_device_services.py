# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task's servers, for real: the helper on its Unix
socket runs fenced commands that start servers; the task's own Prismoid Chrome window reaches them on
loopback and no other task's window does, nor any window Prismoid's own ports.

Everything a test starts is its own: servers run inside its frames and die with them (each pid is
also killed in teardown, by its group, only while its start time still proves it ours), containers
wear the frame's label and only those are removed. Tests that need Chrome or Docker skip without
them. This file is what a window reaches; the rig is `tests/support/rigs/device_services_rig.py`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.conf import settings
from prismoid.egress_relay import LOOPBACK_REFUSED
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.device_services_rig import (
    BLOCKED,
    ECHO,
    SIX,
    Helper,
    free_port,
    listening,
    own,
    page,
    until,
)
from tests.support.rigs.device_services_rig import helper as helper
from tests.support.rigs.device_services_rig import stranger_on_loopback as stranger_on_loopback

pytestmark = darwin_only


@needs_chrome
async def test_task_window_reaches_own_port_only(helper: Helper) -> None:
    a = await helper.frame("ta", profile="browser")
    b = await helper.frame("tb", profile="browser")
    port = free_port()
    (Path(helper.state(a).paths.work) / "echo.py").write_text(ECHO)
    await helper.spawn(a, f"exec /usr/bin/python3 echo.py {port}")
    await until(lambda: listening(port), "the frame's server")
    cdp_a, cdp_b = await helper.dial(a), await helper.dial(b)
    try:
        session = await page(cdp_a)
        nav = await cdp_a.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"}, session_id=session)
        assert "errorText" not in nav, nav
        assert await _until_value(cdp_a, session, "document.title") == "echo"
        # The page's own POST arrives whole, through the proxy's forward: its body, its headers.
        script = "fetch('/x', {method: 'POST', body: 'hi', headers: {'X-Probe': 'yes'}}).then(r => r.text())"
        assert await _eval(cdp_a, session, script, awaited=True) == "hi|yes"

        session_b = await page(cdp_b)
        nav = await cdp_b.call("Page.navigate", {"url": f"http://localhost:{port}/"}, session_id=session_b)
        assert nav.get("errorText") == BLOCKED
        assert {"ref": b, "host": "localhost", "reason": LOOPBACK_REFUSED} in helper.state(b).denials
        # A port nobody opened is refused too, after one look afresh.
        nav = await cdp_a.call(
            "Page.navigate", {"url": f"http://127.0.0.1:{free_port()}/"}, session_id=session
        )
        assert nav.get("errorText") == BLOCKED
    finally:
        await cdp_a.close()
        await cdp_b.close()


@needs_chrome
async def test_task_window_never_reaches_prismoid_api_port(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prismoid's API port and the helper's own proxy port are never a frame's, even with a frame's
    own server listening on the one the API is configured on."""
    a = await helper.frame("ta", profile="browser")
    port = free_port()
    await helper.serve(a, port)
    cdp = await helper.dial(a)
    try:
        session = await page(cdp)
        nav = await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"}, session_id=session)
        assert "errorText" not in nav, nav  # its own port, until that port is Prismoid's
        monkeypatch.setattr(settings.local, "api_port", port)
        helper.app.state.ports.forget(a)
        for target in (port, helper.app.state.egress.port):
            nav = await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{target}/"}, session_id=session)
            assert nav.get("errorText") == BLOCKED, target
    finally:
        await cdp.close()


async def _eval(cdp: WsCdp, session: str, expression: str, *, awaited: bool = False) -> Any:
    got = await cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": awaited},
        session_id=session,
    )
    return got.get("result", {}).get("value")


async def _until_value(cdp: WsCdp, session: str, expression: str) -> Any:
    for _ in range(200):
        if value := await _eval(cdp, session, expression):
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"never a value: {expression}")


async def _echo(helper: Helper, ref: str, *ports: int) -> None:
    """The frame's own dev server on each of `ports`."""
    (Path(helper.state(ref).paths.work) / "echo.py").write_text(ECHO)
    for port in ports:
        await helper.spawn(ref, f"exec /usr/bin/python3 echo.py {port}")
        await until(lambda port=port: listening(port), f"the frame's server on {port}")


def _hits(helper: Helper, ref: str, port: int) -> str:
    log = Path(helper.state(ref).paths.work) / f"hits-{port}.log"
    return log.read_text() if log.exists() else ""


async def _socket(cdp: WsCdp, session: str, url: str) -> str:
    """The page opens a WebSocket: `open`, or `error`."""
    script = (
        f"new Promise(done => {{ const s = new WebSocket({json.dumps(url)}); "
        "s.onopen = () => done('open'); s.onerror = () => done('error'); })"
    )
    return await _eval(cdp, session, script, awaited=True)


@needs_chrome
async def test_a_windows_websocket_reaches_its_own_frames_ports_only(helper: Helper) -> None:
    """C6's two-window probe, and what it must not break: a page on a frame's port opens a socket
    to that port (HMR) or a sibling port of the same frame; a socket to another frame's port never
    arrives, though Fetch does not see a handshake."""
    a, b = await helper.frame("ta", profile="browser"), await helper.frame("tb", profile="browser")
    x, sibling, y = free_port(), free_port(), free_port()
    await _echo(helper, a, x, sibling)
    await _echo(helper, b, y)
    cdp = await helper.dial(a)
    try:
        session = await page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{x}/"}, session_id=session)
        assert await _until_value(cdp, session, "document.title") == "echo"
        assert await _socket(cdp, session, f"ws://127.0.0.1:{x}/hmr") == "open"
        assert await _socket(cdp, session, f"ws://localhost:{sibling}/hmr-sibling") == "open"
        assert await _socket(cdp, session, f"ws://127.0.0.1:{y}/ws-from-A?stolen=1") == "error"
        assert await _socket(cdp, session, f"wss://127.0.0.1:{x}/tls") == "error"
        assert "/hmr" in _hits(helper, a, x) and "/hmr-sibling" in _hits(helper, a, sibling)
        assert _hits(helper, b, y) == ""
    finally:
        await cdp.close()


@needs_chrome
async def test_a_window_never_reaches_a_strangers_address_on_a_port_it_shares(
    helper: Helper, stranger_on_loopback: tuple[int, Path]
) -> None:
    """C5's probe: the frame binds `[::1]:Y` beside a stranger's `127.0.0.1:Y`; neither its window
    nor its proxy credential reaches the stranger."""
    port, log = stranger_on_loopback
    a = await helper.frame("ta", profile="browser")
    (Path(helper.state(a).paths.work) / "six.py").write_text(SIX)
    await helper.spawn(a, f"exec /usr/bin/python3 six.py {port}")
    await until(lambda: ("::1", port) in own(helper, a), "the frame's server on ::1")
    cdp = await helper.dial(a)
    try:
        session = await page(cdp)
        nav = await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/stolen"}, session_id=session)
        assert nav.get("errorText") == BLOCKED
        nav = await cdp.call("Page.navigate", {"url": f"http://localhost:{port}/mine"}, session_id=session)
        assert "errorText" not in nav, nav
        assert await _until_value(cdp, session, "document.title") == "six"
        assert "stolen" not in log.read_text() and "mine" not in log.read_text()
    finally:
        await cdp.close()
