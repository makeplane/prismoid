# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker-side egress proxy keeps a monotonic version per frame."""

import pytest

from prismoid.ai.frames import egress


@pytest.fixture(autouse=True)
def _clean() -> None:  # pyright: ignore[reportUnusedFunction] - autouse: pytest calls it, not us
    egress._ALLOW.clear()  # pyright: ignore[reportPrivateUsage]
    egress._VERSIONS.clear()  # pyright: ignore[reportPrivateUsage]


def test_a_newer_version_replaces_and_a_stale_one_is_refused() -> None:
    assert egress.apply_allow("10.0.0.2", ["a.example"], 3)[0] == 200
    status, body = egress.apply_allow("10.0.0.2", ["b.example"], 2)
    assert status == 409 and body["error"] == "stale_version" and body["version"] == 3
    assert egress._ALLOW["10.0.0.2"] == {"a.example"}  # pyright: ignore[reportPrivateUsage]
    assert egress.apply_allow("10.0.0.2", ["b.example"], 4)[0] == 200
    assert egress._ALLOW["10.0.0.2"] == {"b.example"}  # pyright: ignore[reportPrivateUsage]


def test_an_unversioned_push_always_applies() -> None:
    egress.apply_allow("10.0.0.3", ["a.example"], 5)
    assert egress.apply_allow("10.0.0.3", ["c.example"], None)[0] == 200
    assert egress._ALLOW["10.0.0.3"] == {"c.example"}  # pyright: ignore[reportPrivateUsage]


def test_forgetting_a_frame_forgets_its_version() -> None:
    egress.apply_allow("10.0.0.4", ["a.example"], 9)
    egress.forget("10.0.0.4")
    assert egress.apply_allow("10.0.0.4", ["a.example"], 1)[0] == 200  # a new container on a reused IP
