# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome's per-task fence: a window per task, each task's
CDP relay reaching only its own targets, and the doors a Chrome is launched with - over a scripted Chrome
on the DevTools pipe (`tests/support/fakes/cdp_peer.PipePeer`), so no browser runs here. The real one is
`tests/integration/device/test_device_chrome.py`; the rig is `tests/support/rigs/relay_rig.py`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from prismoid.device.cdp_relay import (
    RequestFence,
)
from prismoid.device.chrome import BASE, Chrome, ChromeError, chrome_env
from prismoid.device.frames import FrameRegistry
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.markers import darwin_only
from tests.support.rigs.relay_rig import PROXY_PORT, TOKEN, A, Agent, B, Rig, commit, image, replies, until
from tests.support.rigs.relay_rig import rig as rig


async def test_relay_drops_commands_for_other_tasks_targets(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    tb = await rig.chrome.window_for(B)
    a, b = Agent(rig.chrome, A), Agent(rig.chrome, B)

    listed = (await a.ask("Target.getTargets"))["result"]["targetInfos"]
    assert [t["targetId"] for t in listed] == [ta]

    refused = await a.ask("Target.attachToTarget", {"targetId": tb, "flatten": True})
    assert refused["error"]["code"] == -32000 and "not this task's" in refused["error"]["message"]

    sb = (await b.ask("Target.attachToTarget", {"targetId": tb, "flatten": True}))["result"]["sessionId"]
    through_a = await a.ask("Runtime.evaluate", {"expression": "document.cookie"}, session=sb)
    assert "error" in through_a
    assert rig.fake.sent("Runtime.evaluate", sb) == []

    for method in ("Browser.close", "SystemInfo.getInfo", "Storage.getCookies", "Target.setDiscoverTargets"):
        params = {"discover": True} if method == "Target.setDiscoverTargets" else {}
        answer = await a.ask(method, params, session=None if method != "Storage.getCookies" else sb)
        if method == "Target.setDiscoverTargets":
            assert answer["result"] == {}  # emulated for the task: it never reaches Chrome as the agent's
        else:
            assert "error" in answer, method
    assert rig.fake.sent("Browser.close") == [] and rig.fake.sent("SystemInfo.getInfo") == []
    assert rig.fake.sent("Storage.getCookies") == []
    # Discovery shows the agent its own targets only.
    assert [e["params"]["targetInfo"]["targetId"] for e in a.events("Target.targetCreated")] == [ta]


async def test_create_target_forced_into_task_window(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    await rig.chrome.window_for(B)  # B's window is the newest: Chrome's own choice for a plain new tab
    a = Agent(rig.chrome, A)

    made = await a.ask("Target.createTarget", {"url": "https://a.test/next", "newWindow": True})
    new = made["result"]["targetId"]
    assert rig.fake.window_of[new] == rig.fake.window_of[ta]
    assert len(rig.fake.sent("Target.createTarget")) == 2  # the two windows; the tab was opened from A's page
    opened = rig.fake.sent("Runtime.evaluate", rig.fake.guard_of[ta])
    assert json.dumps("https://a.test/next") in opened[0]["params"]["expression"]
    assert rig.chrome.owner_of(new) == A

    for url in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)"):
        assert "error" in await a.ask("Target.createTarget", {"url": url}), url


async def test_popup_attached_to_opener_task(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    await rig.chrome.window_for(B)
    a, b = Agent(rig.chrome, A), Agent(rig.chrome, B)
    for agent in (a, b):
        await agent.ask("Target.setDiscoverTargets", {"discover": True})
        await agent.ask(
            "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
        )

    held = await rig.fake.announce("P1", opener=ta, url="")
    attached = await until(lambda: a.events("Target.attachedToTarget"), "the popup reaches A's agent")
    assert attached[0]["params"]["targetInfo"]["targetId"] == "P1"
    assert attached[0]["params"]["sessionId"] != held  # the agent's own session, never the fence's
    assert rig.chrome.owner_of("P1") == A

    # The popup was fenced before it ran: its first request cannot leave unvetted.
    on_held = [m["method"] for m in rig.fake.peer.received if m.get("sessionId") == held]
    assert on_held.index("Fetch.enable") < on_held.index("Runtime.runIfWaitingForDebugger")
    assert not any("P1" in json.dumps(m) for m in b.got)


async def test_person_tab_is_left_alone(rig: Rig) -> None:
    """A page in no task's window - the person's Cmd-N, or a window a page conjured from a click -
    fails closed: it runs, undriven, held to the base list only (empty), so it reaches no task's
    host. Nothing is recorded against a task, since it is none's."""
    await rig.chrome.window_for(A)
    await rig.chrome.window_for(B)
    held = await rig.fake.announce("U1", window=999)
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", held), "the page runs")
    on_held = [m["method"] for m in rig.fake.peer.received if m.get("sessionId") == held]
    assert on_held.index("Fetch.enable") < on_held.index("Runtime.runIfWaitingForDebugger")
    for n, url in enumerate(("https://a.test/", "https://b.test/", "http://127.0.0.1:8100/")):
        assert await _paused(rig, held, f"u{n}", url) == "BlockedByClient", url
    assert rig.chrome.owner_of("U1") is None
    assert list(rig.registry.frames[A].denials) == [] and list(rig.registry.frames[B].denials) == []


async def test_the_base_list_is_all_a_page_in_no_tasks_window_reaches(tmp_path: Path) -> None:
    registry = FrameRegistry(tmp_path / "home")
    registry.create(FrameSpecIn(task_id="a", kind=FrameKind.MACHINE, allow_domains=["a.test"]))
    assert not await RequestFence(registry.frames).allows(BASE, "https://a.test/")  # CHROME_BASE is empty
    fence = RequestFence(registry.frames, base={"base.test"})
    assert await fence.allows(BASE, "https://base.test/") and not await fence.allows(BASE, "https://a.test/")


async def test_an_openerless_tab_in_a_tasks_window_is_fenced_as_that_task(rig: Rig) -> None:
    """A Cmd-, Shift- or middle-click, or the person's Cmd-T, opens a tab with no opener: in a
    task's window it is held to that task's list all the same - but never handed to its agent."""
    ta = await rig.chrome.window_for(A)
    await rig.chrome.window_for(B)
    a = Agent(rig.chrome, A)
    held = await rig.fake.announce("C1", window=rig.fake.window_of[ta])
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", held), "the tab runs")
    on_held = [m["method"] for m in rig.fake.peer.received if m.get("sessionId") == held]
    assert on_held.index("Fetch.enable") < on_held.index("Runtime.runIfWaitingForDebugger")
    assert await _paused(rig, held, "c1", "https://b.test/cmdclick") == "BlockedByClient"
    assert rig.registry.frames[A].denials[-1]["host"] == "b.test"
    assert await _paused(rig, held, "c2", "https://a.test/") == "Fetch.continueRequest"
    assert rig.chrome.owner_of("C1") is None
    listed = (await a.ask("Target.getTargets"))["result"]["targetInfos"]
    assert [t["targetId"] for t in listed] == [ta]


async def test_a_tab_whose_window_cannot_be_read_fails_closed(rig: Rig) -> None:
    """A page whose window cannot be found is never let go unfenced: the base list only."""
    await rig.chrome.window_for(A)
    held = await rig.fake.announce("N1")
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", held), "the tab runs")
    assert await _paused(rig, held, "n1", "https://a.test/") == "BlockedByClient"
    assert await _paused(rig, held, "n2", "http://127.0.0.1:8100/") == "BlockedByClient"


async def test_clicks_and_enter_never_open_a_tab(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    a = Agent(rig.chrome, A)
    sa = (await a.ask("Target.attachToTarget", {"targetId": ta, "flatten": True}))["result"]["sessionId"]
    for modifiers in (2, 4, 8, 1):  # Ctrl, Cmd, Shift, Alt
        press = {
            "type": "mousePressed",
            "x": 5,
            "y": 5,
            "button": "left",
            "clickCount": 1,
            "modifiers": modifiers,
        }
        refused = await a.ask("Input.dispatchMouseEvent", press, session=sa)
        assert "modified click" in refused["error"]["message"]
    middle = {"type": "mousePressed", "x": 5, "y": 5, "button": "middle", "clickCount": 1}
    assert "middle click" in (await a.ask("Input.dispatchMouseEvent", middle, session=sa))["error"]["message"]
    assert rig.fake.sent("Input.dispatchMouseEvent", sa) == []  # refused, never rewritten

    plain = {"type": "mousePressed", "x": 5, "y": 5, "button": "left", "clickCount": 1, "modifiers": 0}
    assert "result" in await a.ask("Input.dispatchMouseEvent", plain, session=sa)
    wheel = {"type": "mouseWheel", "x": 5, "y": 5, "deltaX": 0, "deltaY": 100, "modifiers": 8}
    assert "result" in await a.ask("Input.dispatchMouseEvent", wheel, session=sa)

    shift_enter = {"type": "keyDown", "key": "Enter", "modifiers": 8}  # rewritten, it would still submit
    assert (
        "modified Enter"
        in (await a.ask("Input.dispatchKeyEvent", shift_enter, session=sa))["error"]["message"]
    )
    await a.ask("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter"}, session=sa)
    await a.ask("Input.dispatchKeyEvent", {"type": "keyDown", "key": "A", "modifiers": 8}, session=sa)
    keys = rig.fake.sent("Input.dispatchKeyEvent", sa)
    assert [(m["params"]["key"], m["params"].get("modifiers")) for m in keys] == [("Enter", None), ("A", 8)]


async def test_the_proxy_credential_loop_guard_is_bounded(rig: Rig) -> None:
    for n in range(300):
        await rig.fake.peer.emit(
            "Fetch.authRequired",
            {
                "requestId": f"q{n}",
                "authChallenge": {"source": "Proxy", "origin": f"http://127.0.0.1:{PROXY_PORT}"},
            },
        )
    await until(lambda: len(rig.fake.sent("Fetch.continueWithAuth")) == 300, "every challenge answered")
    assert len(rig.chrome._auth_tried) <= 256  # pyright: ignore[reportPrivateUsage]


async def test_a_closed_windows_tabs_reach_nothing(rig: Rig) -> None:
    ga = rig.fake.guard_of[await rig.chrome.window_for(A)]
    await rig.chrome.close_window(A)
    assert await _paused(rig, ga, "late", "https://a.test/") == "BlockedByClient"
    assert rig.chrome._task_locks.get(A) is not None  # pyright: ignore[reportPrivateUsage]


def test_chrome_gets_none_of_the_helpers_secrets() -> None:
    env = chrome_env(
        {
            "HOME": "/Users/me",
            "PATH": "/usr/bin",
            "LANG": "en_GB.UTF-8",
            "LC_ALL": "en_GB.UTF-8",
            "PRISMOID_WORKER_DATABASE_URL": "postgresql://prismoid:pw@db/prismoid",
            "PRISMOID_WORKER_ANTHROPIC_API_KEY": "sk-secret",
            "OPENAI_API_KEY": "sk-other",
        }
    )
    assert env == {"HOME": "/Users/me", "PATH": "/usr/bin", "LANG": "en_GB.UTF-8", "LC_ALL": "en_GB.UTF-8"}


@darwin_only  # posix_spawn's setsid is macOS-only in CPython, as the device is
async def test_chrome_is_launched_with_the_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in Chrome that writes down what it was given, then exits."""
    seen = tmp_path / "env.txt"
    stand_in = tmp_path / "Google Chrome"
    stand_in.write_text(f"#!/bin/sh\nenv > {seen}\nexit 1\n")
    stand_in.chmod(0o755)
    monkeypatch.setenv("PRISMOID_WORKER_DATABASE_URL", "postgresql://prismoid:s3cret@db/prismoid")
    monkeypatch.setenv("PRISMOID_WORKER_ANTHROPIC_API_KEY", "sk-s3cret")
    with pytest.raises(ChromeError):
        await Chrome(tmp_path / "home", str(stand_in), f"http://chrome:t@127.0.0.1:{PROXY_PORT}").start()
    given = dict(line.split("=", 1) for line in seen.read_text().splitlines() if "=" in line)
    assert "s3cret" not in seen.read_text()
    assert given["HOME"] == os.environ["HOME"] and "PATH" in given
    assert set(given) - {"PWD", "SHLVL", "_", "OLDPWD"} <= {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "LANG",
    } | {k for k in given if k.startswith("LC_")}


async def _paused(rig: Rig, session: str, request_id: str, url: str) -> str:
    await rig.fake.peer.emit(
        "Fetch.requestPaused", {"requestId": request_id, "request": {"url": url}}, session=session
    )

    def answered() -> str | None:
        for m in rig.fake.peer.received:
            if m.get("params", {}).get("requestId") == request_id and m.get("sessionId") == session:
                return m["method"] if m["method"] == "Fetch.continueRequest" else m["params"]["errorReason"]
        return None

    return await until(answered, f"{url} answered")


async def _paused_browser_wide(rig: Rig, frame: str, request_id: str, url: str) -> str:
    """A request as the browser-wide session sees it: named by its frame, on no session."""
    await rig.fake.peer.emit(
        "Fetch.requestPaused", {"requestId": request_id, "frameId": frame, "request": {"url": url}}
    )

    def answered() -> str | None:
        for m in rig.fake.peer.received:
            if m.get("params", {}).get("requestId") == request_id and "sessionId" not in m:
                return m["method"] if m["method"] == "Fetch.continueRequest" else m["params"]["errorReason"]
        return None

    return await until(answered, f"{url} answered browser-wide")


async def test_a_tabs_first_navigation_is_vetted_browser_wide(rig: Rig) -> None:
    """A Cmd-click tab's navigation starts before its own session's `Fetch` is on: the browser-wide
    session vets it by its frame, waiting for the page's verdict. A frame of no page gets no pass
    from that: it is held to the union of the lists, as a worker is."""
    ta = await rig.chrome.window_for(A)
    await rig.fake.announce("C1", window=rig.fake.window_of[ta])
    assert await _paused_browser_wide(rig, "C1", "b1", "https://b.test/clicked") == "BlockedByClient"
    assert rig.registry.frames[A].denials[-1]["host"] == "b.test"
    assert await _paused_browser_wide(rig, "C1", "b2", "https://a.test/") == "Fetch.continueRequest"
    await rig.fake.announce("U1", window=999)  # a page in no task's window: the base list
    assert await _paused_browser_wide(rig, "U1", "b3", "https://a.test/") == "BlockedByClient"
    assert await _paused_browser_wide(rig, "no-such-frame", "b4", "https://x.test/") == "BlockedByClient"


async def test_fetch_paused_request_vetted_per_task(rig: Rig) -> None:
    ga = rig.fake.guard_of[await rig.chrome.window_for(A)]
    gb = rig.fake.guard_of[await rig.chrome.window_for(B)]
    assert rig.fake.sent("Fetch.enable", ga)[0]["params"]["patterns"] == [{"urlPattern": "*"}]

    assert await _paused(rig, ga, "r1", "https://a.test/page?x=1") == "Fetch.continueRequest"
    assert await _paused(rig, ga, "r2", "https://www.a.test/img.png") == "Fetch.continueRequest"
    assert await _paused(rig, ga, "r3", "https://b.test/") == "BlockedByClient"
    assert await _paused(rig, gb, "r4", "https://b.test/") == "Fetch.continueRequest"
    assert await _paused(rig, gb, "r5", "https://a.test/") == "BlockedByClient"
    # Loopback is only for the ports a task's own commands opened: none yet.
    assert await _paused(rig, ga, "r6", "http://127.0.0.1:8100/v1/tasks") == "BlockedByClient"
    assert await _paused(rig, ga, "r7", "http://localhost:3000/") == "BlockedByClient"


async def test_refusal_recorded_in_denials(rig: Rig) -> None:
    ga = rig.fake.guard_of[await rig.chrome.window_for(A)]
    await rig.chrome.window_for(B)
    assert await _paused(rig, ga, "r1", "https://b.test/login?token=s3cret") == "BlockedByClient"
    denials = list(rig.registry.frames[A].denials)
    assert len(denials) == 1
    assert denials[0]["ref"] == A and denials[0]["host"] == "b.test"
    assert "not in allowlist" in denials[0]["reason"]
    assert "s3cret" not in json.dumps(denials) and "/login" not in json.dumps(denials)
    assert list(rig.registry.frames[B].denials) == []


async def test_a_frames_widened_list_applies_at_the_next_request(rig: Rig) -> None:
    ga = rig.fake.guard_of[await rig.chrome.window_for(A)]
    assert await _paused(rig, ga, "r1", "https://c.test/") == "BlockedByClient"
    rig.registry.frames[A].allow.add("c.test")
    assert await _paused(rig, ga, "r2", "https://c.test/") == "Fetch.continueRequest"


async def test_iframes_and_workers_of_a_task_page_are_fenced_as_the_task(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    ga = rig.fake.guard_of[ta]
    auto = rig.fake.sent("Target.setAutoAttach", ga)[0]["params"]
    assert auto["waitForDebuggerOnStart"] and auto["flatten"]
    child = "G-child"
    await rig.fake.peer.emit(
        "Target.attachedToTarget",
        {
            "sessionId": child,
            "targetInfo": {"targetId": "W1", "type": "worker", "url": "", "attached": True},
            "waitingForDebugger": True,
        },
        session=ga,
    )
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", child), "the worker runs")
    on_child = [m["method"] for m in rig.fake.peer.received if m.get("sessionId") == child]
    assert on_child.index("Fetch.enable") < on_child.index("Runtime.runIfWaitingForDebugger")
    assert await _paused(rig, child, "w1", "https://b.test/beacon") == "BlockedByClient"
    assert rig.registry.frames[A].denials[-1]["host"] == "b.test"


async def test_service_worker_held_to_the_outer_list(rig: Rig) -> None:
    """A service worker belongs to no one task: it is held to what the proxy allows Chrome - the
    union of the live tasks' lists - and never to loopback."""
    await rig.chrome.window_for(A)
    sw = await rig.fake.announce("SW1", kind="service_worker", url="https://a.test/sw.js")
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", sw), "the worker runs")
    assert rig.fake.sent("Fetch.enable", sw)
    assert await _paused(rig, sw, "s1", "https://b.test/") == "Fetch.continueRequest"
    assert await _paused(rig, sw, "s2", "https://c.test/") == "BlockedByClient"
    assert await _paused(rig, sw, "s3", "http://127.0.0.1:8100/") == "BlockedByClient"


@pytest.mark.parametrize("kind", ["service_worker", "shared_worker"])
@pytest.mark.parametrize("url", ["blob:https://a.test/0b1", "data:text/javascript,0", "https://a.test/w.js"])
async def test_every_web_worker_is_fenced_whatever_its_scheme(rig: Rig, kind: str, url: str) -> None:
    sw = await rig.fake.announce("W1", kind=kind, url=url)
    await until(lambda: rig.fake.sent("Runtime.runIfWaitingForDebugger", sw), "it runs")
    assert rig.fake.sent("Fetch.enable", sw)
    assert not rig.fake.sent("Target.detachFromTarget")
    assert await _paused(rig, sw, "w", "https://off.test/") == "BlockedByClient"


@pytest.mark.parametrize("kind", ["service_worker", "shared_worker"])
async def test_a_workers_request_that_outran_its_session_is_judged_browser_wide(rig: Rig, kind: str) -> None:
    """Seen on Chrome 156 under load: only the browser-wide session pauses it, under the worker's own
    target id. Let go, it meets only the proxy, and a live pass opens that."""
    await rig.chrome.window_for(A)
    await rig.fake.announce("W2", kind=kind, url="blob:https://a.test/0b1")
    assert await _paused_browser_wide(rig, "W2", "o1", "https://off.test/") == "BlockedByClient"
    assert await _paused_browser_wide(rig, "W2", "o2", "https://a.test/") == "Fetch.continueRequest"


async def test_proxy_challenge_answered_as_chrome_and_a_sites_own_left_to_the_person(rig: Rig) -> None:
    async def challenge(request_id: str, source: str, origin: str) -> dict[str, Any]:
        await rig.fake.peer.emit(
            "Fetch.authRequired",
            {
                "requestId": request_id,
                "request": {"url": "https://a.test/"},
                "authChallenge": {"source": source, "origin": origin},
            },
        )
        n = len(
            [m for m in rig.fake.sent("Fetch.continueWithAuth") if m["params"]["requestId"] == request_id]
        )
        answers = await until(
            lambda: [
                m for m in rig.fake.sent("Fetch.continueWithAuth") if m["params"]["requestId"] == request_id
            ][n:],
            "the challenge answered",
        )
        return answers[0]["params"]["authChallengeResponse"]

    ours = f"http://127.0.0.1:{PROXY_PORT}"
    assert await challenge("q1", "Proxy", ours) == {
        "response": "ProvideCredentials",
        "username": "chrome",
        "password": TOKEN,
    }
    assert (await challenge("q1", "Proxy", ours))["response"] == "CancelAuth"  # a refused credential: no loop
    assert await challenge("q2", "Server", "https://a.test") == {"response": "Default"}
    assert await challenge("q3", "Proxy", "http://127.0.0.1:9") == {"response": "Default"}
    browser_fetch = rig.fake.sent("Fetch.enable")[0]
    assert "sessionId" not in browser_fetch and browser_fetch["params"]["handleAuthRequests"] is True


async def test_navigation_to_a_local_scheme_is_refused(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    a = Agent(rig.chrome, A)
    sa = (await a.ask("Target.attachToTarget", {"targetId": ta, "flatten": True}))["result"]["sessionId"]
    assert "error" in await a.ask("Page.navigate", {"url": "file:///Users/me/.ssh/id_ed25519"}, session=sa)
    assert rig.fake.sent("Page.navigate") == []
    assert "result" in await a.ask("Page.navigate", {"url": "https://a.test/"}, session=sa)


async def test_closing_a_window_ends_its_relays(rig: Rig) -> None:
    ta = await rig.chrome.window_for(A)
    await rig.chrome.window_for(B)
    a, b = Agent(rig.chrome, A), Agent(rig.chrome, B)
    await rig.chrome.close_window(A)
    assert [m["params"]["targetId"] for m in rig.fake.sent("Target.closeTarget")] == [ta]
    assert a.relay.lost.is_set() and not b.relay.lost.is_set()
    assert rig.chrome.owner_of(ta) is None


async def test_a_fence_that_raises_refuses_the_request(rig: Rig) -> None:
    """Fail closed. The proxy cannot be told, in the real path from pause to answer: the excuse would
    have held, so only `_decide`'s own refusal stands between this and a hung or a passed request."""
    told: list[tuple[str, int]] = []

    def broken(host: str, port: int) -> None:
        told.append((host, port))
        raise RuntimeError("no proxy")

    rig.chrome._fence = RequestFence(rig.registry.frames, on_asset=broken)  # pyright: ignore[reportPrivateUsage]
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    await commit(rig, session, "https://a.test/")
    await until(lambda: rig.chrome.top_of(page), "the commit")
    await image(rig, session, page, "raises", None)
    said = (await until(lambda: replies(rig, "raises"), "the request answered"))[0]
    assert told == [("cdn.test", 443)], "it reached the excuse, and that raised"
    assert said["method"] == "Fetch.failRequest" and said["params"]["errorReason"] == "BlockedByClient"
