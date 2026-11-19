# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame
from prismoid.ai.frames.layout import STANDARD_PATHS, FramePaths, OutsideFrame, render, to_real, to_virtual
from prismoid.ai.frames.sandbar_driver import SandbarFrame


def test_virtual_render_is_identity():
    text = "write to /work then publish from /work/out.md; read /task/inputs/a.pdf"
    assert render(text, STANDARD_PATHS) is text


def test_render_rewrites_roots_longest_first():
    lay = FramePaths(
        task="/T",
        work="/W",
        artifacts="/A",
        skills="/S",
        verify="/V",
        inputs="/T/inputs",
        browser="/B",
        on_host=True,
    )
    assert render("cat /task/inputs/a && ls /task /work/x /skills/y/SKILL.md", lay) == (
        "cat /T/inputs/a && ls /T /W/x /S/y/SKILL.md"
    )
    assert render("http://h/work/x and ./work and a/work", lay) == "http://h/work/x and ./work and a/work"


def test_to_real_and_back():
    lay = FramePaths(task="/T", work="/W", inputs="/T/inputs", on_host=True)
    assert to_real("/work/a/b.md", lay) == "/W/a/b.md"
    assert to_virtual("/W/a/b.md", lay) == "/work/a/b.md"
    assert to_virtual("/T/inputs/x.pdf", lay) == "/task/inputs/x.pdf"


def test_unmapped_path_raises():
    lay = FramePaths(work="/W", on_host=True)
    with pytest.raises(OutsideFrame):
        to_virtual("/Users/me/.ssh/id", lay)


def test_cloud_drivers_are_virtual():
    assert DockerFrame.paths is STANDARD_PATHS
    assert SandbarFrame.paths is STANDARD_PATHS
