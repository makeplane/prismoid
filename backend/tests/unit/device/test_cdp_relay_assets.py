# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Prismoid Chrome fence's assets: what a listed page may load off the
list, which documents excuse a request, and the vet of an asset's host and port. The rig is
`tests/support/rigs/relay_rig.py`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from prismoid.device.cdp_relay import (
    ASSET_EXCUSED,
    ASSET_TYPES,
    BROWSER_METHODS,
    SESSION_METHODS,
    Refused,
    RequestFence,
    asset_target,
    page_on_list,
    pages_on_list,
    vet,
)
from prismoid.device.chrome import BASE
from prismoid.device.frames import FrameRegistry
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.rigs.relay_rig import (
    LOADER,
    A,
    Listed,
    Rig,
    attach_child,
    commit,
    image,
    relay_said,
    replies,
    until,
)
from tests.support.rigs.relay_rig import listed as listed
from tests.support.rigs.relay_rig import rig as rig


@pytest.mark.parametrize(
    ("referrer", "excused"),
    [
        ("https://a.test/", True),  # the page that landed: its first assets beat word of its commit
        ("http://localhost:3000/", False),  # the page being left, still running
        ("https://evil.a.test/", False),  # another origin is another page
        (None, False),  # nothing says which page it was
    ],
)
async def test_an_asset_mid_swap_waits_for_the_commit_and_must_be_the_new_pages(
    rig: Rig, referrer: str | None, excused: bool
) -> None:
    told: list[tuple[str, int]] = []
    rig.chrome._fence = RequestFence(rig.registry.frames, on_asset=lambda h, p: told.append((h, p)))  # pyright: ignore[reportPrivateUsage]
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    url = "https://a.test/next"
    document = {"requestId": "d", "frameId": page, "resourceType": "Document", "request": {"url": url}}
    await rig.fake.peer.emit("Fetch.requestPaused", document, session=session)
    await until(lambda: replies(rig, "d"), "the document's own answer")

    await image(rig, session, page, "held", referrer)
    await asyncio.sleep(0.1)
    assert not replies(rig, "held"), "no excuse while the page is unknown, and no refusal yet"

    await commit(rig, session, url)
    said = (await until(lambda: replies(rig, "held"), "the held request answered"))[0]
    assert said["method"] == ("Fetch.continueRequest" if excused else "Fetch.failRequest")
    assert bool(told) is excused


async def test_an_asset_held_for_a_commit_that_never_comes_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 204, a refused download: no commit. Past the wait the page is in no known state."""
    monkeypatch.setattr("prismoid.device.chrome_pages._COMMIT_S", 0.05)
    told: list[tuple[str, int]] = []
    rig.chrome._fence = RequestFence(rig.registry.frames, on_asset=lambda h, p: told.append((h, p)))  # pyright: ignore[reportPrivateUsage]
    page = await rig.chrome.window_for(A)
    session = rig.chrome.fence_session(page)
    document = {
        "requestId": "d",
        "frameId": page,
        "resourceType": "Document",
        "request": {"url": "https://a.test/204"},
    }
    await rig.fake.peer.emit("Fetch.requestPaused", document, session=session)
    await until(lambda: replies(rig, "d"), "the document's own answer")
    await image(rig, session, page, "held", "https://a.test/")
    said = (await until(lambda: replies(rig, "held"), "the held request answered"))[0]
    assert said["method"] == "Fetch.failRequest" and not told


async def test_a_listed_pages_own_document_is_excused(listed: Listed) -> None:
    await image(listed.rig, listed.session, listed.page, "own", None)
    assert await relay_said(listed, "own") == "Fetch.continueRequest"
    assert listed.told == [("cdn.test", 443)]


async def test_an_announcement_that_comes_after_its_pause_still_counts(listed: Listed) -> None:
    """Two channels: the order is Chrome's. The pause waits for the word of who asked."""
    params = {
        "requestId": "late",
        "frameId": listed.page,
        "networkId": "net-late",
        "resourceType": "Image",
        "request": {"url": "https://cdn.test/x.png"},
    }
    await listed.rig.fake.peer.emit("Fetch.requestPaused", params, session=listed.session)
    await asyncio.sleep(0.05)
    assert not replies(listed.rig, "late")
    announcement = {"requestId": "net-late", "frameId": listed.page, "loaderId": LOADER}
    await listed.rig.fake.peer.emit("Network.requestWillBeSent", announcement, session=listed.session)
    assert await relay_said(listed, "late") == "Fetch.continueRequest"


