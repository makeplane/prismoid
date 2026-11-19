# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome fence's size limit: a message, a request, a
challenge, an answer or an announcement too large to read is refused and nothing else ends. The rig is
`tests/support/rigs/relay_rig.py`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from prismoid.device import chrome_pages
from prismoid.device import chrome_pipe as chrome_module
from prismoid.device.cdp_relay import (
    RequestFence,
)
from prismoid.device.chrome import TOO_LARGE, Chrome, ChromeError
from prismoid.device.frames import FrameRegistry
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.rigs.relay_rig import PROXY_PORT, TOKEN, A, FakeBrowser, Rig, commit, replies, until
from tests.support.rigs.relay_rig import rig as rig


def test_frames_cut_at_each_nul_whatever_the_chunks() -> None:
    frames = chrome_module._Frames(100)  # pyright: ignore[reportPrivateUsage]
    assert list(frames.feed(b"one\0two\0thr")) == [b"one", b"two"]
    assert list(frames.feed(b"ee")) == []
    assert list(frames.feed(b"\0\0four\0")) == [b"three", b"", b"four"]


def test_a_message_past_the_limit_comes_out_as_its_two_ends_and_the_next_is_whole() -> None:
    frames = chrome_module._Frames(100)  # pyright: ignore[reportPrivateUsage]
    assert list(frames.feed(b"a" * 100 + b"\0")) == [b"a" * 100], "at the limit it is read"
    assert list(frames.feed(b"HEAD" + b"x" * 60)) == []
    assert list(frames.feed(b"x" * 60)) == []
    for _ in range(50):
        assert list(frames.feed(b"y" * 1000)) == []
    out = list(frames.feed(b"TAIL\0next\0"))
    assert len(out) == 2 and out[1] == b"next"
    over = out[0]
    assert isinstance(over, chrome_module._TooLarge)  # pyright: ignore[reportPrivateUsage]
    assert over.head.startswith(b"HEAD") and over.tail.endswith(b"yTAIL")
    assert over.size == 4 + 120 + 50_000 + 4
    assert len(frames._buffer) == 0, "nothing of it was kept"  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
async def small_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    """A rig whose limit fits a scripted message: `PipePeer` writes on the loop's thread."""
    monkeypatch.setattr(chrome_module, "MAX_MESSAGE", 8192)
    registry = FrameRegistry(tmp_path / "home")
    registry.create(FrameSpecIn(task_id="a", kind=FrameKind.MACHINE, allow_domains=["a.test"]))
    fake = FakeBrowser()
    chrome = Chrome(
        tmp_path / "home",
        "/unused",
        f"http://chrome:{TOKEN}@127.0.0.1:{PROXY_PORT}",
        fence=RequestFence(registry.frames),
    )
    await chrome.connect(*await fake.peer.listen())
    yield Rig(fake, chrome, registry)
    await chrome.close()
    await fake.peer.aclose()


async def _as_chrome_writes(rig: Rig, msg: dict[str, Any]) -> None:
    """Compact: what `_too_large` reads is Chrome's own wire form, not `json.dumps`'s."""
    raw = json.dumps(msg, separators=(",", ":")).encode() + b"\0"
    rig.fake.peer.write_raw(raw)


def _big_pause(
    request: str, body: str, session: str | None, *, url: str = "https://a.test/up?secret=1"
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "method": "Fetch.requestPaused",
        "params": {
            "requestId": request,
            "request": {"url": url, "method": "POST", "postData": body},
            "frameId": "F",
            "resourceType": "XHR",
        },
    }
    if session is not None:
        msg["sessionId"] = session
    return msg


async def test_a_request_too_large_is_refused_where_it_paused_and_kept_host_only(small_rig: Rig) -> None:
    rig = small_rig
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    await _as_chrome_writes(rig, _big_pause("big", "x" * 20_000, session))
    said = (await until(lambda: replies(rig, "big"), "the request answered"))[0]
    assert said["method"] == "Fetch.failRequest" and said["sessionId"] == session
    assert said["params"]["errorReason"] == "BlockedByClient"
    assert rig.chrome.running
    assert list(rig.registry.frames[A].denials) == [{"ref": A, "host": "a.test", "reason": TOO_LARGE}]


async def test_the_browser_sessions_look_at_a_request_too_large_is_refused_there(small_rig: Rig) -> None:
    rig = small_rig
    await rig.chrome.window_for(A)
    await _as_chrome_writes(rig, _big_pause("big", "x" * 20_000, None))
    said = (await until(lambda: replies(rig, "big"), "the request answered"))[0]
    assert said["method"] == "Fetch.failRequest" and "sessionId" not in said
    assert not rig.registry.frames[A].denials, "the fence's own session kept it, if any"


