# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome, for real: windows per task, the credential, the profile, the relay's door, the
person's own windows, and a Chrome that goes. Sites and the helper
fixture are `tests/support/rigs/chrome_rig.py`. Tests that need Chrome skip when `settings.local.chrome_path` is not
there, and fail under `just device-proof` (`tests/support/markers.py`)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.exceptions import InvalidStatus

from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.ai.frames.image_browser.sidecar.cdp import CdpClosed, CdpError
from prismoid.device.chrome import LAUNCH_FLAGS, Chrome, ChromeMissing, missing
from prismoid.device.helper import KEY_HEADER
from prismoid.runner.device_door import create_app
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    BLOCKED,
    CHROME,
    FenceWatch,
    Helper,
    Site,
    chrome_eval,
    chrome_laid_out,
    chrome_page,
    click,
    settled,
    until,
)
from tests.support.rigs.chrome_rig import helper as helper


@darwin_only
@needs_chrome
async def test_two_tasks_two_windows_separate_lists(helper: Helper) -> None:
    port = helper.site.port
    refs = {"a.test": await helper.frame("ta", "a.test"), "b.test": await helper.frame("tb", "b.test")}
    windows: dict[str, int] = {}
    pages: dict[str, str] = {}
    dialled: dict[str, WsCdp] = {}
    try:
        for own, other in (("a.test", "b.test"), ("b.test", "a.test")):
            ref = refs[own]
            cdp = dialled[own] = await helper.dial(ref)
            target, session = await chrome_page(cdp)
            pages[own] = target
            windows[own] = (await cdp.call("Browser.getWindowForTarget", {"targetId": target}))["windowId"]

            # Its own host loads - through the egress proxy, its 407 answered over CDP, no dialog.
            nav = await cdp.call("Page.navigate", {"url": f"http://{own}:{port}/"}, session_id=session)
            assert "errorText" not in nav, nav
            await until(lambda own=own: helper.site.saw(own, "/"), f"{own} served")
            # The other task's host is refused before it leaves, and the refusal is kept, host only.
            nav = await cdp.call(
                "Page.navigate", {"url": f"http://{other}:{port}/?k=s3cret"}, session_id=session
            )
            assert nav.get("errorText") == BLOCKED
            assert not helper.site.saw(other, "/?k=s3cret")
            kept = await helper.denials(ref)
            assert {d["host"] for d in kept} == {other} and "s3cret" not in json.dumps(kept)

            # A dedicated worker the page starts is fenced as the page.
            await cdp.call("Page.navigate", {"url": f"http://{own}:{port}/w"}, session_id=session)
            await until(lambda own=own: helper.site.saw(own, "/w"), "the worker's page")
            script = (
                f"fetch('http://{other}:{port}/from-worker').catch(() => 0).then(() => postMessage('tried'))"
            )
            said = await chrome_eval(
                cdp,
                session,
                f"new Promise(done => {{ new Worker(URL.createObjectURL(new Blob([{json.dumps(script)}])))"
                ".onmessage = m => done(m.data); })",
            )
            assert said == "tried", "the worker ran and its fetch came back"
            assert not helper.site.saw(other, "/from-worker")

        assert windows["a.test"] != windows["b.test"]  # a window per task
        with pytest.raises(CdpError):  # A's relay reaches none of B's targets
            await dialled["a.test"].call(
                "Target.attachToTarget", {"targetId": pages["b.test"], "flatten": True}
            )
        with pytest.raises(CdpError):
            await dialled["a.test"].call("Browser.close")
    finally:
        for cdp in dialled.values():
            await cdp.close()