@pytest.mark.parametrize("kind", ["worker"])
async def test_a_workers_session_is_never_excused_whatever_it_names(listed: Listed, kind: str) -> None:
    """The session decides, and nothing a page or its server shapes. Paused on the page's session
    under the page's frame, even naming the document that is here now, a worker's asset is refused."""
    await attach_child(listed, "W1", kind)
    await image(listed.rig, listed.session, listed.page, "wk", "https://a.test/", announced_on="W1")
    assert await relay_said(listed, "wk") == "Fetch.failRequest"
    assert listed.told == []


async def test_a_document_no_longer_in_the_page_is_not_excused(listed: Listed) -> None:
    """What outlives a commit asks from the old document: the page holds a new one now, listed."""
    await commit(listed.rig, listed.session, "https://a.test/next", "L2")
    await until(
        lambda: _loaders(listed.rig, listed.page) == {listed.page: "L2"},  # pyright: ignore[reportPrivateUsage]
        "the second commit",
    )
    await image(listed.rig, listed.session, listed.page, "old", "https://a.test/", loader=LOADER)
    assert await relay_said(listed, "old") == "Fetch.failRequest"
    assert listed.told == []


async def test_an_iframe_is_excused_only_while_its_document_is_in_the_page(listed: Listed) -> None:
    rig, page, session = listed.rig, listed.page, listed.session
    await commit(rig, session, "about:srcdoc", "L-if", frame="if1", parent=page)
    await image(rig, session, page, "in", None, frame="if1", loader="L-if")
    assert await relay_said(listed, "in") == "Fetch.continueRequest"

    await commit(rig, session, "https://a.test/next", "L2")
    await until(lambda: _loaders(rig, page) == {page: "L2"}, "the top commits")  # pyright: ignore[reportPrivateUsage]
    await commit(rig, session, "about:srcdoc", "L-late", frame="if1", parent="a-frame-gone")
    await asyncio.sleep(0.05)
    assert _loaders(rig, page) == {page: "L2"}, "left mid-load: its parent is gone"  # pyright: ignore[reportPrivateUsage]


def _loaders(rig: Rig, page: str) -> dict[str, str]:
    return {frame: document.loader for frame, document in rig.chrome.documents_of(page).items()}


async def test_a_frame_held_by_a_loopback_frame_is_refused_by_its_holder(listed: Listed) -> None:
    """A listed page frames a task's server, and that frame holds a `srcdoc`, which has no origin to
    say whose it is. The asset comes from the `srcdoc`: what holds it refuses it."""
    rig, page, session = listed.rig, listed.page, listed.session
    await commit(rig, session, "http://127.0.0.1:3000/own.html", "L-own", frame="own", parent=page)
    await commit(rig, session, "about:srcdoc", "L-in", frame="inner", parent="own")
    await image(rig, session, page, "nested", None, frame="inner", loader="L-in")
    assert await relay_said(listed, "nested") == "Fetch.failRequest"
    assert listed.told == []


async def test_a_frames_new_document_ends_what_the_old_one_held(listed: Listed) -> None:
    """The holder has moved on to a listed document: the `srcdoc` it held is gone with the old one,
    down to its last descendant, and cannot ride on the new one."""
    rig, page, session = listed.rig, listed.page, listed.session
    await commit(rig, session, "http://127.0.0.1:3000/own.html", "L-own", frame="own", parent=page)
    await commit(rig, session, "about:srcdoc", "L-in", frame="inner", parent="own")
    await commit(rig, session, "about:srcdoc", "L-deep", frame="deep", parent="inner")
    await commit(rig, session, "https://a.test/listed", "L-now", frame="own", parent=page)
    await until(lambda: _loaders(rig, page).get("own") == "L-now", "the frame's new document")
    assert _loaders(rig, page) == {page: LOADER, "own": "L-now"}