async def test_what_a_page_put_in_a_body_names_no_session(small_rig: Rig) -> None:
    """Its quotes are escaped on the wire, so the ends of the message are Chrome's own, even a body
    written to look like them."""
    rig = small_rig
    await rig.chrome.window_for(A)
    body = "x" * 20_000 + '","sessionId":"PLANTED"}'
    await _as_chrome_writes(rig, _big_pause("big", body, None))
    said = (await until(lambda: replies(rig, "big"), "the request answered"))[0]
    assert "sessionId" not in said


async def test_a_challenge_too_large_is_cancelled(small_rig: Rig) -> None:
    rig = small_rig
    await _as_chrome_writes(
        rig,
        {
            "method": "Fetch.authRequired",
            "params": {
                "requestId": "big",
                "request": {"url": "http://a.test/", "postData": "x" * 20_000},
                "authChallenge": {"source": "Proxy", "origin": f"http://127.0.0.1:{PROXY_PORT}"},
            },
        },
    )
    said = (await until(lambda: replies(rig, "big"), "the challenge answered"))[0]
    assert said["method"] == "Fetch.continueWithAuth"
    assert said["params"]["authChallengeResponse"] == {"response": "CancelAuth"}


async def test_an_answer_too_large_fails_its_call_and_the_next_call_is_answered(small_rig: Rig) -> None:
    rig = small_rig
    await rig.chrome.window_for(A)
    held = asyncio.Event()
    rig.fake.peer.holds["Page.getNavigationHistory"] = held
    waiting = asyncio.create_task(rig.chrome.call("Page.getNavigationHistory", timeout=2.0))
    await asyncio.sleep(0.02)
    sent = rig.fake.sent("Page.getNavigationHistory")[0]["id"]
    await _as_chrome_writes(rig, {"id": sent, "result": {"data": "x" * 20_000}})
    with pytest.raises(ChromeError, match="too large"):
        await waiting
    held.set()
    assert await rig.chrome.call("Browser.getVersion") == {}


async def test_an_event_too_large_goes_unheard_and_the_loop_lives(small_rig: Rig) -> None:
    rig = small_rig
    await rig.chrome.window_for(A)
    await _as_chrome_writes(rig, {"method": "Runtime.consoleAPICalled", "params": {"args": "x" * 20_000}})
    assert await rig.chrome.call("Browser.getVersion") == {}
    assert rig.chrome.running


async def test_an_announcement_too_large_excuses_nothing(small_rig: Rig) -> None:
    """Heard by nobody, it names nobody: the request it announced has no document, and refuses."""
    rig = small_rig
    rig.chrome._fence = RequestFence(rig.registry.frames, on_asset=lambda _h, _p: None)  # pyright: ignore[reportPrivateUsage]
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    await commit(rig, session, "https://a.test/")
    await until(lambda: rig.chrome.top_of(page), "the commit")
    await _as_chrome_writes(
        rig,
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "N",
                "request": {"url": "https://cdn.test/x.png", "postData": "x" * 20_000},
            },
            "sessionId": session,
        },
    )
    await rig.fake.peer.emit(
        "Fetch.requestPaused",
        {
            "requestId": "asked",
            "frameId": page,
            "resourceType": "Image",
            "networkId": "N",
            "request": {"url": "https://cdn.test/x.png", "headers": {}},
        },
        session=session,
    )
    await asyncio.sleep(chrome_pages._COMMIT_S + 0.1)  # pyright: ignore[reportPrivateUsage]
    said = (await until(lambda: replies(rig, "asked"), "the request answered"))[0]
    assert said["method"] == "Fetch.failRequest"


async def test_news_that_never_comes_is_not_kept_for_ever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("prismoid.device.chrome_pages._COMMIT_S", 0)
    monkeypatch.setattr("prismoid.device.chrome_pages._ASKED_KEPT", 3)
    chrome = Chrome(tmp_path, "chrome", f"http://chrome:t@127.0.0.1:{PROXY_PORT}")
    for lost in range(10):
        assert not await chrome._news_of(f"request-{lost}", lambda: False)  # pyright: ignore[reportPrivateUsage]
    assert list(chrome._news) == ["request-7", "request-8", "request-9"]  # pyright: ignore[reportPrivateUsage]


_BIG = '"postData":"' + "A" * 20000 + '"'


_ID = '"requestId":"interception-job-1.0"'


_REQUEST = f'"request":{{"url":"http://a.test/",{_BIG}}}'


_OUT_OF_SIGHT = '"extra":"' + "B" * 6000 + '"'


_READ_IN_ANY_ORDER = {
    "as Chrome 156 writes it": f'{{"method":"Fetch.requestPaused","params":{{{_ID},{_REQUEST}}},"sessionId":"S1"}}',
    "params first": f'{{"params":{{{_ID},{_REQUEST}}},"method":"Fetch.requestPaused","sessionId":"S1"}}',
    "request id after the request": f'{{"method":"Fetch.requestPaused","params":{{{_REQUEST},{_ID}}},"sessionId":"S1"}}',
    "a new field first": f'{{"method":"Fetch.requestPaused","params":{{"frameId":"F",{_ID},{_REQUEST}}},"sessionId":"S1"}}',
    "session first": f'{{"sessionId":"S1","method":"Fetch.requestPaused","params":{{{_ID},{_REQUEST}}}}}',
    "session not last": f'{{"method":"Fetch.requestPaused","params":{{{_ID},{_REQUEST}}},"sessionId":"S1","extra":1}}',
    "a field after the id": f'{{"method":"Fetch.requestPaused","params":{{{_REQUEST},{_ID},"networkId":"N"}},"sessionId":"S1"}}',
}