@darwin_only
@needs_chrome
async def test_a_tab_the_task_opens_is_in_its_window_and_fenced(helper: Helper) -> None:
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await helper.frame("tb", "b.test")  # B's window is the newest: Chrome's own pick for a new tab
    cdp = await helper.dial(ref)
    try:
        target, _ = await chrome_page(cdp)
        made = await cdp.call("Target.createTarget", {"url": f"http://b.test:{port}/tab"})
        assert (await cdp.call("Browser.getWindowForTarget", {"targetId": made["targetId"]}))["windowId"] == (
            await cdp.call("Browser.getWindowForTarget", {"targetId": target})
        )["windowId"]
        await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "the tab refused")
        assert not helper.site.saw("b.test", "/tab")
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_modified_and_middle_clicks_never_reach_the_other_tasks_host(helper: Helper) -> None:
    """Cmd-, Shift- and Ctrl-clicks and a middle click the agent sends are refused by the relay,
    saying why: nothing opens, nothing is rewritten."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await helper.frame("tb", "b.test")  # b.test is on the proxy's union: only the inner layer stops it
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        for tag, modifiers in (("meta", 4), ("shift", 8), ("ctrl", 2)):
            await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-{tag}"}, session_id=session)
            await until(lambda tag=tag: helper.site.saw("a.test", f"/link-{tag}"), "the link page")
            await asyncio.sleep(0.3)
            with pytest.raises(CdpError, match="modified click"):
                await click(cdp, session, modifiers=modifiers)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-middle"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-middle"), "the link page")
        with pytest.raises(CdpError):
            await click(cdp, session, button="middle")
        await asyncio.sleep(1.0)
        assert not any(host == "b.test" for host, _ in helper.site.hits)
        assert [t["type"] for t in (await cdp.call("Target.getTargets"))["targetInfos"]] == ["page"]
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_an_openerless_tab_in_the_task_window_is_fenced(helper: Helper) -> None:
    """The person's own Cmd- or middle-click in a task's window opens a tab Chrome names no opener
    for: it is fenced by the window it is in, so the other task's host is refused there too."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await helper.frame("tb", "b.test")
    cdp = await helper.dial(ref)
    try:
        target, session = await chrome_page(cdp)
        own = helper.chrome.fence_session(target)
        for tag, kw in (("cmd", {"modifiers": 4}), ("middle", {"button": "middle"})):
            await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-{tag}"}, session_id=session)
            await until(lambda tag=tag: helper.site.saw("a.test", f"/link-{tag}"), "the link page")
            await chrome_laid_out(helper.chrome, own, f"/link-{tag}")
            before = len(helper.chrome_denials(ref))
            await click(helper.chrome, own, **kw)  # pyright: ignore[reportArgumentType]
            try:
                await until(
                    lambda before=before: len(helper.chrome_denials(ref)) > before, f"{tag}'s tab refused"
                )
            finally:  # a timeout must not hide a leak: a failure says which of the two it was
                assert not helper.site.saw("b.test", f"/clicked-{tag}"), f"{tag}'s tab reached b.test"
        assert helper.chrome_denials(ref)[-1]["host"] == "b.test"
        # Fenced, never driven: the agent's list is still its own one tab.
        assert [t["targetId"] for t in (await cdp.call("Target.getTargets"))["targetInfos"]] == [target]
    finally:
        await cdp.close()


@pytest.mark.parametrize(
    ("url", "target", "alarmed"),
    [
        ("http://a.test/", "page", True),
        ("https://a.test/", "page", True),
        ("chrome-extension://x/thunk.js", "page", False),
        ("http://a.test/", "iframe", False),
    ],
)
async def test_a_pages_request_with_no_verdict_rings_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str, target: str, alarmed: bool
) -> None:
    """Every page has its verdict before its first request (the attach comes first on the pipe), so
    this path should never run. If it does it is a page's request unfenced: it says so, and the
    fence judges it, here a fence that refuses all."""
    chrome = Chrome(tmp_path, CHROME, "http://chrome:t@127.0.0.1:1")
    sent: list[str] = []
    chrome._fire = lambda method, *_: sent.append(method)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    chrome._info["f1"] = {"type": target}  # pyright: ignore[reportPrivateUsage]

    with caplog.at_level(logging.WARNING, logger="prismoid.device.chrome"):
        chrome._on_paused(None, {"requestId": "r", "frameId": "f1", "request": {"url": url}})  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)

    assert sent == ["Fetch.failRequest" if alarmed else "Fetch.continueRequest"]
    assert bool(caplog.records) is alarmed
    assert "a.test" not in caplog.text, "the line names the page, never the URL"


@pytest.mark.parametrize(
    ("origin", "answer"),
    [
        ("http://127.0.0.1:1", "ProvideCredentials"),
        ("http://127.0.0.1:2", "ProvideCredentials"),
        ("http://127.0.0.1:3", "Default"),
    ],
)
def test_the_credential_goes_to_the_two_doors_and_no_other(tmp_path: Path, origin: str, answer: str) -> None:
    chrome = Chrome(tmp_path, CHROME, "http://chrome:t@127.0.0.1:1", socket_proxy="http://127.0.0.1:2")
    said: list[dict[str, Any]] = []
    chrome._fire = lambda _method, params, *_: said.append(params)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    challenge = {"source": "Proxy", "origin": origin}
    chrome._on_auth({"requestId": "r", "authChallenge": challenge})  # pyright: ignore[reportPrivateUsage]
    assert said[0]["authChallengeResponse"]["response"] == answer


