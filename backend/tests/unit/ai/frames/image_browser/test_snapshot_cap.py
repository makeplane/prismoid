# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The proof check's read is the whole page (a 12k cap can cut a Wikipedia
quote). `snapshot` takes a `cap` from the system - never from the model, whose tool builds its own
command - bounded so no read is unbounded."""

from __future__ import annotations

import sys
from typing import Any

from tests.support.fakes.cdp_scripts import FakeCdp
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.tree import render  # noqa: E402
from sidecar.verbs import BrowserVerbs  # noqa: E402


def _long_page(n: int) -> list[dict[str, Any]]:
    kids = [str(i) for i in range(2, n + 2)]
    root = {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": ""}, "childIds": kids}
    texts = [
        {
            "nodeId": k,
            "parentId": "1",
            "role": {"value": "StaticText"},
            "name": {"value": f"line {k} " + "x" * 80},
        }
        for k in kids
    ]
    return [root, *texts]


async def _read(command: dict[str, Any], lines: int = 400) -> dict[str, Any]:
    cdp = FakeCdp()
    verbs = BrowserVerbs(cdp, snapshot_chars=2_000)
    cdp.answer(
        "Target.getTargets", {"targetInfos": [{"targetId": "T1", "type": "page", "url": "about:blank"}]}
    )
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F"}}})
    await verbs.attach()
    cdp.answer("Accessibility.getFullAXTree", {"nodes": _long_page(lines)})
    return await verbs.handle(command)


async def _snapshot(command: dict[str, Any]) -> str:
    return str((await _read(command))["text"])


async def test_a_system_cap_reads_past_the_models_window() -> None:
    assert "line 401" not in await _snapshot({"verb": "snapshot"})
    assert "line 401" in (await _read({"verb": "snapshot", "cap": 100_000}))["reading"]


async def test_a_cap_out_of_bounds_falls_back_to_the_window() -> None:
    for cap in (0, -1, "lots", 400_001):
        assert "line 401" not in await _snapshot({"verb": "snapshot", "cap": cap})


def test_the_system_read_types_the_page_apart_from_the_renderer() -> None:
    """The proof reads the page's own lines (`written`) and words (`reading`), never the renderer's
    truncation marker, which the tree's text carries and no page said."""
    snap = render(_long_page(400), epoch=1, cap=3_000, password_backend_ids=set())
    assert "truncated" in snap.text
    assert "truncated" not in snap.written and "truncated" not in snap.reading
    assert snap.written.startswith('StaticText "line 2 ') and snap.reading.startswith("line 2 x")


async def test_a_huge_page_s_proof_read_stays_within_the_one_cap() -> None:
    """`text`, `written` and `reading` share the system's one cap, the tree's text trimmed first."""
    read = await _read({"verb": "snapshot", "cap": 100_000}, lines=4_000)
    assert sum(len(read[k]) for k in ("text", "written", "reading")) <= 100_000
    assert read["reading"].startswith("line 2 x") and read["written"].startswith('StaticText "line 2 ')
    assert len(read["text"]) < len(read["written"]), "the text gives way first"
    assert "reading" not in await _read({"verb": "snapshot"}), "the model's look carries no proof read"