async def test_an_iframe_commit_behind_its_first_asset_is_waited_for(listed: Listed) -> None:
    rig, page, session = listed.rig, listed.page, listed.session
    await image(rig, session, page, "early", None, frame="if2", loader="L-if2")
    await asyncio.sleep(0.05)
    assert not replies(rig, "early")
    await commit(rig, session, "about:srcdoc", "L-if2", frame="if2", parent=page)
    assert await relay_said(listed, "early") == "Fetch.continueRequest"


async def test_a_request_nothing_announced_is_not_excused(
    listed: Listed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("prismoid.device.chrome_pages._COMMIT_S", 0.05)
    await image(listed.rig, listed.session, listed.page, "no-id", None, loader=None, network=False)
    assert await relay_said(listed, "no-id") == "Fetch.failRequest"
    await image(listed.rig, listed.session, listed.page, "unheard", None, loader=None)
    assert await relay_said(listed, "unheard") == "Fetch.failRequest"
    assert listed.told == []


def test_allowlists_are_data_with_a_reason_each() -> None:
    for table in (SESSION_METHODS, BROWSER_METHODS):
        for method, reason in table.items():
            assert "." in method and reason.strip(), method
    for refused in (
        "Browser.close",
        "Storage.getCookies",
        "SystemInfo.getInfo",
        "Network.getAllCookies",
        "Fetch.enable",
    ):
        assert refused not in SESSION_METHODS and refused not in BROWSER_METHODS


@pytest.mark.parametrize(
    ("url", "verdict"),
    [
        ("https://a.test/x?q=secret", None),
        ("https://api.a.test/", None),
        ("wss://a.test/socket", None),
        ("data:text/plain,hi", None),
        ("blob:https://a.test/0b1", None),
        ("https://nota.test/", ("nota.test", "not in allowlist")),
        ("http://127.0.0.1:3000/", ("127.0.0.1", "loopback")),
        ("http://[::1]:3000/", ("::1", "loopback")),
        ("http://app.localhost:3000/", ("app.localhost", "loopback")),
        ("http://10.0.0.1/", ("10.0.0.1", "inside the network")),
        ("file:///etc/passwd", ("file:", "scheme")),
        ("chrome://settings/", ("chrome:", "scheme")),
    ],
)
def test_vet(url: str, verdict: tuple[str, str] | None) -> None:
    got = vet(url, {"a.test"}, own=())
    if verdict is None:
        assert got is None
    else:
        assert got is not None and got[0] == verdict[0] and verdict[1] in got[1]


@pytest.mark.parametrize(
    ("url", "allowed", "code", "excused"),
    [
        ("https://cdn.test/x.png", {"a.test"}, Refused.OFF_LIST, True),
        ("https://cdn.test/x.png", set[str](), Refused.NO_LIST, True),
        ("http://8.8.8.8/x.png", {"a.test"}, Refused.OFF_LIST, True),  # a public address is a public host
        ("http://127.0.0.1:3000/x.png", {"a.test"}, Refused.LOOPBACK, False),
        ("http://[::1]:3000/x.png", {"a.test"}, Refused.LOOPBACK, False),
        ("http://app.localhost/x.png", {"a.test"}, Refused.LOOPBACK, False),
        ("http://10.0.0.1/x.png", {"a.test"}, Refused.PRIVATE, False),
        ("http://192.168.1.1/x.png", {"a.test"}, Refused.PRIVATE, False),
        ("http://169.254.169.254/latest/meta-data", {"a.test"}, Refused.PRIVATE, False),  # metadata
        ("http://[fe80::1]/x.png", {"a.test"}, Refused.PRIVATE, False),
        ("http://100.64.0.1/x.png", {"a.test"}, Refused.PRIVATE, False),  # carrier NAT, Tailscale
        ("http://[::ffff:10.0.0.1]/x.png", {"a.test"}, Refused.PRIVATE, False),
        ("file:///etc/passwd", {"a.test"}, Refused.SCHEME, False),
        ("http://a.test:bad/", {"a.test"}, Refused.UNREADABLE, False),
        ("http:///x.png", {"a.test"}, Refused.NO_HOST, False),
    ],
)
def test_the_reason_table_says_what_an_asset_is_excused(
    url: str, allowed: set[str], code: Refused, excused: bool
) -> None:
    refusal = vet(url, allowed, own=())
    assert refusal is not None and refusal.code is code
    assert (asset_target(url, "Image", refusal) is not None) is excused


def test_only_the_lists_refusals_are_excused() -> None:
    """A reason added to `Refused` is excused by nothing until someone writes it here too."""
    assert {Refused.NO_LIST, Refused.OFF_LIST} == ASSET_EXCUSED
    assert {"Stylesheet", "Image", "Font", "Media", "Script"} == ASSET_TYPES


@pytest.mark.parametrize(
    "kind",
    [
        "Document",
        "XHR",
        "Fetch",
        "WebSocket",
        "EventSource",
        "Ping",
        "Other",
        "Manifest",
        "TextTrack",
        "Preflight",
        "CSPViolationReport",
        "SignedExchange",
        "",
    ],
)
def test_no_other_resource_type_is_an_asset(kind: str) -> None:
    refusal = vet("https://cdn.test/x", {"a.test"}, own=())
    assert refusal is not None and asset_target("https://cdn.test/x", kind, refusal) is None


@pytest.mark.parametrize("kind", sorted(ASSET_TYPES))
def test_every_asset_type_reaches_the_host_and_the_port_it_named(kind: str) -> None:
    for url, port in (
        ("https://cdn.test/x", 443),
        ("http://cdn.test/x", 80),
        ("https://cdn.test:8443/x", 8443),
    ):
        refusal = vet(url, {"a.test"}, own=())
        assert refusal is not None and asset_target(url, kind, refusal) == ("cdn.test", port)


def test_a_url_with_credentials_or_another_scheme_is_no_asset() -> None:
    for url in ("https://u:p@cdn.test/x.png", "https://u@cdn.test/x.png", "wss://cdn.test/x.png"):
        refusal = vet(url, {"a.test"}, own=())
        assert refusal is not None and asset_target(url, "Image", refusal) is None, url


LISTED = "https://a.test/page"


def _at(url: str | None) -> Callable[[], Awaitable[tuple[str, ...]]]:
    async def top() -> tuple[str, ...]:
        return () if url is None else (url,)

    return top


@pytest.mark.parametrize(
    ("top", "excused"),
    [
        (LISTED, True),
        ("https://www.a.test/page", True),
        ("http://a.test:8080/", True),
        (None, False),  # mid-swap, or never seen to commit
        ("", False),
        ("about:blank", False),
        ("about:srcdoc", False),
        ("data:text/html,x", False),
        ("blob:https://a.test/0b1", False),
        ("chrome-error://chromewebdata/", False),
        ("https://off.test/", False),
        ("http://127.0.0.1:3000/", False),
        ("http://localhost:3000/", False),
        ("http://[::1]:3000/", False),
        ("http://10.0.0.1/", False),
    ],
)
def test_only_a_page_on_its_list_excuses_an_asset(top: str | None, excused: bool) -> None:
    assert page_on_list(top, {"a.test"}) is excused


@pytest.mark.parametrize(
    ("pages", "excused"),
    [
        ((), False),
        ((LISTED,), True),
        ((LISTED, "https://www.a.test/frame"), True),  # another listed document
        ((LISTED, "about:srcdoc"), True),  # no origin of its own: it rides on its top
        ((LISTED, "about:blank"), True),
        ((LISTED, "blob:https://a.test/0b1"), True),
        ((LISTED, "http://127.0.0.1:3000/own.html"), False),  # a listed page framing a task's server
        ((LISTED, "http://localhost:3000/own.html"), False),
        ((LISTED, "https://off.test/frame"), False),
        ((LISTED, "http://127.0.0.1:3000/own.html", "about:srcdoc"), False),  # held by that frame
        ((LISTED, "http://127.0.0.1:3000/own.html", "blob:http://127.0.0.1:3000/0b1"), False),
        (("about:blank", LISTED), False),  # the top is the top: nothing below stands in for it
        (("http://127.0.0.1:3000/", LISTED), False),
    ],
)
def test_every_document_down_to_the_one_that_asked_must_pass(pages: tuple[str, ...], excused: bool) -> None:
    assert pages_on_list(pages, {"a.test"}) is excused


async def test_the_page_that_asked_decides_the_excuse(tmp_path: Path) -> None:
    """A task's own loopback server is the road out of the Seatbelt fence: a fenced command writes
    the page, the agent opens it. It stays a refusal, and a denial, even on its own port."""
    registry = FrameRegistry(tmp_path / "home")
    frame = registry.create(FrameSpecIn(task_id="a", kind=FrameKind.MACHINE, allow_domains=["a.test"]))
    told: list[tuple[str, int]] = []
    fence = RequestFence(registry.frames, on_asset=lambda host, port: told.append((host, port)))
    for top in (None, "about:blank", "http://127.0.0.1:3000/idx.html"):
        assert not await fence.allows(frame.ref, "https://cdn.test/x.png", "Image", _at(top)), top
    assert told == [] and [d["host"] for d in frame.denials] == ["cdn.test"] * 3


async def test_an_asset_is_excused_for_a_live_tasks_page_only(tmp_path: Path) -> None:
    """A service worker (`None`), a page in no task's window (`BASE`) and a dead task's page get no
    excuse. Off the list, an asset is told to the proxy and leaves no denial; refused, the opposite."""
    registry = FrameRegistry(tmp_path / "home")
    frame = registry.create(FrameSpecIn(task_id="a", kind=FrameKind.MACHINE, allow_domains=["a.test"]))
    told: list[tuple[str, int]] = []
    fence = RequestFence(registry.frames, on_asset=lambda host, port: told.append((host, port)))

    assert await fence.allows(frame.ref, "https://cdn.test/x.png", "Image", _at(LISTED))
    assert told == [("cdn.test", 443)] and not frame.denials

    told.clear()
    assert not await fence.allows(frame.ref, "https://cdn.test/x.json", "Fetch", _at(LISTED))
    assert not await fence.allows(frame.ref, "http://10.0.0.1/x.png", "Image", _at(LISTED))
    assert not await fence.allows(None, "https://cdn.test/x.png", "Image", _at(LISTED))
    assert not await fence.allows(BASE, "https://cdn.test/x.png", "Image", _at(LISTED))
    assert not await fence.allows("device-gone", "https://cdn.test/x.png", "Image", _at(LISTED))
    assert told == []
    assert [d["host"] for d in frame.denials] == ["cdn.test", "10.0.0.1"]


async def test_no_proxy_to_tell_means_no_excuse(tmp_path: Path) -> None:
    registry = FrameRegistry(tmp_path / "home")
    frame = registry.create(FrameSpecIn(task_id="a", kind=FrameKind.MACHINE, allow_domains=["a.test"]))
    assert not await RequestFence(registry.frames).allows(
        frame.ref, "https://cdn.test/x.png", "Image", _at(LISTED)
    )


def test_vet_lets_a_tasks_own_loopback_port_through() -> None:
    own = {("127.0.0.1", 3000)}
    assert vet("http://localhost:3000/", {"a.test"}, own=own) is None
    assert vet("http://127.0.0.1:3001/", {"a.test"}, own=own) is not None


def test_vet_judges_the_address_and_the_port() -> None:
    """A frame that binds `[::1]:3000` does not own `127.0.0.1:3000` (a stranger may hold it)."""
    own = {("::1", 3000)}
    assert vet("http://[::1]:3000/", {"a.test"}, own=own) is None
    assert vet("http://localhost:3000/", {"a.test"}, own=own) is None  # the proxy dials `::1` only
    assert vet("http://127.0.0.1:3000/", {"a.test"}, own=own) is not None