def test_the_prismoid_chrome_runs_no_extension() -> None:
    assert {"--disable-extensions", "--disable-component-extensions-with-background-pages"} <= set(
        LAUNCH_FLAGS
    )


class _AllowAll:
    async def allows(
        self, ref: str | None, url: str, kind: str = "", top: object = None, handed: object = None
    ) -> bool:
        return True

    def refused(self, ref: str | None, url: str, reason: str) -> None:
        pass

    def listed(self, ref: str, url: str) -> bool:
        return True

    def public(self, url: str) -> bool:
        return True


@darwin_only
@needs_chrome
async def test_the_disk_cache_stays_in_the_read_denied_profile() -> None:
    """On macOS a profile under ~/Library/Application Support caches into ~/Library/Caches/<same
    path> - for the default home, inside the agent's own cache root. The Prismoid Chrome's stays home."""
    name = f"PrismoidD1Test-{os.urandom(4).hex()}"
    support = Path.home() / "Library" / "Application Support" / name
    mirror = Path.home() / "Library" / "Caches" / name
    site = Site()
    server = await asyncio.start_server(site.handle, "127.0.0.1", 0)  # the site is the proxy, too
    site.port = server.sockets[0].getsockname()[1]
    chrome = Chrome(support, CHROME, f"http://chrome:t@127.0.0.1:{site.port}", fence=_AllowAll())
    try:
        await chrome.start()
        await chrome.open_tab("device-cache", "http://a.test/page")
        await until(lambda: site.saw("a.test", "http://a.test/page"), "the page served")
        await asyncio.sleep(2.0)
        assert [p for p in (support / "chrome" / "cache").rglob("*") if p.is_file()]  # it did cache
        leaked = [p for p in mirror.rglob("*") if p.is_file()] if mirror.exists() else []
        assert leaked == []
    finally:
        await chrome.close()
        server.close()
        shutil.rmtree(support, ignore_errors=True)
        shutil.rmtree(mirror, ignore_errors=True)
    assert not support.exists() and not mirror.exists()


@darwin_only
@needs_chrome
async def test_a_fenced_command_cannot_open_a_window_in_the_prismoid_chrome(helper: Helper) -> None:
    """Chrome's process singleton (`<home>/chrome/SingletonSocket`) and LaunchServices (`open`) are
    two doors to a window no task owns: from a real fenced command, both stay shut."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    profile = helper.home / "chrome"
    assert (profile / "SingletonSocket").is_symlink()  # the door being tried is really there
    pages = set(helper.chrome.pages_listed())
    attempts = {
        "singleton": f"{json.dumps(CHROME)} --user-data-dir={json.dumps(str(profile))} --no-first-run "
        f"http://b.test:{port}/via-singleton",
        "open-a": f"open -a 'Google Chrome' --args --user-data-dir={json.dumps(str(profile))} "
        f"http://b.test:{port}/via-open-a",
        "open": f"open http://b.test:{port}/via-open",
    }
    async with helper.client() as c:
        for tag, command in attempts.items():
            r = await c.post(
                f"/frames/{ref}/exec", json={"exec_id": f"probe-{tag}", "command": command, "timeout": 20}
            )
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["exit_code"] != 0, (tag, out)
    await asyncio.sleep(2.0)
    assert not [path for _, path in helper.site.hits if path.startswith("/via-")]
    now = set(helper.chrome.pages_listed())
    assert now == pages
    assert helper.chrome.running


@darwin_only
@needs_chrome
async def test_the_relay_is_only_for_a_browser_frame(helper: Helper) -> None:
    ref = await helper.plain_frame("plain")
    with pytest.raises(InvalidStatus):
        await helper.dial(ref)


@darwin_only
@needs_chrome
async def test_a_pages_websocket_to_loopback_never_leaves(helper: Helper) -> None:
    """A WebSocket handshake is never paused by `Fetch` - the inner layer cannot see it - and Chrome
    sends loopback past a proxy by default. So loopback goes to the proxy too, which refuses it: no
    task page reaches a port on this Mac (Prismoid's own included) through a socket."""
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{helper.site.port}/"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/"), "the page served")
        said = await chrome_eval(
            cdp, session, settled(f"new WebSocket('ws://127.0.0.1:{helper.site.port}/ws-loop')")
        )
        assert said == "failed", "it was tried, and came to nothing"
        assert not helper.site.saw("127.0.0.1", "/ws-loop")
    finally:
        await cdp.close()


