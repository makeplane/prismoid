# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Browser noise: one Wikipedia read can journal 22 `frame.egress` denials - the
page's own images, eight times one host, and Chromium's background calls home (update.googleapis.com,
clients2.google.com, content-autofill.googleapis.com). The browser launches quiet, and the journal
keeps one row per denied host, saying how often, and none for Chromium's own background traffic."""

from __future__ import annotations

import sys
from typing import Any

from prismoid.ai.tools.call_rows import journal_egress_denials
from tests.support.fakes.frame_files import Journal
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.browser import CHROMIUM_FLAGS  # noqa: E402


def test_chromium_launches_without_its_background_calls_home() -> None:
    for flag in (
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--no-pings",
        "--metrics-recording-only",
    ):
        assert flag in CHROMIUM_FLAGS, flag
    features = next(f for f in CHROMIUM_FLAGS if f.startswith("--disable-features="))
    for feature in ("AutofillServerCommunication", "OptimizationHints", "MediaRouter", "Translate"):
        assert feature in features.split("=", 1)[1].split(","), feature
    assert sum(f.startswith("--disable-features=") for f in CHROMIUM_FLAGS) == 1, "Chromium keeps only one"


def _denied(host: str) -> dict[str, Any]:
    return {"ip": "172.20.0.3", "host": host, "reason": "not in allowlist ['en.wikipedia.org']"}


async def test_the_journal_keeps_one_row_per_denied_host_and_none_for_the_browsers_own_traffic() -> None:
    journal = Journal()
    denials = [
        *[_denied("thumb.wikimedia.org")] * 8,
        _denied("update.googleapis.com"),
        _denied("clients2.google.com"),
        *[_denied("content-autofill.googleapis.com")] * 5,
        _denied("pypi.org"),
    ]
    await journal_egress_denials(journal, denials)
    rows = [(e["summary"], e["denied_reason"]) for e in journal.entries]
    assert rows == [
        ("host=thumb.wikimedia.org", "not in allowlist ['en.wikipedia.org'] (8 times)"),
        ("host=pypi.org", "not in allowlist ['en.wikipedia.org']"),
    ]