# Each of these can only be a guess, so it stays paused and says so.
_NEVER_GUESSED = {
    "the id out of sight": f'{{"method":"Fetch.requestPaused","params":{{{_REQUEST},{_ID},{_OUT_OF_SIGHT}}},"sessionId":"S1"}}',
    "the session out of sight": f'{{"method":"Fetch.requestPaused","params":{{{_ID},{_REQUEST}}},"sessionId":"S1",{_OUT_OF_SIGHT}}}',
    "the method out of sight": f'{{{_OUT_OF_SIGHT},"method":"Fetch.requestPaused","params":{{{_ID},{_REQUEST}}},{_OUT_OF_SIGHT}}}',
    "a page's header named requestId, the real one out of sight": (
        f'{{"method":"Fetch.requestPaused","params":{{"request":{{{_BIG},"headers":{{"requestId":"evil"}}}},'
        f'"frameId":"F"}},"sessionId":"S1"}}'
    ),
}


def _too_large_to(message: str) -> tuple[list[tuple[str, str, str | None]], Chrome]:
    chrome = Chrome(Path("/nonexistent"), "/nonexistent", "http://127.0.0.1:1")
    fired: list[tuple[str, str, str | None]] = []

    def fire(method: str, params: dict[str, Any], session: str | None = None) -> None:
        fired.append((method, params["requestId"], session))

    chrome._fire = fire  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    over = next(iter(chrome_module._Frames(1000).feed(message.encode() + b"\0")))  # pyright: ignore[reportPrivateUsage]
    assert isinstance(over, chrome_module._TooLarge)  # pyright: ignore[reportPrivateUsage]
    chrome._too_large(over)  # pyright: ignore[reportPrivateUsage]
    return fired, chrome


@pytest.mark.parametrize("message", _READ_IN_ANY_ORDER.values(), ids=_READ_IN_ANY_ORDER.keys())
def test_a_request_too_large_is_refused_whatever_order_chrome_writes_it_in(
    message: str, caplog: pytest.LogCaptureFixture
) -> None:
    fired, _ = _too_large_to(message)
    assert fired == [("Fetch.failRequest", "interception-job-1.0", "S1")]
    assert "stays paused" not in caplog.text


@pytest.mark.parametrize("message", _NEVER_GUESSED.values(), ids=_NEVER_GUESSED.keys())
def test_what_cannot_be_named_stays_paused_and_says_so(
    message: str, caplog: pytest.LogCaptureFixture
) -> None:
    fired, _ = _too_large_to(message)
    assert fired == [], "never excused, and no refusal to a guess"
    assert "could not be named, and stays paused" in caplog.text


def test_a_request_on_the_browser_session_goes_to_the_browser_session() -> None:
    fired, _ = _too_large_to(f'{{"method":"Fetch.requestPaused","params":{{{_ID},{_REQUEST}}}}}')
    assert fired == [("Fetch.failRequest", "interception-job-1.0", None)]


def test_a_challenge_too_large_is_cancelled_in_any_order() -> None:
    fired, _ = _too_large_to(
        f'{{"params":{{{_REQUEST},{_ID}}},"sessionId":"S1","method":"Fetch.authRequired"}}'
    )
    assert fired == [("Fetch.continueWithAuth", "interception-job-1.0", "S1")]


def test_an_answer_too_large_is_found_with_its_id_at_either_end() -> None:
    for message in (f'{{"id":7,"result":{{{_BIG}}}}}', f'{{"result":{{{_BIG}}},"id":7}}'):
        chrome = Chrome(Path("/nonexistent"), "/nonexistent", "http://127.0.0.1:1")
        answers: list[dict[str, Any]] = []
        chrome._pending[7] = answers.append  # pyright: ignore[reportPrivateUsage]
        over = next(iter(chrome_module._Frames(1000).feed(message.encode() + b"\0")))  # pyright: ignore[reportPrivateUsage]
        assert isinstance(over, chrome_module._TooLarge)  # pyright: ignore[reportPrivateUsage]
        chrome._too_large(over)  # pyright: ignore[reportPrivateUsage]
        assert answers and "error" in answers[0]


def test_an_event_too_large_is_no_request(caplog: pytest.LogCaptureFixture) -> None:
    fired, _ = _too_large_to(
        f'{{"method":"Network.requestWillBeSent","params":{{{_REQUEST}}},"sessionId":"S1"}}'
    )
    assert fired == [] and "stays paused" not in caplog.text
