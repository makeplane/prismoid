# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The `CdpConnection` contract (a remote browser's contract): what the
verbs (`BrowserVerbs`) rely on from ANY connection, pinned once per connection type - the daemon's
pipe to the Chromium it launched (`PipeTransport`) and the worker's WebSocket to a computer's
browser (`WsCdp`). A refusal is a `CdpError` with its code and message; a dead connection is
`CdpClosed`, in flight and after; a detached tab's calls fail at once, not at their timeout;
handlers run synchronously, in order, before the next message; a handler that raises breaks
nothing."""

from __future__ import annotations

import asyncio
import gc
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed

from prismoid.ai.frames.cdp_ws import GATE_CLOSE, WsCdp
from prismoid.ai.frames.image_browser.sidecar.cdp import CdpClosed, CdpConnection, CdpError
from prismoid.ai.frames.image_browser.sidecar.verbs import BrowserVerbs
from tests.support.fakes.cdp_peer import PipePeer, WsPeer
from tests.support.fakes.cdp_scripts import AX_BUTTON, FakeCdp, ref_for

# pyright checks this: a connection that drifts from what the verbs speak fails the type check.
_FITS: list[type[CdpConnection]] = [WsCdp]


class _Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Script:
    """A Chromium that answers each method with a scripted reply or refusal, `{}` otherwise."""

    def __init__(self) -> None:
        self.replies: dict[str, dict[str, Any]] = {}
        self.refusals: dict[str, tuple[int, str]] = {}

    async def __call__(self, method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        if method in self.refusals:
            raise _Refused(*self.refusals[method])
        return self.replies.get(method, {})


async def _until(ready: Callable[[], object], patience: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + patience
    while not ready():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.01)


Wired = tuple[Script, PipePeer | WsPeer, CdpConnection]


@pytest.fixture(params=["pipe", "ws"])
async def wired(request: pytest.FixtureRequest) -> AsyncIterator[Wired]:
    script = Script()
    peer: PipePeer | WsPeer = PipePeer(script) if request.param == "pipe" else WsPeer(script)
    conn = await peer.connect()
    try:
        yield script, peer, conn
    finally:
        await conn.close()
        await peer.aclose()


async def test_a_reply_resolves_its_call(wired: Wired) -> None:
    script, _, conn = wired
    script.replies["Browser.getVersion"] = {"product": "Chrome/1"}
    assert await conn.call("Browser.getVersion") == {"product": "Chrome/1"}


async def test_a_refusal_is_a_cdp_error_with_its_code_and_message(wired: Wired) -> None:
    script, _, conn = wired
    script.refusals["DOM.focus"] = (-32000, "Element is not focusable")
    with pytest.raises(CdpError) as refused:
        await conn.call("DOM.focus", {"backendNodeId": 7})
    assert (refused.value.code, refused.value.message) == (-32000, "Element is not focusable")
    assert await conn.call("Browser.getVersion") == {}, "a refusal leaves the connection usable"


async def test_a_call_carries_its_method_params_and_session(wired: Wired) -> None:
    _, peer, conn = wired
    await conn.call("Page.enable", {"x": 1}, session_id="S1")
    sent = peer.received[-1]
    assert (sent["method"], sent["params"], sent["sessionId"]) == ("Page.enable", {"x": 1}, "S1")


async def test_an_event_reaches_every_handler_before_the_reply_that_follows_it(wired: Wired) -> None:
    _, peer, conn = wired
    seen: list[str] = []
    conn.on("Page.loadEventFired", lambda msg: seen.append(f"first {msg.get('sessionId')}"))
    conn.on("Page.loadEventFired", lambda msg: seen.append("second"))
    hold = peer.holds["Page.navigate"] = asyncio.Event()
    call = asyncio.create_task(conn.call("Page.navigate", {"url": "about:blank"}))
    await _until(lambda: any(m.get("method") == "Page.navigate" for m in peer.received))
    await peer.emit("Page.loadEventFired", {}, session="S1")
    hold.set()
    await call
    assert seen == ["first S1", "second"], "both handlers ran, in order, before the reply resolved"


async def test_a_handler_that_raises_never_breaks_the_connection(wired: Wired) -> None:
    _, peer, conn = wired
    seen: list[str] = []

    def boom(msg: dict[str, Any]) -> None:
        raise KeyError("a reply missing a field")

    conn.on("Target.targetCreated", boom)
    conn.on("Target.targetCreated", lambda msg: seen.append(msg["params"]["targetInfo"]["targetId"]))
    await peer.emit("Target.targetCreated", {"targetInfo": {"targetId": "T2"}})
    assert await conn.call("Browser.getVersion") == {}
    assert seen == ["T2"]


async def test_a_detached_tab_fails_its_calls_at_once_and_spares_the_others(wired: Wired) -> None:
    _, peer, conn = wired
    peer.holds["DOM.getDocument"] = asyncio.Event()  # Chromium never answers a tab that closed
    on_s2 = asyncio.create_task(conn.call("DOM.getDocument", session_id="S2", timeout=30))
    on_s1 = asyncio.create_task(conn.call("DOM.getDocument", session_id="S1", timeout=30))
    await _until(lambda: sum(m.get("method") == "DOM.getDocument" for m in peer.received) == 2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await peer.emit("Target.detachedFromTarget", {"sessionId": "S2"})
    with pytest.raises(CdpError) as closed:
        await on_s2
    assert closed.value.code == -32001 and loop.time() - started < 5, "failed at once, not at 30 s"
    assert not on_s1.done()
    peer.holds["DOM.getDocument"].set()
    assert await on_s1 == {}


async def test_the_peer_going_away_fails_every_call_with_cdp_closed(wired: Wired) -> None:
    _, peer, conn = wired
    peer.holds["Page.navigate"] = asyncio.Event()
    inflight = asyncio.create_task(conn.call("Page.navigate", {"url": "about:blank"}))
    await _until(lambda: peer.received)
    await peer.drop()
    with pytest.raises(CdpClosed):
        await asyncio.wait_for(inflight, 5)
    with pytest.raises(CdpClosed):
        await conn.call("Browser.getVersion")


async def test_after_close_a_call_raises_cdp_closed(wired: Wired) -> None:
    _, _, conn = wired
    await conn.close()
    with pytest.raises(CdpClosed):
        await conn.call("Browser.getVersion")


async def test_a_person_taking_the_screen_is_readable_from_the_close_code() -> None:
    """A remote CDP gate closes every client of the browser 1013 when a person takes the
    screen: the owner tells that apart from a dropped connection."""
    peer = WsPeer(Script())
    conn = await peer.connect()
    try:
        assert conn.close_code is None
        await peer.drop(GATE_CLOSE, "a person took over the screen")
        await _until(lambda: conn.close_code is not None)
        assert conn.close_code == GATE_CLOSE
        with pytest.raises(CdpClosed):
            await conn.call("Browser.getVersion")
    finally:
        await conn.close()
        await peer.aclose()


async def test_the_ticket_rides_the_dialled_url_and_nothing_else() -> None:
    peer = WsPeer(Script())
    conn = await peer.connect()
    try:
        assert peer.path == "/v1/computers/cmp_test/browser/cdp?ticket=tk_secret"
    finally:
        await conn.close()
        await peer.aclose()


@pytest.mark.parametrize("kind", ["pipe", "ws"])
async def test_the_verbs_snapshot_and_click_by_reference_over_either_connection(kind: str) -> None:
    """The verbs run once per connection type, on the same recorded Chromium."""
    fake = FakeCdp()
    fake.answer(
        "Target.getTargets", {"targetInfos": [{"targetId": "T1", "type": "page", "url": "about:blank"}]}
    )
    fake.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F1"}}})
    fake.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON})

    async def answer(method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        return await fake.call(method, params, session_id=session)

    peer: PipePeer | WsPeer = PipePeer(answer) if kind == "pipe" else WsPeer(answer)
    conn = await peer.connect()
    try:
        verbs = BrowserVerbs(conn)
        await verbs.attach()
        snap = await verbs.handle({"verb": "snapshot"})
        assert snap["ok"] and snap["epoch"] == 1
        clicked = await verbs.handle({"verb": "click", "ref": ref_for(snap, 'button "Save changes"')})
        assert clicked["ok"] and clicked["_record"]["jpeg"] == b"\xff\xd8\xff"
        assert set(fake.sent_on("Input.dispatchMouseEvent")) == {"S1"}
    finally:
        await conn.close()
        await peer.aclose()


async def test_a_call_whose_send_fails_leaves_no_unretrieved_exception() -> None:
    """A call that dies inside `send` (the peer dropped before the pump noticed, or the pump died
    while the send was in flight) raises `CdpClosed` once; its future holds no second, unread one."""
    loop = asyncio.get_running_loop()
    leaks: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: leaks.append(context))
    peer = WsPeer(Script())
    conn = await peer.connect()
    try:
        assert isinstance(conn, WsCdp)
        gate, entered = asyncio.Event(), asyncio.Event()

        async def dying_send(_data: str) -> None:
            entered.set()
            await gate.wait()  # still inside send when the pump dies
            raise ConnectionClosed(None, None)

        conn._ws.send = dying_send  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
        call = asyncio.create_task(conn.call("Browser.getVersion"))
        await entered.wait()
        await peer.drop()
        await _until(lambda: conn.close_code is not None)
        gate.set()
        with pytest.raises(CdpClosed):
            await call
        # a call made after the peer dropped but before the pump noticed: send itself fails
        await conn.close()
        peer2 = WsPeer(Script())
        conn = await peer2.connect()
        assert isinstance(conn, WsCdp)

        async def refused_send(_data: str) -> None:
            raise ConnectionClosed(None, None)

        conn._ws.send = refused_send  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
        with pytest.raises(CdpClosed):
            await conn.call("Browser.getVersion")
        await peer2.aclose()
        del call
        gc.collect()
        await asyncio.sleep(0)
        assert [c for c in leaks if "never retrieved" in str(c.get("message"))] == []
    finally:
        loop.set_exception_handler(None)
        await conn.close()
        await peer.aclose()


async def test_a_malformed_relay_frame_never_kills_the_pump() -> None:
    peer = WsPeer(Script())
    conn = await peer.connect()
    try:
        for params in ("not a dict", None, ["x"], 7):
            await peer.emit("Target.detachedFromTarget", params)  # pyright: ignore[reportArgumentType]
        for bad_id in ([1], {"a": 1}, "7", None, 1.5, True):
            await peer.send_raw({"id": bad_id, "result": {}})
        await peer.send_raw({"method": ["unhashable"], "params": {}})
        assert await conn.call("Browser.getVersion") == {}
        assert conn.close_code is None
    finally:
        await conn.close()
        await peer.aclose()


async def test_a_failed_dial_names_neither_the_ticket_nor_the_host() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    for url in (
        f"ws://127.0.0.1:{port}/v1/computers/cmp_x/browser/cdp?ticket=tk_secret",
        "not a url?ticket=tk_secret",
        f"http://127.0.0.1:{port}/cdp?ticket=tk_secret",
    ):
        with pytest.raises(ConnectionError) as failed:
            await WsCdp.connect(url, open_timeout=2)
        text = f"{failed.value} {failed.value!r}"
        for leak in ("tk_secret", "127.0.0.1", str(port), "/cdp", "cmp_x"):
            assert leak not in text, (url, leak)
        assert failed.value.__cause__ is None and failed.value.__suppress_context__


async def test_a_refusal_with_a_malformed_code_still_resolves_its_call() -> None:
    peer = WsPeer(Script())
    conn = await peer.connect()
    try:
        for bad_code, bad_message in (("x", "odd"), (None, None), ([1], 7), (float("inf"), "big")):
            peer.holds["Page.navigate"] = asyncio.Event()  # Chromium has not answered: we answer for it
            call = asyncio.create_task(conn.call("Page.navigate"))
            await _until(lambda: peer.received and peer.received[-1].get("method") == "Page.navigate")
            await peer.send_raw(
                {"id": peer.received[-1]["id"], "error": {"code": bad_code, "message": bad_message}}
            )
            with pytest.raises(CdpError) as refused:
                await asyncio.wait_for(call, 1)
            assert refused.value.code == 0 and refused.value.message == str(bad_message)
            peer.received.clear()
        del peer.holds["Page.navigate"]
        assert await conn.call("Browser.getVersion") == {}
    finally:
        await conn.close()
        await peer.aclose()