def _pages(chrome: Chrome) -> set[str]:
    return set(chrome.pages_listed())


_SYNTHETIC = (
    "document.body.innerHTML = \"<div id=d style='position:fixed;left:0;top:0;width:200px;height:200px'>x</div>"
    "<a id=l href='http://b.test:{port}/synth-{tag}'>y</a>\";"
    "document.getElementById('d').onclick = () => document.getElementById('l').dispatchEvent("
    "new MouseEvent('click', {{{key}: true, bubbles: true, cancelable: true}})); 0"
)


@darwin_only
@needs_chrome
# On macOS Cmd is the new-tab modifier and Shift the new-window one (Ctrl is a context click). One
# per Chrome: a page opens one such tab or window per session here, so each gets a fresh helper.
@pytest.mark.parametrize(("tag", "key"), [("shift", "shiftKey"), ("meta", "metaKey")])
async def test_a_page_cannot_turn_a_click_into_an_unfenced_window(helper: Helper, tag: str, key: str) -> None:
    """A page answers the agent's plain click with a synthetic Shift-click on a link: Chrome opens
    a NEW WINDOW naming no opener. It is in no task's window, so it fails closed - the base list,
    empty - and the other task's host is never served. (A synthetic Cmd-click opens a tab in the
    task's own window: fenced as the task.)"""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    await helper.frame("tb", "b.test")  # on the proxy's union: only the inner layer stops it
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        await cdp.call("Page.navigate", {"url": f"http://a.test:{port}/link-synth"}, session_id=session)
        await until(lambda: helper.site.saw("a.test", "/link-synth"), "the page")
        await asyncio.sleep(1.0)  # served is not yet committed: the script goes in the new page
        expression = _SYNTHETIC.format(port=port, tag=tag, key=key)
        await cdp.call("Runtime.evaluate", {"expression": expression}, session_id=session)
        await asyncio.sleep(0.3)
        pages = _pages(helper.chrome)
        await click(cdp, session)  # a plain click, as the relay lets through
        try:
            await until(lambda: _pages(helper.chrome) - pages, f"{tag}: a page opens")
            await asyncio.sleep(1.5)
        finally:  # a timeout must not hide a leak: a failure says which of the two it was
            assert not helper.site.saw("b.test", f"/synth-{tag}"), f"{tag}'s window reached b.test"
            assert not any(host == "b.test" for host, _ in helper.site.hits)
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_persons_new_window_reaches_no_tasks_host(helper: Helper) -> None:
    """The person's own Cmd-N is a window no task owns: it gets the base list only, so not even a
    host on a live task's list loads there. (The cost the owner accepted: the person works in the
    task's window.)"""
    await helper.frame("ta", "a.test")
    fence = FenceWatch(helper.chrome)
    made = await helper.chrome.call(
        "Target.createTarget", {"url": f"http://a.test:{helper.site.port}/cmd-n", "newWindow": True}
    )
    await until(lambda: fence.refused("/cmd-n"), "the window's request refused")
    assert not helper.site.saw("a.test", "/cmd-n")
    assert helper.chrome.owner_of(made["targetId"]) is None


@darwin_only
@needs_chrome
async def test_a_persons_tab_in_a_task_window_behaves_as_the_tasks(helper: Helper) -> None:
    """The person's Cmd-T in the task's window (the newest - the only - window, where Chrome puts a
    plain new tab) loads the task's hosts - through the proxy, its 407 answered with no dialog - and
    is refused another task's, as the task's own tabs are. It is never handed to the agent."""
    port = helper.site.port
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        target, _ = await chrome_page(cdp)
        window = (await cdp.call("Browser.getWindowForTarget", {"targetId": target}))["windowId"]
        ok = await helper.chrome.call("Target.createTarget", {"url": f"http://a.test:{port}/cmd-t"})
        assert (await helper.chrome.call("Browser.getWindowForTarget", ok))["windowId"] == window
        await until(lambda: helper.site.saw("a.test", "/cmd-t"), "the task's host served")
        await helper.chrome.call("Target.createTarget", {"url": f"http://b.test:{port}/cmd-t"})
        await until(lambda: any(d["host"] == "b.test" for d in helper.chrome_denials(ref)), "b refused")
        assert not helper.site.saw("b.test", "/cmd-t")
        assert [t["targetId"] for t in (await cdp.call("Target.getTargets"))["targetInfos"]] == [target]
    finally:
        await cdp.close()


