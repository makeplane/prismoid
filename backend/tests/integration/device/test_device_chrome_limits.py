# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome's limits and its give-ups: a request too large to read, a navigation that never
commits, a frame made as Chrome goes, a removed iframe's document. Sites and the helper fixture are
`tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from prismoid.ai.frames.image_browser.sidecar.cdp import CdpError
from prismoid.device.chrome import TOO_LARGE
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    Helper,
    at,
    chrome_eval,
    chrome_page,
    serve_own_page,
    stop_own_page,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper

_MIB = 1024 * 1024


_BODIES = {
    "string": f"'x'.repeat({28 * _MIB})",
    "array-buffer": f"new ArrayBuffer({28 * _MIB})",
    "blob": f"new Blob([new ArrayBuffer({28 * _MIB})])",
    "form-data": (
        f"(() => {{ const f = new FormData(); f.append('f', new File([new ArrayBuffer({28 * _MIB})], 'x.bin'));"
        " return f; })()"
    ),
}


def _post(body: str) -> str:
    return f"fetch('/up', {{method: 'POST', body: {body}}}).then(r => r.status, () => 'refused')"


@darwin_only
@needs_chrome
@pytest.mark.parametrize("body", _BODIES.values(), ids=_BODIES.keys())
async def test_a_request_too_large_to_read_is_refused_and_every_task_goes_on(
    helper: Helper, body: str
) -> None:
    """The deep review's: Chrome writes a body into the event twice, so 28 MiB took the one pipe down
    and every task's window with it. It is refused, with a listed target and before anything left."""
    ref, theirs = await helper.frame("ta", "a.test"), await helper.frame("tb", "b.test")
    cdp, other = await helper.dial(ref), await helper.dial(theirs)
    try:
        _, session = await chrome_page(cdp)
        _, their_session = await chrome_page(other)
        await at(cdp, session, helper, "a.test", "/page")
        await at(other, their_session, helper, "b.test", "/theirs")
        pid = helper.chrome.pid

        assert await chrome_eval(cdp, session, _post(body)) == "refused"

        assert helper.chrome.running and helper.chrome.pid == pid
        assert not helper.site.saw("a.test", "/up"), "refused before it left"
        assert helper.chrome_denials(ref)[-1] == {"ref": ref, "host": "a.test", "reason": TOO_LARGE}
        await other.call(
            "Page.navigate", {"url": f"http://b.test:{helper.site.port}/after"}, session_id=their_session
        )
        await until(lambda: helper.site.saw("b.test", "/after"), "the other task's next page")
        assert await chrome_eval(cdp, session, "fetch('/again').then(r => r.status)") == 200, "and this one's"
    finally:
        await cdp.close()
        await other.close()


@darwin_only
@needs_chrome
async def test_an_xhr_or_a_form_too_large_to_read_is_refused_too(helper: Helper) -> None:
    """An XHR is a request as a `fetch` is. A form leaves the page, and its refusal shows as Chrome's
    error page: the commit says so, and the page's next navigation is as any other."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        pid = helper.chrome.pid
        xhr = (
            "new Promise(done => { const x = new XMLHttpRequest(); x.open('POST', '/up-xhr');"
            " x.onload = () => done(x.status); x.onerror = () => done('refused');"
            f" x.send('x'.repeat({28 * _MIB})); }})"
        )
        assert await chrome_eval(cdp, session, xhr) == "refused"
        form = (
            "(() => { const f = document.createElement('form'); f.method = 'post'; f.action = '/up-form';"
            " const t = document.createElement('textarea'); t.name = 'a';"
            f" t.value = 'x'.repeat({28 * _MIB}); f.append(t); document.body.append(f); f.submit(); }})()"
        )
        await chrome_eval(cdp, session, form)
        await until(lambda: "chrome-error" in str(helper.chrome.tops()), "its error page")
        assert helper.chrome.running and helper.chrome.pid == pid
        assert not helper.site.saw("a.test", "/up-xhr") and not helper.site.saw("a.test", "/up-form")
        await at(cdp, session, helper, "a.test", "/next")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_request_under_the_limit_is_judged_as_ever_and_network_hears_no_body(helper: Helper) -> None:
    """`Network` was the one event of the three a parameter can shrink: 5 MiB gave 11.7 MiB, and its
    announcement needs no body. `Fetch` has no such parameter: its own copy is why the limit is there."""
    ref = await helper.frame("ta", "a.test")
    heard: list[dict[str, Any]] = []
    chrome = helper.chrome
    real = chrome._dispatch  # pyright: ignore[reportPrivateUsage]

    def spy(msg: dict[str, Any]) -> None:
        if msg.get("method") == "Network.requestWillBeSent":
            heard.append(msg["params"]["request"])
        real(msg)

    chrome._dispatch = spy  # pyright: ignore[reportPrivateUsage]
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        assert await chrome_eval(cdp, session, _post(f"'x'.repeat({5 * _MIB})")) == 200
        assert helper.site.saw("a.test", "/up")
        posts = [r for r in heard if r["method"] == "POST"]
        assert posts and all(len(json.dumps(r)) < 8192 for r in posts), "the announcement carries no body"
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_an_answer_or_an_event_too_large_to_read_ends_nothing(helper: Helper) -> None:
    """The review's second door, an agent's `Runtime.enable` and a page's 70 MiB `console.log`, and an
    answer the same size: the event goes unheard, the call fails, and both are the end of nothing."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        pid = helper.chrome.pid
        await cdp.call("Runtime.enable", session_id=session)
        assert await chrome_eval(cdp, session, f"console.log('x'.repeat({70 * _MIB})); 1") == 1
        with pytest.raises(CdpError, match="too large"):
            await chrome_eval(cdp, session, f"'x'.repeat({70 * _MIB})")
        assert await chrome_eval(cdp, session, "1 + 1") == 2
        assert helper.chrome.running and helper.chrome.pid == pid
    finally:
        await cdp.close()


_IMAGE = "new Promise(done => {{ const i = new Image(); i.onload = () => done('loaded'); i.onerror = () => done('refused'); i.src = '{url}'; }})"


@darwin_only
@needs_chrome
@pytest.mark.parametrize("path", ["/204", "/dl"])
async def test_a_navigation_that_never_commits_leaves_a_listed_page_as_it_was(
    helper: Helper, path: str
) -> None:
    """The deep review's: one click on a download link left the page unknown for good, so every
    later asset waited 2 s, was refused, and read as a list problem."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        await chrome_eval(cdp, session, f"location.href = {path!r}; 1")
        await until(lambda: helper.site.saw("a.test", path), "the navigation")
        chrome = helper.chrome
        await until(lambda: target not in chrome.navigating(), "it was given up")
        assert chrome.top_of(target) == f"http://a.test:{port}/page", "the URL it last committed"
        said = await asyncio.wait_for(
            chrome_eval(cdp, session, _IMAGE.format(url=f"http://b.test:{port}/after-{path[1:]}.png")), 1.5
        )
        assert said == "loaded" and not helper.chrome_denials(ref)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_navigation_that_never_commits_excuses_nothing_on_a_loopback_page(helper: Helper) -> None:
    """Going back to the URL it last committed gives a page no more than it had: the page being left
    here is a fenced command's, and the 204 it went to is on the list."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "204")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
        await until(lambda: f":{own}/own.html" in str(helper.chrome.tops()), "its page")
        await until(lambda: len(helper.chrome_denials(ref)) >= 2, "its assets refused")
        await chrome_eval(cdp, session, f"location.href = 'http://a.test:{port}/204'; 1")
        await until(lambda: helper.site.saw("a.test", "/204"), "the navigation")
        await until(lambda: target not in helper.chrome.navigating(), "it was given up")
        assert (
            await chrome_eval(cdp, session, _IMAGE.format(url=f"http://b.test:{port}/left.png")) == "refused"
        )
        assert not any(host == "b.test" for host, _ in helper.site.hits)
        assert not helper.app.state.egress._passed(False, "chrome", "b.test", port, False)  # pyright: ignore[reportPrivateUsage]
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_navigation_that_never_commits_excuses_no_worker(helper: Helper) -> None:
    """The top comes back to a listed page, and a worker there is still no document of it."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        await chrome_eval(cdp, session, "location.href = '/204'; 1")
        await until(lambda: helper.site.saw("a.test", "/204"), "the navigation")
        await until(lambda: target not in helper.chrome.navigating(), "it was given up")
        body = f"try {{ importScripts('http://b.test:{port}/after-204.js'); postMessage('ran'); }} catch (e) {{ postMessage('refused'); }}"
        said = await chrome_eval(
            cdp,
            session,
            "new Promise(done => { const w = new Worker(URL.createObjectURL(new Blob(["
            + json.dumps(body)
            + "]))); w.onmessage = m => done(m.data); })",
        )
        assert said == "refused" and not helper.site.saw("b.test", "/after-204.js")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_frame_made_as_chrome_goes_waits_for_it_and_gets_a_new_one(helper: Helper) -> None:
    """The last Chrome is still exiting when the pipe has gone, and one launched then is handed
    straight back to it: "exited or went silent". The helper closed this pipe itself."""
    ref = await helper.frame("ta", "a.test")
    chrome = helper.chrome
    for _ in range(3):
        last = chrome.pid
        # As the review had it: Chrome busy with a large request as its pipe goes, so slow to exit.
        page = await chrome.window_for(ref)
        chrome._fire(  # pyright: ignore[reportPrivateUsage]
            "Runtime.evaluate",
            {
                "expression": f"fetch('http://a.test:{helper.site.port}/up', {{method: 'POST', body: 'x'.repeat({20 * _MIB})}})"
            },
            chrome.fence_session(page),
        )
        await asyncio.sleep(0.15)
        chrome._gone("a test")  # pyright: ignore[reportPrivateUsage]
        await chrome.window_for(ref)  # at once: the frame door is slower than this, and the race is here
        assert chrome.running and chrome.pid != last
    assert chrome.targets_of(ref)


@darwin_only
@needs_chrome
async def test_a_same_process_iframes_navigation_leaves_nothing_behind(helper: Helper) -> None:
    """Its frame is no target, so no target's end cleaned up what its Document request wrote down:
    one entry each, each holding a URL, for the life of Chrome."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    chrome = helper.chrome
    try:
        target, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        for n in range(3):
            make = (
                "new Promise(done => { const f = document.createElement('iframe');"
                f" f.src = '/framed?{n}'; f.onload = () => done(1); document.body.append(f); }})"
            )
            assert await chrome_eval(cdp, session, make) == 1
        assert list(chrome._landed) == [target] and not chrome.navigating()  # pyright: ignore[reportPrivateUsage]
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_removed_iframes_document_goes_with_it(helper: Helper) -> None:
    """Every commit walks what a page holds, so what a page once held must not stay in it."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/page")
        chrome = helper.chrome
        churn = (
            "(async () => { for (let i = 0; i < 200; i++) { const f = document.createElement('iframe');"
            " f.src = 'data:text/html,' + i; const up = new Promise(r => f.onload = r);"
            " document.body.append(f); await up; f.remove(); } return 1; })()"
        )
        assert await chrome_eval(cdp, session, churn) == 1
        await until(lambda: len(chrome.documents_of(target)) == 1, "only the page's own document")
        kept = (
            "new Promise(done => { const f = document.createElement('iframe'); f.src = '/framed';"
            " f.onload = () => done(1); document.body.append(f); })"
        )
        assert await chrome_eval(cdp, session, kept) == 1
        await until(lambda: len(chrome.documents_of(target)) == 2, "a live iframe stays")
    finally:
        await cdp.close()
