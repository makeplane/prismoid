# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker proxy's overlay: a read-write bind of a repository gets its
`.git` read-only, and a start without the overlay is refused. Pure: no daemon. The rig is
`tests/support/rigs/docker_policy_rig.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from prismoid.device.docker_policy import (
    Verdict,
    recheck,
)
from tests.support.rigs.docker_policy_rig import create, inspected, refused
from tests.support.rigs.docker_policy_rig import root as root


@pytest.fixture
def repo(root: Path) -> Path:
    """A granted folder's own git folder, as the fence protects it."""
    git = root / ".git"
    (git / "hooks").mkdir(parents=True)
    return git


def _mounts(verdict: Verdict) -> list[dict[str, Any]]:
    assert verdict.allow, verdict.reason
    assert verdict.body is not None
    return json.loads(verdict.body)["HostConfig"].get("Mounts") or []


def _overlay(source: Path, target: str) -> dict[str, Any]:
    return {"Type": "bind", "Source": str(source), "Target": target, "ReadOnly": True}


def test_a_rw_bind_of_a_repository_gets_its_git_folder_read_only(root: Path, repo: Path) -> None:
    """A container has no fence: its rw bind would reach `.git/hooks`, so the proxy mounts the git
    folder read-only over the same path inside it."""
    verdict = create({"Binds": [f"{root}:/w"]}, root, protected=(repo,))
    assert _mounts(verdict) == [_overlay(repo, "/w/.git")]
    assert json.loads(verdict.body or b"{}")["HostConfig"]["Binds"] == [f"{root}:/w"]


def test_a_mount_bind_gets_the_overlay_too_and_keeps_the_clients(root: Path, repo: Path) -> None:
    host = {"Mounts": [{"Type": "bind", "Source": str(root), "Target": "/w/"}]}
    assert _mounts(create(host, root, protected=(repo,))) == [*host["Mounts"], _overlay(repo, "/w/.git")]


def test_keys_in_any_spelling_are_read_as_docker_reads_them(root: Path, repo: Path) -> None:
    host = {"mounts": [{"type": "bind", "source": str(root), "target": "/w"}]}
    assert _overlay(repo, "/w/.git") in _mounts(create(host, root, protected=(repo,)))


def test_a_nested_repository_lands_at_its_path_in_the_bind(root: Path) -> None:
    git = root / "data" / "lib" / ".git"
    git.mkdir(parents=True)
    assert _mounts(create({"Binds": [f"{root}:/w"]}, root, protected=(git,))) == [
        _overlay(git, "/w/data/lib/.git")
    ]
    assert _mounts(create({"Binds": [f"{root}/data:/d"]}, root, protected=(git,))) == [
        _overlay(git, "/d/lib/.git")
    ]


def test_a_worktrees_pointer_file_is_covered_the_same_way(root: Path) -> None:
    pointer = root / ".git"
    pointer.write_text("gitdir: /somewhere/.git/worktrees/w\n")
    assert _mounts(create({"Binds": [f"{root}:/w"]}, root, protected=(pointer,))) == [
        _overlay(pointer, "/w/.git")
    ]


def test_every_rw_bind_gets_its_own_overlay(root: Path, repo: Path) -> None:
    host = {"Binds": [f"{root}:/a", f"{root}:/b:rw"]}
    assert _mounts(create(host, root, protected=(repo,))) == [
        _overlay(repo, "/a/.git"),
        _overlay(repo, "/b/.git"),
    ]


def test_a_read_only_bind_or_a_bind_beside_the_repository_needs_none(root: Path, repo: Path) -> None:
    assert _mounts(create({"Binds": [f"{root}:/w:ro"]}, root, protected=(repo,))) == []
    assert _mounts(create({"Binds": [f"{root}/data:/d"]}, root, protected=(repo,))) == []
    ro = {"Mounts": [{"Type": "bind", "Source": str(root), "Target": "/w", "ReadOnly": True}]}
    assert _mounts(create(ro, root, protected=(repo,))) == ro["Mounts"]


def test_an_overlay_the_client_sent_is_not_sent_twice(root: Path, repo: Path) -> None:
    host = {"Binds": [f"{root}:/w", f"{repo}:/w/.git:ro"]}
    assert _mounts(create(host, root, protected=(repo,))) == []


def test_a_repository_gone_from_the_disk_gets_no_overlay(root: Path) -> None:
    """A bind of a missing source would fail the container: nothing is there to protect."""
    assert _mounts(create({"Binds": [f"{root}:/w"]}, root, protected=(root / ".git",))) == []


@pytest.mark.parametrize("inside", ["", "hooks"])
def test_a_rw_bind_into_a_git_folder_is_refused(root: Path, repo: Path, inside: str) -> None:
    """Nothing can be laid over it at the same place: it is mounted read-only, or not at all."""
    refused(
        create({"Binds": [f"{repo / inside}:/g".replace("/:", ":")]}, root, protected=(repo,)), "read-only"
    )
    host = {"Mounts": [{"Type": "bind", "Source": str(repo / inside), "Target": "/g"}]}
    refused(create(host, root, protected=(repo,)), "read-only")
    assert create({"Binds": [f"{repo}:/g:ro"]}, root, protected=(repo,)).allow


def _rw(source: Path, target: str = "/w") -> dict[str, Any]:
    return {"Type": "bind", "Source": str(source), "Destination": target, "RW": True}


def test_start_refuses_a_rw_bind_without_its_overlay(root: Path, repo: Path) -> None:
    """The daemon's own view: a container made before the repository was protected, or any way
    past the create's rewrite, never starts."""
    why, _ = recheck(inspected(None, _rw(root)), "web", roots=[str(root)], protected=[str(repo)])
    assert why is not None and "read-only" in why and str(root) not in why
    wrong = {**_rw(root / "data", "/w/.git"), "RW": False}  # read-only, of something else
    assert recheck(inspected(None, _rw(root), wrong), "web", roots=[str(root)], protected=[str(repo)])[0]
    writable = _rw(repo, "/w/.git")
    assert recheck(inspected(None, _rw(root), writable), "web", roots=[str(root)], protected=[str(repo)])[0]


def test_start_passes_a_rw_bind_with_its_overlay(root: Path, repo: Path) -> None:
    overlay = {**_rw(repo, "/w/.git"), "RW": False}
    assert (
        recheck(inspected(None, _rw(root), overlay), "web", roots=[str(root)], protected=[str(repo)])[0]
        is None
    )
    assert recheck(inspected(None, _rw(root)), "web", roots=[str(root)])[0] is None  # nothing protected


def test_start_refuses_a_rw_bind_into_a_git_folder(root: Path, repo: Path) -> None:
    why, _ = recheck(
        inspected(None, _rw(repo / "hooks", "/h")), "web", roots=[str(root)], protected=[str(repo)]
    )
    assert why is not None and "read-only" in why
