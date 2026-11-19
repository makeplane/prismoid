# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome fence's lifecycle: a call Chrome never answers, a
page that goes before or after it was fenced, a pipe that is closing, and a task that is closed. The rig
is `tests/support/rigs/relay_rig.py`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from prismoid.device import chrome_pipe as chrome_module
from prismoid.device.chrome import GONE, Chrome, ChromeError
from tests.support.rigs.relay_rig import A, Agent, B, FakeBrowser, Rig, replies, until
from tests.support.rigs.relay_rig import rig as rig


async def test_a_call_chrome_never_answers_is_a_chrome_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builtin `TimeoutError` reached no caller's `except`: the agent's `createTarget` was never
    answered, and a frame's door gave a 500 past its own cleanup."""
    defaults = Chrome.call.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "timeout", 0.05)
    rig.fake.peer.holds["Target.createTarget"] = asyncio.Event()
    with pytest.raises(ChromeError, match="did not open"):
        await rig.chrome.window_for(A)
    a = Agent(rig.chrome, A)
    said = await a.ask("Target.createTarget", {"url": "about:blank"})
    assert "did not open" in said["error"]["message"]


async def test_a_page_gone_before_it_was_fenced_leaves_no_owner(rig: Rig) -> None:
    """Created, attached, detached and destroyed in one read: `_adopt` starts only after the pump
    yields, and would write down an owner for a dead target that `window_for` then hands back."""
    first = await rig.chrome.window_for(A)
    info = {"targetId": "POP", "type": "page", "url": "about:blank", "attached": True, "openerId": first}
    messages = (
        {"method": "Target.targetCreated", "params": {"targetInfo": {**info, "attached": False}}},
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "GP", "targetInfo": info, "waitingForDebugger": True},
        },
        {"method": "Target.detachedFromTarget", "params": {"sessionId": "GP", "targetId": "POP"}},
        {"method": "Target.targetDestroyed", "params": {"targetId": "POP"}},
    )
    rig.fake.peer.write_raw(b"".join(json.dumps(m).encode() + b"\0" for m in messages))
    await asyncio.sleep(0.05)
    chrome = rig.chrome
    assert chrome.owner_of("POP") is None and "POP" not in chrome.fenced_pages()
    assert "GP" not in chrome.fenced_sessions() and "GP" not in chrome._page_of  # pyright: ignore[reportPrivateUsage]
    assert [t["targetId"] for t in chrome.targets_of(A)] == [first]


async def test_a_page_let_go_before_it_was_fenced_leaves_no_session(rig: Rig) -> None:
    """Its session went and the target lives: nothing would ever clear a fence on a dead session."""
    first = await rig.chrome.window_for(A)
    info = {"targetId": "POP", "type": "page", "url": "about:blank", "attached": True, "openerId": first}
    messages = (
        {"method": "Target.targetCreated", "params": {"targetInfo": {**info, "attached": False}}},
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "GP", "targetInfo": info, "waitingForDebugger": True},
        },
        {"method": "Target.detachedFromTarget", "params": {"sessionId": "GP", "targetId": "POP"}},
    )
    rig.fake.peer.write_raw(b"".join(json.dumps(m).encode() + b"\0" for m in messages))
    await asyncio.sleep(0.05)
    assert "GP" not in rig.chrome.fenced_sessions() and rig.chrome.owner_of("POP") is None


async def test_a_page_gone_in_the_other_order_leaves_no_owner_either(rig: Rig) -> None:
    first = await rig.chrome.window_for(A)
    info = {"targetId": "POP", "type": "page", "url": "about:blank", "attached": True, "openerId": first}
    messages = (
        {"method": "Target.targetCreated", "params": {"targetInfo": {**info, "attached": False}}},
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "GP", "targetInfo": info, "waitingForDebugger": True},
        },
        {"method": "Target.targetDestroyed", "params": {"targetId": "POP"}},
    )
    rig.fake.peer.write_raw(b"".join(json.dumps(m).encode() + b"\0" for m in messages))
    await asyncio.sleep(0.05)
    assert rig.chrome.owner_of("POP") is None
    assert "GP" not in rig.chrome.fenced_sessions()


async def test_a_request_waiting_on_a_page_that_went_is_refused(rig: Rig) -> None:
    """Its verdict was popped with the target, and nothing else would ever answer it."""
    chrome = rig.chrome
    info = {"targetId": "POP", "type": "page", "url": "about:blank", "attached": True}
    verdict = asyncio.get_running_loop().create_future()
    chrome._verdicts["POP"] = verdict  # pyright: ignore[reportPrivateUsage]
    chrome._info["POP"] = info  # pyright: ignore[reportPrivateUsage]
    chrome._on_paused(  # pyright: ignore[reportPrivateUsage]
        None,
        {
            "requestId": "late",
            "frameId": "POP",
            "resourceType": "Fetch",
            "request": {"url": "https://a.test/"},
        },
    )
    await rig.fake.peer.emit("Target.targetDestroyed", {"targetId": "POP"})
    await until(lambda: replies(rig, "late"), "the request answered")
    assert replies(rig, "late")[0]["method"] == "Fetch.failRequest"