@darwin_only
async def test_missing_chrome_is_a_clear_error(tmp_path: Path) -> None:
    nowhere = str(tmp_path / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    why = missing(nowhere)
    assert why is not None and nowhere in why and "PRISMOID_LOCAL_CHROME_PATH" in why
    with pytest.raises(ChromeMissing) as err:
        await Chrome(tmp_path / "home", nowhere, "http://chrome:t@127.0.0.1:1").start()
    assert str(err.value) == why

    app = create_app(tmp_path / "home", device_name=lambda: "Test Mac", chrome_path=nowhere)
    key = (tmp_path / "home" / "helper.key").read_text().strip()
    plain = f"t{os.urandom(4).hex()}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as c:
        r = await c.post("/frames", json={"task_id": "t1", "kind": "machine", "profile": "browser"})
        assert r.status_code == 503 and r.json()["detail"] == why
        assert (await c.get("/frames")).json() == []  # no half-made frame is left behind
        # A frame with no browser never needs Chrome.
        made = await c.post("/frames", json={"task_id": plain, "kind": "machine"})
        assert made.status_code == 200
        await c.delete(f"/frames/{made.json()['ref']}")


@darwin_only
@needs_chrome
async def test_user_chrome_open_does_not_block(helper: Helper, tmp_path: Path) -> None:
    """A Chrome already open on its own profile - the person's everyday Chrome is just this - keeps
    running, and the Prismoid Chrome starts beside it as a separate instance on its own profile."""
    theirs = subprocess.Popen(  # noqa: ASYNC220 - a launch, not a wait
        [
            CHROME,
            f"--user-data-dir={tmp_path / 'their-profile'}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await asyncio.sleep(2.0)
        assert theirs.poll() is None
        ref = await helper.frame("ta", "a.test")
        cdp = await helper.dial(ref)
        try:
            _, session = await chrome_page(cdp)
            nav = await cdp.call(
                "Page.navigate", {"url": f"http://a.test:{helper.site.port}/"}, session_id=session
            )
            assert "errorText" not in nav
        finally:
            await cdp.close()
        assert helper.chrome.pid not in (None, theirs.pid)
        assert theirs.poll() is None
    finally:
        os.killpg(theirs.pid, signal.SIGTERM)
        theirs.wait(10)


@darwin_only
@needs_chrome
async def test_chrome_gone_closes_the_relay_and_the_next_frame_relaunches_it(helper: Helper) -> None:
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    first = helper.chrome.pid
    assert first is not None
    os.killpg(first, signal.SIGKILL)
    with pytest.raises((CdpClosed, CdpError)):
        for _ in range(100):
            await cdp.call("Target.getTargets")
            await asyncio.sleep(0.05)
    await cdp.close()

    again = await helper.dial(ref)  # the relay's window comes back with a new Chrome
    try:
        await chrome_page(again)
        assert helper.chrome.pid not in (None, first)
    finally:
        await again.close()


@darwin_only
@needs_chrome
async def test_a_malformed_url_is_refused_and_the_connection_goes_on(helper: Helper) -> None:
    ref = await helper.frame("ta", "a.test")
    cdp = await helper.dial(ref)
    try:
        _, session = await chrome_page(cdp)
        with pytest.raises(CdpError, match="http"):
            await cdp.call("Page.navigate", {"url": "http://[oops"}, session_id=session)
        assert (await cdp.call("Target.getTargets"))["targetInfos"]
    finally:
        await cdp.close()


@darwin_only
@needs_chrome
async def test_a_frame_made_again_gets_its_window_again(helper: Helper) -> None:
    """A destroyed frame's task gets no window from a dial that was on its way, until a frame is
    made for it: a Machine's ref is its task's, so a reset makes the same one."""
    ref = await helper.frame("ta", "a.test")
    async with helper.client() as c:
        assert (await c.delete(f"/frames/{ref}")).status_code == 204
    assert not helper.chrome.targets_of(ref)
    assert await helper.frame("ta", "a.test") == ref
    assert helper.chrome.targets_of(ref)


async def test_the_relay_needs_the_helper_key(helper: Helper) -> None:
    ref = await helper.plain_frame("ta")
    for key in ("wrong", ""):
        with pytest.raises(InvalidStatus) as err:
            await helper.dial(ref, key=key)
        assert err.value.response.status_code in (401, 403)
    with pytest.raises(InvalidStatus):  # no such frame: nothing to relay
        await helper.dial("device-nope")
