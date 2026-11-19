# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A service worker in the shared profile never answers a task's page: not
its first navigation, not one in no task's window. A worker answers past `Fetch`, so what it served
reaches a page no list would have let it. Sites and the helper fixture are `tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from prismoid.device import chrome_pipe as chrome_module
from prismoid.device.chrome import BASE, LAUNCH_FLAGS
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import Helper, chrome_eval, chrome_page, tls_context, until
from tests.support.rigs.chrome_rig import helper as helper

_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => event.respondWith(
  new Response('from the worker', {headers: {'Content-Type': 'text/html'}})));
"""


async def _site(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        path = head.split(" ", 2)[1]
        kind, body = ("text/javascript", _WORKER) if path == "/sw.js" else ("text/html", "from the network")
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}".encode()
        )
        await writer.drain()
    writer.close()


@dataclass
class Worked:
    """A worker kept in the profile for `a.test`, from an earlier task; a task on `b.test`."""

    port: int
    ref: str
    page: str
    session: str


@pytest.fixture
async def worked(helper: Helper, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Worked]:
    context, pin = tls_context(helper.home, "http/1.1")
    flags = (*LAUNCH_FLAGS, f"--ignore-certificate-errors-spki-list={pin}")
    monkeypatch.setattr(chrome_module, "LAUNCH_FLAGS", flags)
    server = await asyncio.start_server(_site, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    earlier = await helper.frame("earlier", "a.test")
    cdp = await helper.dial(earlier)
    _, session = await chrome_page(cdp)
    await cdp.call("Page.navigate", {"url": f"https://a.test:{port}/first"}, session_id=session)
    await until(lambda: "/first" in str(helper.chrome.tops_of(earlier)), "the earlier task's page")
    state = await chrome_eval(
        cdp,
        session,
        "navigator.serviceWorker.register('/sw.js')"
        ".then(() => navigator.serviceWorker.ready).then(r => r.active.state)",
    )
    assert state in ("activating", "activated"), "the probe needs a live worker"
    await cdp.close()

    ref = await helper.frame("ta", "b.test")
    cdp = await helper.dial(ref)
    page, session = await chrome_page(cdp)
    await cdp.call("Page.navigate", {"url": f"https://b.test:{port}/here"}, session_id=session)
    await until(lambda: "/here" in str(helper.chrome.tops_of(ref)), "the task's page")
    await cdp.close()
    try:
        yield Worked(port, ref, page, helper.chrome.fence_session(page))
    finally:
        server.close()


async def _conjures(helper: Helper, worked: Worked, key: str) -> str:
    """A page's synthetic modified click: a tab or a window with no opener. The new target."""
    chrome = helper.chrome
    before = chrome.pages_listed()
    click = (
        "const a = document.createElement('a');"
        f"a.href = 'https://a.test:{worked.port}/third'; document.body.append(a);"
        f"a.dispatchEvent(new MouseEvent('click', {{{key}: true, bubbles: true, cancelable: true}})); 0"
    )
    await chrome.call("Runtime.evaluate", {"expression": click, "userGesture": True}, session=worked.session)
    new = await until(lambda: chrome.pages_listed() - before, "the new page")
    return next(iter(new))


async def _body(helper: Helper, page: str) -> str:
    """What the page shows, through a session of the test's own: a base page keeps no fence session."""
    chrome = helper.chrome
    session = (await chrome.call("Target.attachToTarget", {"targetId": page, "flatten": True}))["sessionId"]
    await asyncio.sleep(1.0)
    said = await chrome.call(
        "Runtime.evaluate",
        {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True},
        session=session,
    )
    return str(said["result"].get("value"))


@darwin_only
@needs_chrome
async def test_a_popups_first_navigation_is_not_a_workers(helper: Helper, worked: Worked) -> None:
    """The agent drives a popup, so this is the one it could read."""
    chrome = helper.chrome
    before = chrome.pages_listed()
    opened = f"window.open('https://a.test:{worked.port}/popup'); 0"
    await chrome.call("Runtime.evaluate", {"expression": opened, "userGesture": True}, session=worked.session)
    popup = next(iter(await until(lambda: chrome.pages_listed() - before, "the popup")))
    assert "from the worker" not in await _body(helper, popup)


@darwin_only
@needs_chrome
async def test_a_tabs_first_navigation_is_not_a_workers(helper: Helper, worked: Worked) -> None:
    tab = await _conjures(helper, worked, "metaKey")
    await until(lambda: tab in helper.chrome.pages_of_task(worked.ref), "the tab, in the task's window")
    assert "from the worker" not in await _body(helper, tab)


@darwin_only
@needs_chrome
async def test_a_page_in_no_tasks_window_is_not_a_workers(helper: Helper, worked: Worked) -> None:
    window = await _conjures(helper, worked, "shiftKey")
    assert BASE and window not in helper.chrome.pages_of_task(worked.ref)
    assert "from the worker" not in await _body(helper, window)


@darwin_only
@needs_chrome
async def test_a_page_in_no_tasks_window_does_not_go_through_one_later(
    helper: Helper, worked: Worked
) -> None:
    """Its next navigation, after a refused first: a worker would make the base list the union."""
    window = await _conjures(helper, worked, "shiftKey")
    chrome = helper.chrome
    session = (await chrome.call("Target.attachToTarget", {"targetId": window, "flatten": True}))["sessionId"]
    await chrome.call("Page.navigate", {"url": f"https://a.test:{worked.port}/fourth"}, session=session)
    assert "from the worker" not in await _body(helper, window)
