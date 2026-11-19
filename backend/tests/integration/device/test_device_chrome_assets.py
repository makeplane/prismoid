# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome's first assets and the asset fence: what a listed page may load off the list, with
the pass a task holds, redirects, HTTP/2 and the shell proxy. Sites and the helper fixture are
`tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import struct
from dataclasses import dataclass

import pytest

from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    BLOCKED,
    PNG,
    Helper,
    at,
    chrome_eval,
    chrome_page,
    settled,
    tls_context,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


@darwin_only
@needs_chrome
async def test_a_page_renders_its_style_image_font_and_script_from_a_host_off_the_list(
    helper: Helper, caplog: pytest.LogCaptureFixture
) -> None:
    """The live run's goodreads: a page bare text because its CDN was refused. An asset from a host
    on no list loads, and it is no denial, no log line and no widening of anything."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    listed = set(helper.app.state.registry.frames[ref].allow)
    cdp = await helper.dial(ref)
    try:
        with caplog.at_level(logging.INFO):
            _, session = await chrome_page(cdp)
            await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/styled"}, session_id=session)
            for path in ("/s.css", "/i.png", "/f.woff2", "/s.js"):
                await until(lambda path=path: helper.site.saw("b.test", path), path)
            await until(lambda: helper.site.saw("a.test", "/styled"), "the page")
            drawn = await chrome_eval(
                cdp,
                session,
                "(async () => { while (document.readyState !== 'complete')"
                " await new Promise(r => setTimeout(r, 20));"
                " return [getComputedStyle(document.body).color,"
                " document.getElementById('i').naturalWidth, window.assetRan === true]; })()",
            )
        assert drawn == ["rgb(1, 2, 3)", 1, True]
        assert not helper.chrome_denials(ref)
        assert helper.app.state.registry.frames[ref].allow == listed
        assert "b.test" not in caplog.text
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_every_load_of_a_listed_page_gets_its_first_assets(helper: Helper) -> None:
    """The excuse waits for the commit, and a page's first assets come right behind it: were they to
    beat it, a listed page would lose its CSS at random. Each load here is fresh, and each asset new."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        for n in range(30):
            await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/styled-{n}"}, session_id=session)
            # All four, before the next load: a font still due then would be the page being left's.
            for name in (f"/s-{n}.css", f"/i-{n}.png", f"/f-{n}.woff2", f"/s-{n}.js"):
                await until(lambda name=name: helper.site.saw("b.test", name), f"load {n}: {name}")
        assert not helper.chrome_denials(ref)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_tab_the_task_opens_gets_its_first_assets(helper: Helper) -> None:
    """A new tab's navigation starts as it is attached, before its own session could hear the commit."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        await chrome_page(cdp)
        await cdp.call("Target.createTarget", {"url": f"http://a.test:{port}/styled-tab"})
        await until(lambda: helper.site.saw("b.test", "/s-tab.js"), "the tab's script")
        assert not helper.chrome_denials(ref)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_fetch_xhr_websocket_iframe_and_navigation_to_an_asset_host_are_refused(
    helper: Helper,
) -> None:
    """An asset's pass is for assets: what a script reads back, a frame, a socket or a navigation to
    that same host, in the same seconds, meets the fence as if the image had never loaded."""
    port = helper.site.port
    other = f"http://b.test:{port}"
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/"}, session_id=session)
        await until(lambda: helper.site.saw("b.test", "/from-a.test.png"), "the asset passed")

        tried = await chrome_eval(
            cdp, session, f"fetch('{other}/fetched').then(() => 'reached', () => 'refused')"
        )
        assert tried == "refused"
        tried = await chrome_eval(
            cdp,
            session,
            "new Promise(done => { const x = new XMLHttpRequest(); x.onload = () => done('reached');"
            f" x.onerror = () => done('refused'); x.open('GET', '{other}/xhr'); x.send(); }})",
        )
        assert tried == "refused"
        assert (
            await chrome_eval(cdp, session, settled(f"new WebSocket('ws://b.test:{port}/socket')"))
            == "failed"
        )
        await chrome_eval(
            cdp,
            session,
            "new Promise(done => { const f = document.createElement('iframe'); f.onload = () => done(0);"
            f" f.onerror = () => done(0); f.src = '{other}/framed'; document.body.append(f); }})",
        )
        nav = await cdp.call("Page.navigate", {"url": f"{other}/navigated"}, session_id=session)
        assert nav.get("errorText") == BLOCKED

        await asyncio.sleep(0.5)
        for path in ("/fetched", "/xhr", "/socket", "/framed", "/navigated"):
            assert not helper.site.saw("b.test", path), path
        assert helper.chrome_denials(ref) and {d["host"] for d in helper.chrome_denials(ref)} == {"b.test"}
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_websocket_to_a_listed_host_still_reaches_it(helper: Helper) -> None:
    """A WebSocket's 407 reaches no CDP event, so the socket door's credential has to be in Chrome's
    cache before a page's first socket: here nothing plain has gone out, on a blank page."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call(
            "Runtime.evaluate",
            {"expression": f"new WebSocket('ws://a.test:{helper.site.port}/listed-socket'); 0"},
            session_id=session,
        )
        await until(lambda: helper.site.saw("a.test", "/listed-socket"), "the socket reached its host")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_an_image_at_a_private_address_or_a_loopback_port_not_its_own_is_refused(
    helper: Helper,
) -> None:
    """Only the list is excused for an asset: an address inside the network, or a port of this Mac
    the task did not open, stays refused, and stays a denial. The denials are the whole proof: this
    file's seam resolves nothing but `.test`, so no request here could reach the site anyway."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/link-x")
        await chrome_eval(
            cdp,
            session,
            f"new Image().src = 'http://10.1.2.3/p.png'; new Image().src = 'http://127.0.0.1:{port}/l.png'; 0",
        )
        await until(lambda: len(helper.chrome_denials(ref)) >= 2, "both refused")
        assert {d["host"] for d in helper.chrome_denials(ref)} == {"10.1.2.3", "127.0.0.1"}
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_an_asset_redirect_is_judged_again_at_each_hop(helper: Helper) -> None:
    """Each hop is its own pause: an image may hop to another public host, never inward."""
    port = helper.site.port
    helper.site.hops = {
        "/out.png": f"http://c.test:{port}/landed.png",
        "/in.png": "http://10.1.2.3/inward.png",
    }
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/link-x")
        for path in ("/out.png", "/in.png"):
            await chrome_eval(cdp, session, f"new Image().src = 'http://b.test:{port}{path}'; 0")
        await until(lambda: helper.site.saw("c.test", "/landed.png"), "the public hop")
        await until(lambda: helper.chrome_denials(ref), "the inward hop refused")
        assert [d["host"] for d in helper.chrome_denials(ref)] == ["10.1.2.3"]
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_the_frames_shell_proxy_still_refuses_a_host_passed_as_an_asset(helper: Helper) -> None:
    """A pass is Chrome's alone: the task's own commands, on the same frame, meet its list."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/"}, session_id=session)
        await until(lambda: helper.site.saw("b.test", "/from-a.test.png"), "the asset passed")
        command = f"curl -sS -m 5 -o /dev/null http://b.test:{port}/from-shell; curl -sS -m 5 https://b.test:{port}/"
        async with helper.client() as c:
            r = await c.post(f"/frames/{ref}/exec", json={"exec_id": "e1", "command": command, "timeout": 20})
        assert r.status_code == 200 and r.json()["exit_code"] != 0, r.text
        assert not helper.site.saw("b.test", "/from-shell")
        refused = [d for d in helper.chrome_denials(ref) if d["host"] == "b.test"]
        assert len(refused) == 2 and all("not in allowlist" in d["reason"] for d in refused)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_an_asset_host_passed_for_one_task_gives_the_other_no_fetch(helper: Helper) -> None:
    """A pass is the proxy's and it is global: with A's live, the proxy would forward B's request.
    What stops it is B's own fence, judging a Fetch as a Fetch, so this holds with the pass live."""
    port = helper.site.port
    a = await helper.dial(await helper.frame("ta", "a.test"))
    ref_b = await helper.frame("tb", "c.test")
    b = await helper.dial(ref_b)
    try:
        _, session_a = await chrome_page(a)
        await a.call("Page.navigate", {"url": f"http://a.test:{port}/"}, session_id=session_a)
        await until(lambda: helper.site.saw("b.test", "/from-a.test.png"), "A's asset passed")
        _, session_b = await chrome_page(b)
        await b.call("Page.navigate", {"url": f"http://c.test:{port}/link-x"}, session_id=session_b)
        await until(lambda: helper.site.saw("c.test", "/link-x"), "B's page")
        passed = helper.app.state.egress._passed  # pyright: ignore[reportPrivateUsage]
        assert passed(False, "chrome", "b.test", port, False), "the outer layer would let it through"
        tried = await chrome_eval(
            b, session_b, f"fetch('http://b.test:{port}/b-fetched').then(() => 'reached', () => 'refused')"
        )
        assert tried == "refused" and not helper.site.saw("b.test", "/b-fetched")
        assert [d["host"] for d in helper.chrome_denials(ref_b)] == ["b.test"]
    finally:
        await a.close()
        await b.close()


@dataclass
class H2Site:
    """Just enough HTTP/2 for one image, over TLS, advertising extended CONNECT (RFC 8441), which is
    what lets Chrome put a `wss:` on an open session. It counts the requests on its sessions."""

    requests: int = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def frame(kind: int, flags: int, stream: int, body: bytes = b"") -> bytes:
            return struct.pack(">I", len(body))[1:] + bytes([kind, flags]) + struct.pack(">I", stream) + body

        with contextlib.suppress(OSError, asyncio.IncompleteReadError, ssl.SSLError):
            await reader.readexactly(24)  # the client's preface
            writer.write(frame(4, 0, 0, struct.pack(">HI", 8, 1)))  # SETTINGS_ENABLE_CONNECT_PROTOCOL
            await writer.drain()
            while True:
                size, kind, flags, stream = (
                    struct.unpack(">I", b"\0" + await reader.readexactly(3))[0],
                    *struct.unpack(">BBI", await reader.readexactly(6)),
                )
                body = await reader.readexactly(size)
                if kind == 4 and not flags & 1:
                    writer.write(frame(4, 1, 0))
                elif kind == 6 and not flags & 1:
                    writer.write(frame(6, 1, 0, body))
                elif kind == 1:
                    self.requests += 1
                    writer.write(frame(1, 4, stream, b"\x88"))  # END_HEADERS, `:status: 200`
                    writer.write(frame(0, 1, stream, PNG))  # END_STREAM
                await writer.drain()


@darwin_only
@needs_chrome
async def test_a_websocket_cannot_ride_the_http2_session_an_asset_opened(helper: Helper) -> None:
    """The way past a host check: Chrome puts a `wss:` on an open HTTP/2 session to its host, inside
    a tunnel the proxy cannot read, if the server offers extended CONNECT. Sockets have their own
    proxy door, and Chrome keys a session by its proxy, so this one has none to share."""
    site = H2Site()
    server = await asyncio.start_server(site.handle, "127.0.0.1", 0, ssl=tls_context(helper.home)[0])
    h2_port = server.sockets[0].getsockname()[1]
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        own = helper.chrome.fence_session(target)
        await helper.chrome.call("Security.setIgnoreCertificateErrors", {"ignore": True}, session=own)
        await at(cdp, session, helper, "a.test", "/link-x")
        loaded = await chrome_eval(
            cdp,
            session,
            "new Promise(done => { const i = new Image(); i.onload = () => done(true);"
            f" i.onerror = () => done(false); i.src = 'https://h.test:{h2_port}/i.png'; }})",
        )
        assert (loaded, site.requests, helper.chrome_denials(ref)) == (True, 1, [])  # the session is open
        said = await chrome_eval(cdp, session, settled(f"new WebSocket('wss://h.test:{h2_port}/socket')"))
        assert said == "failed"
        assert site.requests == 1
    finally:
        await cdp.close()
        server.close()


@darwin_only
@needs_chrome
async def test_a_fenced_command_reaches_neither_the_socket_door_nor_its_port(helper: Helper) -> None:
    """The socket door is Chrome's. A real fenced command can neither dial it, where it would be
    challenged, nor bind it. Its own proxy door still answers, so the refusal is the fence's."""
    egress = helper.app.state.egress
    ref = await helper.frame("ta", "a.test")
    code = "curl -sS -m 4 -o /dev/null -w '%{http_code}' --noproxy '*'"
    bind = f"s = __import__('socket').socket(); s.bind(('127.0.0.1', {egress.socket_port}))"
    command = (
        f"echo door-a=$({code} http://127.0.0.1:{egress.port}/ 2>/dev/null);"
        f" echo door-b=$({code} http://127.0.0.1:{egress.socket_port}/ 2>/dev/null);"
        f' /usr/bin/python3 -c "{bind}" 2>&1 | tail -1'
    )
    async with helper.client() as c:
        r = await c.post(f"/frames/{ref}/exec", json={"exec_id": "door", "command": command, "timeout": 20})
    out = r.json()["stdout"]
    assert "door-a=407" in out, out
    assert "door-b=000" in out, out
    assert "Operation not permitted" in out, out
