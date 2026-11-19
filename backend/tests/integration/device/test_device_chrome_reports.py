# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome and what a page can say to a host: reporting, browser APIs, NEL and the back
button. Sites and the helper fixture are `tests/support/rigs/chrome_rig.py`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.ai.frames.image_browser.sidecar.cdp import CdpError
from prismoid.device import chrome_pipe as chrome_module
from prismoid.device.chrome import LAUNCH_FLAGS
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import Helper, at, chrome_eval, chrome_page, tls_context, until
from tests.support.rigs.chrome_rig import helper as helper


@dataclass
class ReportSite:
    """An https site whose page asks for CSP reports at another host, and `/nel-page` for Network
    Error Logging of every load: everything it heard."""

    port: int = 0
    hits: list[tuple[str, str]] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError, ssl.SSLError):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            method, path = head.split()[:2]
            self.hits.append((method, path))
            extra, kind, body = "", "text/css", b"body {}"
            if path == "/report-page":
                sink = f"https://sink.test:{self.port}/report-sink"
                extra = (
                    f'Reporting-Endpoints: main="{sink}"\r\n'
                    "Content-Security-Policy: img-src 'none'; report-to main\r\n"
                )
                kind, body = "text/html", b"<html><body>page</body></html>"
            elif path == "/nel-page":
                sink = f"https://sink.test:{self.port}/nel-sink"
                group = {"group": "nel", "max_age": 3600, "endpoints": [{"url": sink}]}
                nel = {"report_to": "nel", "max_age": 3600, "success_fraction": 1.0}
                extra = f"Report-To: {json.dumps(group)}\r\nNEL: {json.dumps(nel)}\r\n"
                kind, body = "text/html", b"<html><body>page</body></html>"
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nAccess-Control-Allow-Origin: *\r\n"
                f"Access-Control-Allow-Headers: *\r\n{extra}Connection: close\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        writer.close()


@pytest.fixture
async def report_site(helper: Helper, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ReportSite]:
    """The review's Reporting probe: a certificate Chrome trusts, and reports sent at once."""
    context, pin = tls_context(helper.home, "http/1.1")
    flags = (f"--ignore-certificate-errors-spki-list={pin}", "--short-reporting-delay")
    monkeypatch.setattr(chrome_module, "LAUNCH_FLAGS", (*LAUNCH_FLAGS, *flags))
    site = ReportSite()
    server = await asyncio.start_server(site.handle, "127.0.0.1", 0, ssl=context)
    site.port = server.sockets[0].getsockname()[1]
    yield site
    server.close()


