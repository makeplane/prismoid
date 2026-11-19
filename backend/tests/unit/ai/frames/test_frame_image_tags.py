# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Three image profiles in a line, three tags, and a tag moves when its context or any image below it moves."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from prismoid.ai.frames import docker_driver as dd
from prismoid.ai.frames import images, layout


def test_browser_layout_paths_exist_and_are_outside_the_snapshot_set():
    assert layout.BROWSER == "/browser"
    assert layout.SIDECAR_SOCK.startswith("/run/browser/")
    assert layout.BROWSER not in layout.ALL  # the plain snapshot never carries cookies
    assert dd.BROWSER_UID == 1001 and dd.AGENT_UID == 1000


def test_each_profile_has_its_own_tag_layered_on_the_one_below() -> None:
    assert images.TAGS["base"].startswith("prismoid-frame:")
    assert images.TAGS["code"].startswith("prismoid-frame-code:")
    assert images.TAGS["browser"].startswith("prismoid-frame-browser:")
    assert len(set(images.TAGS.values())) == 3
    assert [parent for _root, parent in images.RECIPES.values()] == [None, "base", "code"]
    for profile, (root, parent) in images.RECIPES.items():
        if parent is not None:
            assert "FROM ${BASE_IMAGE}" in (root / "Dockerfile").read_text(), profile


def test_a_base_change_moves_every_tag_above_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    before = {p: images.image_digest(p) for p in images.RECIPES}
    monkeypatch.setitem(images.RECIPES, "base", (tmp_path, None))
    after = {p: images.image_digest(p) for p in images.RECIPES}
    assert all(before[p] != after[p] for p in images.RECIPES), (before, after)


def _installs(profile: str) -> str:
    install = re.search(r"apt-get install[^&]*", (images.RECIPES[profile][0] / "Dockerfile").read_text())
    assert install is not None
    return install.group()


def test_chromium_is_pinned_to_one_exact_build():
    """The tag hashes the build context, so only an exact pin makes "same tag" mean "same
    Chromium" and a bump mean a new image on every worker. An unpinned install froze
    whatever build each worker first resolved, and no security update ever reached it."""
    pins = re.findall(r"\bchromium(=\S+)?", _installs("browser"))
    assert pins and all(re.fullmatch(r"=\d+(\.\d+)+-\S+", pin) for pin in pins), pins


def test_the_code_tools_are_pinned_to_exact_builds() -> None:
    """Every package the code image installs names its exact build, and node comes from one
    version of the official image, by digest: no floating tag ever decides what a step runs."""
    packages = re.findall(
        r"\s([a-z][a-z0-9.+-]*)(=\S+)?", _installs("code").split("--no-install-recommends", 1)[1]
    )
    assert {name for name, _ in packages} >= {"git", "ripgrep"}
    assert all(pin for _, pin in packages), packages
    dockerfile = (images.RECIPES["code"][0] / "Dockerfile").read_text()
    assert re.search(r"^FROM node:\d+\.\d+\.\d+-bookworm-slim@sha256:[0-9a-f]{64} AS node$", dockerfile, re.M)
