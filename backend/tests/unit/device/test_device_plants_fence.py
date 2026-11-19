# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fence list and the move: nested repositories join the
fence, a plant that will not move stops nothing, and a grant on another disk moves by copy. The rig is
`tests/support/rigs/plants_rig.py`."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from prismoid.device import frames, plants
from prismoid.device.egress import apply_allow
from prismoid.device.frames import FrameRegistry
from prismoid.device.plants import QUARANTINE
from prismoid.device.seatbelt import under
from prismoid.schemas.device import FolderGrant, FrameSpecIn, GrantsIn
from prismoid.schemas.machines import FrameKind
from tests.support.markers import darwin_only
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import frame, git, quarantined
from tests.support.rigs.plants_rig import proj as proj


def test_every_nested_repository_joins_the_fence(tmp_path: Path, proj: Path) -> None:
    """I1: `find_repos` skips `node_modules` and stops at depth 6: the baseline walk does not."""
    deep = proj / "node_modules" / "pkg"
    deep.mkdir(parents=True)
    git("init", "-q", cwd=deep)
    far = proj / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h"
    far.mkdir(parents=True)
    git("init", "-q", cwd=far)
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    assert {str(deep / ".git"), str(far / ".git")} <= set(state.protected_git)
    again = FrameRegistry(home).frames["device-t1"]  # kept across a helper life
    assert str(deep / ".git") in again.protected_git