def _allowing(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """This test's Chrome goes without `names` in its `--disable-features` and
    `--disable-blink-features`: a control, proving the probe beside it can see what the flag stops."""
    assert names

    def without(flag: str) -> str:
        switch, sep, listed = flag.partition("=")
        if switch not in ("--disable-features", "--disable-blink-features"):
            return flag
        return switch + sep + ",".join(name for name in listed.split(",") if name not in names)

    monkeypatch.setattr(chrome_module, "LAUNCH_FLAGS", tuple(without(f) for f in chrome_module.LAUNCH_FLAGS))


async def _a_report_falls_due(helper: Helper, report_site: ReportSite, cdp: WsCdp, session: str) -> None:
    """A listed https page, a live pass for the sink, and a CSP violation the page itself saw."""
    port = report_site.port
    await cdp.call("Page.navigate", {"url": f"https://a.test:{port}/report-page"}, session_id=session)
    await until(lambda: ("GET", "/report-page") in report_site.hits, "the page")
    await until(lambda: "/report-page" in str(helper.chrome.tops()), "its commit")
    await chrome_eval(
        cdp,
        session,
        "new Promise(done => { const l = document.createElement('link'); l.rel = 'stylesheet';"
        f" l.onload = l.onerror = done; l.href = 'https://sink.test:{port}/pass.css';"
        " document.head.append(l); }).then(() => 0)",
    )
    assert ("GET", "/pass.css") in report_site.hits, "the pass is live"
    violated = await chrome_eval(
        cdp,
        session,
        "new Promise(done => { addEventListener('securitypolicyviolation', () => done(true));"
        " new Image().src = '/blocked.png?secret=hunter2'; setTimeout(() => done(false), 3000); })",
    )
    assert violated, "there is a report to send"


_REPORT_WAIT_S = 6.0


@darwin_only
@needs_chrome
async def test_a_report_never_reaches_a_host_holding_a_pass(helper: Helper, report_site: ReportSite) -> None:
    """Reporting uploads run in the network service, so `Fetch` never pauses them and Chrome's TLS
    passes the hello gate: an asset's pass let a CSP report POST to an off-list host, secrets in the
    URL it quotes. The feature is off. The wait is only as good as the control below, which is what
    fails if Chrome ever drops `--short-reporting-delay`."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await _a_report_falls_due(helper, report_site, cdp, session)
        await asyncio.sleep(_REPORT_WAIT_S)
        assert not [hit for hit in report_site.hits if hit[1].startswith("/report-sink")]
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_with_reporting_on_the_same_report_does_reach_the_host(
    helper: Helper, report_site: ReportSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the flag is what stops it, inside the wait the test above gives it."""
    _allowing(monkeypatch, "Reporting")
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await _a_report_falls_due(helper, report_site, cdp, session)
        await until(
            lambda: [hit for hit in report_site.hits if hit[1].startswith("/report-sink")],
            "the report's upload",
            _REPORT_WAIT_S,
        )
    finally:
        await cdp.close()


_API_PROBE = (
    "[isSecureContext, typeof ServiceWorkerRegistration, typeof BackgroundFetchManager,"
    " typeof IdentityCredential, typeof PaymentRequest]"
)


async def _apis(helper: Helper, report_site: ReportSite) -> list[Any]:
    """What a secure page finds, listed in the order of `_API_PROBE`."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        url = f"https://a.test:{report_site.port}/report-page"
        await cdp.call("Page.navigate", {"url": url}, session_id=session)
        await until(lambda: "/report-page" in str(helper.chrome.tops()), "its commit")
        return await chrome_eval(cdp, session, _API_PROBE)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_page_has_no_api_the_browser_would_fetch_for_it(
    helper: Helper, report_site: ReportSite
) -> None:
    """Background Fetch, FedCM and a payment method's manifest each reached a host holding a pass
    with no `Fetch` pause. On a secure page, where they would be, they are not there."""
    said = await _apis(helper, report_site)
    assert said == [True, "function", "undefined", "undefined", "undefined"]


@darwin_only
@needs_chrome
@pytest.mark.parametrize(
    ("names", "now"),
    [
        (("BackgroundFetch",), ["function", "undefined", "undefined"]),
        (("WebPayments",), ["undefined", "undefined", "function"]),
        # Named in both flags. Each alone keeps the API away, so the pair is the smallest control: the
        # feature's own hold on a page cannot be seen while Blink's stands, nor the other way.
        (("FedCm",), ["undefined", "function", "undefined"]),
    ],
    ids=["background-fetch", "payment", "fedcm"],
)
async def test_each_flag_is_what_keeps_its_api_off_the_page(
    helper: Helper,
    report_site: ReportSite,
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...],
    now: list[str],
) -> None:
    _allowing(monkeypatch, *names)
    said = await _apis(helper, report_site)
    assert said == [True, "function", *now]


@darwin_only
@needs_chrome
@pytest.mark.parametrize("reporting_on", [False, True], ids=["as-launched", "control"])
async def test_nel_has_nothing_to_show_and_its_flag_stays(
    helper: Helper, report_site: ReportSite, monkeypatch: pytest.MonkeyPatch, reporting_on: bool
) -> None:
    """Network Error Logging's flag holds nothing a test can see, and this says why. A policy asking
    for a report of every load sends nothing here on Chrome 156, even with Reporting and NEL both on
    (the control), so no test can tell the flag from its absence. This proxy or this certificate pin
    may be the reason: that was not told apart. The flag stays, since NEL shares Reporting's uploader
    and costs nothing off. The day the control sends, it fails, and the flag can have a real test."""
    if reporting_on:
        _allowing(monkeypatch, "Reporting", "NetworkErrorLogging")
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        url = f"https://a.test:{report_site.port}/nel-page"
        for _ in range(2):  # the second load is the one a policy from the first would report
            await cdp.call("Page.navigate", {"url": url}, session_id=session)
            await until(lambda: "/nel-page" in str(helper.chrome.tops()), "its commit")
        await asyncio.sleep(_REPORT_WAIT_S)
        assert not [hit for hit in report_site.hits if hit[1].startswith("/nel-sink")]
    finally:
        await cdp.close()


async def _back_restores(helper: Helper, ref: str) -> bool:
    """A listed page, a second one, back: whether the first came back out of the cache, script state
    and all, rather than from a Document request the fence hears."""
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await at(cdp, session, helper, "a.test", "/first")
        await chrome_eval(cdp, session, "window.kept = true; 0")
        await at(cdp, session, helper, "a.test", "/second")
        await cdp.call("Runtime.evaluate", {"expression": "history.back(); 0"}, session_id=session)

        async def on_first() -> bool:
            return (
                await chrome_eval(cdp, session, "location.pathname + document.readyState") == "/firstcomplete"
            )

        for _ in range(100):
            with contextlib.suppress(CdpError):
                if await on_first():
                    return bool(await chrome_eval(cdp, session, "window.kept === true"))
            await asyncio.sleep(0.05)
        raise AssertionError("it never came back")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_the_back_button_loads_a_page_again_for_the_fence_to_see(helper: Helper) -> None:
    """The back/forward cache restores a page with no Document request, and `_on_committed` is what
    keeps a page's URL. Off, a page coming back is a load like any other."""
    assert not await _back_restores(helper, await helper.frame("ta", "a.test"))


@darwin_only
@needs_chrome
async def test_with_the_cache_on_the_back_button_restores_the_page(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the flag is what makes the test above true."""
    _allowing(monkeypatch, "BackForwardCache")
    assert await _back_restores(helper, await helper.frame("ta", "a.test"))