@pytest.mark.parametrize("url", ["http://[oops", "ftp://a.test/", "file:///etc/passwd", 7, None])
async def test_a_url_no_tab_can_open_is_refused_each_way_in(rig: Rig, url: object) -> None:
    page = await rig.chrome.window_for(A)
    a = Agent(rig.chrome, A)
    own = (await a.ask("Target.attachToTarget", {"targetId": page, "flatten": True}))["result"]["sessionId"]
    assert "error" in await a.ask("Page.navigate", {"url": url}, session=own)
    assert "error" in await a.ask("Target.createTarget", {"url": url})
    assert not rig.fake.sent("Page.navigate")


@pytest.mark.parametrize("session", [[], {}, 7, None])
async def test_a_detach_of_something_no_session_could_be_is_refused(rig: Rig, session: object) -> None:
    a = Agent(rig.chrome, A)
    await rig.chrome.window_for(A)
    assert (
        "not this task's"
        in (await a.ask("Target.detachFromTarget", {"sessionId": session}))["error"]["message"]
    )


async def test_a_chrome_whose_pipe_is_closing_is_not_ended_under_the_next_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`running` goes false a turn or two before the old reader sees the end of its pipe. A `start`
    in that gap launched a Chrome the old reader then ended, and never reaped the old one."""
    chrome = rig.chrome
    await chrome.window_for(A)
    old, new = 4001, 4002
    chrome.pid = old
    reaped: list[int] = []

    async def reap(pid: int) -> None:
        reaped.append(pid)

    next_chrome = FakeBrowser()
    fds = await next_chrome.peer.listen()
    monkeypatch.setattr(chrome_module, "_launch", lambda _path, _args: (new, *fds))
    monkeypatch.setattr(chrome_module, "missing", lambda _path: None)
    monkeypatch.setattr(chrome, "_reap", reap)
    a = Agent(chrome, A)
    assert chrome._writer is not None  # pyright: ignore[reportPrivateUsage]
    chrome._writer.close()  # pyright: ignore[reportPrivateUsage]
    assert not chrome.running

    await chrome.start()
    await asyncio.sleep(0.05)
    assert chrome.running and chrome.pid == new
    assert reaped == [old], "the old one, and only it"
    assert a.relay.why == GONE
    await next_chrome.peer.aclose()


async def test_start_waits_for_the_chrome_that_is_going(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Still exiting, it holds the profile, and a Chrome launched now is handed straight back to it."""
    chrome = rig.chrome
    await chrome.window_for(A)
    chrome.pid = 4001
    going, launched = asyncio.Event(), asyncio.Event()

    async def reap(_pid: int) -> None:
        await going.wait()

    next_chrome = FakeBrowser()
    fds = await next_chrome.peer.listen()

    def launch(_path: str, _args: list[str]) -> tuple[int, int, int]:
        launched.set()
        return (4002, *fds)

    monkeypatch.setattr(chrome_module, "_launch", launch)
    monkeypatch.setattr(chrome_module, "missing", lambda _path: None)
    monkeypatch.setattr(chrome, "_reap", reap)
    await rig.fake.peer.drop()
    await until(lambda: not chrome.running, "the pipe ended")
    starting = asyncio.create_task(chrome.start())
    await asyncio.sleep(0.05)
    assert not launched.is_set()
    going.set()
    await starting
    assert chrome.pid == 4002
    await next_chrome.peer.aclose()


async def test_a_closed_task_gets_no_window_until_a_frame_is_made_again(rig: Rig) -> None:
    """A dial past its frame check, as the frame goes: nothing would ever close what it made, and
    the frame's end is what closes a window."""
    await rig.chrome.window_for(A)
    await rig.chrome.close_window(A)
    made = len(rig.fake.sent("Target.createTarget"))
    with pytest.raises(ChromeError, match="closed"):
        await rig.chrome.window_for(A)
    with pytest.raises(ChromeError, match="closed"):
        await rig.chrome.open_tab(A, "about:blank")
    assert len(rig.fake.sent("Target.createTarget")) == made
    await rig.chrome.window_for(B)  # another task is no business of this
    rig.chrome.reopen(A)
    assert await rig.chrome.window_for(A)


async def test_no_chrome_starts_once_the_helper_is_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launched(*_: Any) -> None:
        raise AssertionError("Chrome was launched")

    monkeypatch.setattr(chrome_module, "_launch", launched)
    chrome = Chrome(tmp_path, "/bin/echo", "http://127.0.0.1:1")
    chrome.no_more_launches()
    with pytest.raises(ChromeError, match=GONE):
        await chrome.start()
