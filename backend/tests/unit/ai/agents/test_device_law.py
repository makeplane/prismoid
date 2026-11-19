# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device law: what an agent working on the user's own Mac must know, in the
system prompt of a frame on the device and of no other. That the cloud's prompts and tools do not move
a byte is `test_request_snapshots.py`'s: every agent's first request is a reviewed file."""

from __future__ import annotations

from prismoid.ai.agents import all as _all
from prismoid.ai.agents.base import DEVICE_LAW, instructions_for
from prismoid.ai.frames.layout import STANDARD_PATHS, FramePaths
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.schemas.ids import AgentName
from tests.support.fakes.fake_frames import FakeFrame

HOST = FramePaths(
    work="/Users/me/code/demo",
    task="/p/t",
    inputs="/p/t/inputs",
    artifacts="/p/a",
    skills="/p/s",
    verify="/p/v",
    browser="/p/c",
    out="/p/out",
    on_host=True,
)


def test_device_law_only_on_host() -> None:
    maker = _all.REGISTRY[AgentName("maker")]
    assert DEVICE_LAW not in instructions_for(maker, STANDARD_PATHS)
    assert DEVICE_LAW not in instructions_for(maker)
    assert "dev servers" not in instructions_for(maker)
    assert "dev servers" in instructions_for(maker, HOST)
    for definition in _all.REGISTRY.values():
        assert "dev servers" in instructions_for(definition, HOST)


def test_the_law_is_spelled_in_the_frames_own_paths() -> None:
    text = instructions_for(_all.REGISTRY[AgentName("maker")], HOST)
    assert "/p/out/change" in text and "/Users/me/code/demo" in text
    assert "`/out" not in text


def test_a_device_publish_offers_the_change() -> None:
    frame = FakeFrame("t1")
    frame.paths = HOST
    publish = frame_toolset(("frame.publish",), frame, None, ()).tools["publish"].description
    assert "**change**" in (publish or "")


def test_the_law_says_every_limit_the_fence_taught() -> None:
    law = DEVICE_LAW
    for words in (
        # the work
        "in place",
        "Never commit or push",
        "save_as",
        "test-output.txt",
        "patch.diff",
        "report.md",
        "`change`",
        "no git",
        # servers
        "127.0.0.1",
        "0.0.0.0",
        "https://localhost",
        "own servers",
        # git
        "git-lfs",
        "submodule update",
        "bisect",
        "worktree add",
        "`am`",
        "git init",
        "--vcs none",
        "from the Mac side",
        "git cannot change what the fence protects",
        "git add -A",
        # the plant scan
        "quarantine",
        "bare",
        # docker
        "Docker grant",
        "bind mounts",
        "privileged",
        # the browser
        "Downloads are blocked",
    ):
        assert words in law, words


def test_the_law_states_the_no_git_form_the_suite_reads() -> None:
    assert "a line that says exactly `no git`" in DEVICE_LAW


def test_the_law_names_prismoids_python_for_a_granted_folder() -> None:
    assert '"$PRISMOID_PYTHON" script.py' in DEVICE_LAW


def test_the_law_is_short_and_house_style() -> None:
    assert len(DEVICE_LAW.strip().splitlines()) <= 42
    assert "—" not in DEVICE_LAW