def test_the_fence_list_stops_at_its_cap(tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap a repository is still the person's (known, never moved), only not fenced."""
    monkeypatch.setattr(frames, "FULL_GIT", 2)
    monkeypatch.setattr(frames, "PROTECTED_CAP", 3)
    for n in range(8):
        (proj / "node_modules" / f"p{n}").mkdir(parents=True)
        git("init", "-q", cwd=proj / "node_modules" / f"p{n}")
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    assert str(proj / "mirror.git") in state.protected_git  # a bare one has no name rule to lean on
    assert str(proj / ".git") in state.protected_git and len(state.protected_git) == 2  # shallowest
    assert len(state.pinned_git) == 3
    assert FrameRegistry(home).frames["device-t1"].pinned_git == state.pinned_git
    state.sweep_plants()
    assert quarantined(home) == [] and (proj / "node_modules" / "p7" / ".git").is_dir()


def test_a_link_out_of_the_write_roots_is_not_put_on_the_fence_list(tmp_path: Path, proj: Path) -> None:
    """The fence refuses a protected path outside its roots: one such entry would stop every
    command of the frame."""
    (tmp_path / "gitstore").mkdir()
    (proj / "linked").mkdir()
    (proj / "linked" / ".git").symlink_to(tmp_path / "gitstore")
    state = frame(tmp_path / "home", FolderGrant(path=str(proj), mode="rw"))
    assert all(under(os.path.realpath(g), str(proj)) for g in state.protected_git)


def test_a_decoy_named_manifest_never_ends_the_walk(tmp_path: Path, proj: Path) -> None:
    """I2: a plant named `manifest.json`, sorted before the real one."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "a").mkdir()
    (proj / "a" / "manifest.json").symlink_to(Path.home())
    (proj / "zsub").mkdir()
    git("init", "-q", cwd=proj / "zsub")
    state.sweep_plants()
    assert sorted(k["virtual_path"] for k in quarantined(home)) == [
        "/work/a/manifest.json",
        "/work/zsub/.git",
    ]
    assert not (proj / "zsub" / ".git").exists()


def test_one_plant_that_will_not_move_never_stops_the_next(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "a-link").symlink_to(Path.home())
    (proj / "b-link").symlink_to(Path.home())
    real = os.rename

    def stuck(src: Any, dst: Any, **k: Any) -> None:
        if src == "a-link":
            raise OSError(errno.EIO, "no")
        real(src, dst, **k)

    with monkeypatch.context() as m:
        m.setattr(plants.os, "rename", stuck)
        state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/b-link"]
    assert any(d["host"] == "/work/a-link" and "could not be moved" in d["reason"] for d in state.denials)
    state.sweep_plants()  # tried again
    assert sorted(k["virtual_path"] for k in quarantined(home)) == ["/work/a-link", "/work/b-link"]


@darwin_only
def test_an_immutable_flag_does_not_keep_a_plant(tmp_path: Path, proj: Path) -> None:
    """I3: `chflags uchg sub`: the walk clears the flags that stop its rename."""
    assert sys.platform == "darwin"  # chflags is the BSD call
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    sub = proj / "sub"
    sub.mkdir()
    git("init", "-q", cwd=sub)
    (sub / ".git").chmod(0o500)
    os.chflags(sub / ".git", stat.UF_IMMUTABLE)
    os.chflags(sub, stat.UF_IMMUTABLE)
    try:
        state.sweep_plants()
        assert not (sub / ".git").exists()
        assert [k["virtual_path"] for k in quarantined(home)] == ["/work/sub/.git"]
    finally:
        os.chflags(sub, 0)


def test_a_failed_move_leaves_no_slot(tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """I3: a slot exists only for what is in it."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "away").symlink_to(Path.home())

    def stuck(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EPERM, "no")

    with monkeypatch.context() as m:
        m.setattr(plants.os, "rename", stuck)
        state.sweep_plants()
        state.sweep_plants()
    base = home / QUARANTINE / "device-t1"
    assert not base.exists() or list(base.iterdir()) == []


def test_the_persons_entries_survive_a_rename(tmp_path: Path, proj: Path) -> None:
    """I4: any refactor by the agent: known entries are matched by identity, not by path."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "vendor").rename(proj / "third_party")
    (proj / "docs").rename(proj / "documents")
    state.sweep_plants()
    assert quarantined(home) == []
    assert (proj / "third_party" / "lib" / ".git").is_dir() and (proj / "documents").is_symlink()
    (proj / "documents").unlink()  # a new inode and another target: not the person's
    (tmp_path / "other").mkdir()
    (proj / "documents").symlink_to(tmp_path / "other")
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/documents"]


def test_the_persons_entries_survive_a_chmod_cycle(tmp_path: Path, proj: Path) -> None:
    """I4: `chmod 000 data`, a walk, `chmod 755 data`, a walk."""
    (proj / "data").mkdir()
    git("init", "-q", cwd=proj / "data")
    (proj / "data" / "out").symlink_to(tmp_path / "elsewhere")
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "data").chmod(0)
    state.sweep_plants()
    (proj / "data").chmod(0o755)
    state.sweep_plants()
    assert quarantined(home) == []
    assert (proj / "data" / ".git").is_dir() and (proj / "data" / "out").is_symlink()


def test_widening_the_list_keeps_the_guards_denials(tmp_path: Path, proj: Path) -> None:
    """M3: a wider allowlist forgets the proxy's refusals; a plant's is no such thing."""
    state = frame(tmp_path / "home", FolderGrant(path=str(proj), mode="rw"))
    (proj / "away").symlink_to(Path.home())
    state.sweep_plants()
    state.denials.append({"ref": state.ref, "host": "a.test", "reason": "not in allowlist"})
    assert apply_allow(state, ["a.test"], None)[0] == 200
    assert [d["host"] for d in state.denials] == ["/work/away"]


def test_a_link_into_a_revoked_grant_is_outward_at_the_next_walk(tmp_path: Path, proj: Path) -> None:
    """M4: what was inward is judged again when the grants change."""
    home = tmp_path / "home"
    other = (tmp_path / "other").resolve()
    other.mkdir()
    registry = FrameRegistry(home)
    both = [FolderGrant(path=str(proj), mode="rw"), FolderGrant(path=str(other), mode="rw")]
    state = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=both))
    state.baseline_plants()
    (proj / "sibling").symlink_to(other)
    state.sweep_plants()
    assert quarantined(home) == []
    registry.regrant(state, GrantsIn(grants=both[:1]))
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/sibling"]


def test_a_link_into_a_read_only_grant_stays(tmp_path: Path, proj: Path) -> None:
    """M5: the frame's own read-only grant is one of the person's granted folders."""
    home = tmp_path / "home"
    other = (tmp_path / "other").resolve()
    other.mkdir()
    state = frame(home, FolderGrant(path=str(proj), mode="rw"), FolderGrant(path=str(other), mode="ro"))
    (proj / "ro-link").symlink_to(other)
    state.sweep_plants()
    assert quarantined(home) == []


@darwin_only
def test_a_grant_on_another_disk_moves_by_copy(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: the rename across a disk fails with EXDEV, and the copy keeps links as links."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    made = proj / "sub"
    made.mkdir()
    git("init", "-q", cwd=made)
    (made / ".git" / "out").symlink_to(Path.home())

    def across(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EXDEV, "no")

    with monkeypatch.context() as m:
        m.setattr(plants.os, "rename", across)
        state.sweep_plants()
    moved = home / QUARANTINE / "device-t1" / "1" / "item" / ".git"
    assert not (made / ".git").exists() and (moved / "out").is_symlink()


def test_an_unchanged_folder_is_not_listed_again(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk is bounded by change: only folders whose ctime moved are listed. A command cannot
    hide a change by setting the folder's mtime back - ctime is not settable."""
    home = tmp_path / "home"
    for n in range(50):
        (proj / "tree" / f"d{n}" / "e").mkdir(parents=True)
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    listed: list[object] = []
    real = os.scandir

    def counting(path: Any) -> Any:
        listed.append(path)
        return real(path)

    monkeypatch.setattr(plants.os, "scandir", counting)
    state.sweep_plants()
    assert listed == []
    folder = proj / "tree" / "d7" / "e"
    before = folder.stat()
    (folder / "link").symlink_to(Path.home())
    os.utime(folder, ns=(before.st_atime_ns, before.st_mtime_ns))  # mtime put back as it was
    state.sweep_plants()
    assert len(listed) == 2  # the changed folder, and again after what was moved out of it
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/tree/d7/e/link"]


def test_a_folder_swapped_for_a_link_is_never_followed_to_move(tmp_path: Path, proj: Path) -> None:
    """Between the walk and the move, a parent swapped for a link to the person's other folder:
    the move opens each folder without following a link, so their file is never moved."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    theirs = tmp_path / "theirs"
    (theirs / "x").mkdir(parents=True)
    git("init", "-q", cwd=theirs / "x")
    (proj / "a" / "x").mkdir(parents=True)
    git("init", "-q", cwd=proj / "a" / "x")
    real_walk = state.plants._walk  # pyright: ignore[reportPrivateUsage]

    def walk_then_swap(root: str, known: Any = None) -> Any:
        found = real_walk(root, known)
        shutil.rmtree(proj / "a")
        (proj / "a").symlink_to(theirs)
        return found

    state.plants._walk = walk_then_swap  # pyright: ignore[reportPrivateUsage]
    state.sweep_plants()
    assert (theirs / "x" / ".git").is_dir()
    assert quarantined(home) == []
    assert "could not be moved" in state.denials[-1]["reason"]
