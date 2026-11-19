# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`BrowserVerbs` on its own: the verbs over any CDP connection, with no
Chromium launched and no `Browser` around them - what a worker drives a remote computer's browser
with (ADR-063)."""

from __future__ import annotations

import sys

from tests.support.fakes.cdp_scripts import AX_BUTTON, FakeCdp, ref_for
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.browser import Browser  # noqa: E402
from sidecar.cdp import CdpConnection, PipeTransport  # noqa: E402
from sidecar.verbs import BrowserVerbs  # noqa: E402

# The daemon's pipe and a scripted connection both fit what the verbs speak: pyright checks this
# assignment, so a connection that drifts from `CdpConnection` fails the type check, not a run.
_FITS: list[CdpConnection] = [PipeTransport(), FakeCdp()]


async def test_attach_takes_the_first_page_and_sets_it_up_before_watching_for_tabs() -> None:
    cdp = FakeCdp()
    cdp.answer(
        "Target.getTargets", {"targetInfos": [{"targetId": "T1", "type": "page", "url": "about:blank"}]}
    )
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F1"}}})
    set_up: list[tuple[str, int]] = []

    async def on_tab(session: str) -> None:
        set_up.append((session, len(cdp.calls)))

    verbs = BrowserVerbs(cdp, on_tab=on_tab)
    await verbs.attach()
    methods = [m for m, _ in cdp.calls]
    assert verbs.session == "S1" and [s for s, _ in set_up] == ["S1"]
    at = set_up[0][1]  # how many calls had gone out when the tab was set up
    assert "Runtime.enable" in methods[:at] and "Page.getFrameTree" in methods[at:]
    assert methods.index("Target.setDiscoverTargets") < methods.index("Target.setAutoAttach")


async def test_verbs_act_over_any_connection_with_no_browser_launched() -> None:
    cdp = FakeCdp()
    verbs = BrowserVerbs(cdp)
    verbs._session, verbs._main_frame_id = "S1", "F"  # pyright: ignore[reportPrivateUsage]
    cdp.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON})
    snap = await verbs.handle({"verb": "snapshot"})
    clicked = await verbs.handle({"verb": "click", "ref": ref_for(snap, 'button "Save changes"')})
    assert clicked["ok"] and clicked["_record"]["jpeg"], "the capture rides back for the recording"
    assert set(cdp.sent_on("Input.dispatchMouseEvent")) == {"S1"}


async def test_a_new_tab_is_set_up_by_the_verbs_owner_before_it_may_run() -> None:
    """The order is the property: the owner's set-up (proxy-auth Fetch) is in place before the
    paused tab is let run, or its first request goes out unauthenticated."""
    cdp = FakeCdp()
    set_up: list[tuple[str, int]] = []

    async def on_tab(session: str) -> None:
        set_up.append((session, len(cdp.calls)))

    verbs = BrowserVerbs(cdp, on_tab=on_tab)
    verbs._target, verbs._session = "T1", "S1"  # pyright: ignore[reportPrivateUsage]
    verbs._tabs, verbs._order = {"T1": "S1"}, ["T1"]  # pyright: ignore[reportPrivateUsage]
    info = {"targetId": "T2", "type": "page", "openerId": "T1", "url": "about:blank"}
    cdp.emit("Target.attachedToTarget", {"sessionId": "S2", "targetInfo": info, "waitingForDebugger": True})
    await cdp.drain()
    assert [s for s, _ in set_up] == ["S2"] and cdp.sent_on("Runtime.runIfWaitingForDebugger") == ["S2"]
    at = set_up[0][1]  # how many calls had gone out when the tab was set up
    methods = [m for m, _ in cdp.calls]
    assert "Runtime.runIfWaitingForDebugger" in methods[at:], "set up before it may run"


async def test_a_restart_keeps_counting_epochs_so_an_old_reference_never_resolves() -> None:
    """The split must not reset the epoch. Each Chromium start is a new
    `BrowserVerbs`, and its snapshots continue the epoch, so a reference from before the restart is
    stale after it - never the new page's button. A guard, not a fix: `start()` never reset the
    epoch before the split either; this fails before it only because `connect` did not exist."""
    b = Browser(profile_dir="/nonexistent", proxy=None)
    b.alive = True
    first = FakeCdp()
    first.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON})
    b.connect(first)._session = "S1"  # pyright: ignore[reportPrivateUsage]
    old = ref_for(await b.handle({"verb": "snapshot"}), 'button "Save changes"')
    second = FakeCdp()
    second.answer("Accessibility.getFullAXTree", {"nodes": AX_BUTTON})
    b.connect(second)._session = "S1"  # pyright: ignore[reportPrivateUsage]
    new = ref_for(await b.handle({"verb": "snapshot"}), 'button "Save changes"')
    assert old.startswith("e1.") and new.startswith("e2.")
    refused = await b.handle({"verb": "click", "ref": old})
    assert refused == {"ok": False, "error": f"stale reference {old}; take a new snapshot"}
