# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The plants walk: a new folder of a repository's names, the
restore, a protected repository, and a folder that cannot be listed. The rig is `tests/support/rigs/plants_rig.py`."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from prismoid.device import plants
from prismoid.device.plants import QUARANTINE, Plants
from prismoid.schemas.device import FolderGrant
from tests.support.markers import darwin_only
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import frame, git, quarantined
from tests.support.rigs.plants_rig import proj as proj


def test_the_restore_follows_no_link_on_its_way(tmp_path: Path) -> None:
    """Fix round 3, the Minor: a parent swapped for a link between its visit and a descendant's
    restore. The chmod goes a component at a time, so a folder outside the grant keeps its mode."""
    root = (tmp_path / "proj").resolve()
    (root / "a" / "b").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    (outside / "b").chmod(0)
    home = tmp_path / "home"
    store = Plants(tmp_path / "plants.json", home, "device-t1")
    (root / "a" / "b").rmdir()
    (root / "a").rmdir()
    (root / "a").symlink_to(outside)
    anchor = plants._Anchor(os.open(root, os.O_RDONLY | os.O_DIRECTORY), str(root))  # pyright: ignore[reportPrivateUsage]
    try:
        st = os.stat(outside / "b")
        store._restore(anchor, "a/b", str(root / "a" / "b"), st)  # pyright: ignore[reportPrivateUsage]
        assert stat.S_IMODE(os.stat(outside / "b").st_mode) == 0
    finally:
        os.close(anchor.fd)
        (outside / "b").chmod(0o755)


def test_the_restore_still_opens_a_deep_folder(tmp_path: Path) -> None:
    """Going a component at a time changes nothing for a folder really there."""
    root = (tmp_path / "proj").resolve()
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(root), mode="rw"))
    (deep / "out").symlink_to(Path.home())
    deep.chmod(0o311)
    try:
        state.sweep_plants()
        assert [k["virtual_path"] for k in quarantined(home)] == ["/work/a/b/c/out"]
        assert stat.S_IMODE(deep.stat().st_mode) & 0o700 == 0o700
    finally:
        deep.chmod(0o755)


def test_a_commondir_fifo_never_hangs_the_walk(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    store = proj / "store"
    store.mkdir()
    (store / "HEAD").write_text("ref: refs/heads/main\n")
    os.mkfifo(store / "commondir")
    done = threading.Event()
    threading.Thread(target=lambda: (state.sweep_plants(), done.set()), daemon=True).start()
    assert done.wait(5)


@darwin_only
def test_a_new_inode_at_a_protected_path_is_a_plant(tmp_path: Path, proj: Path) -> None:
    """C3: the path alone blesses nothing: a hostile `.git` at a protected path is moved. APFS never
    hands a deleted inode number on; ext4 does, so on Linux the new `.git` looks like the old one."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    nested = proj / "vendor" / "lib"
    assert str(nested / ".git") in state.protected_git
    shutil.rmtree(nested / ".git")
    git("init", "-q", cwd=nested)
    state.sweep_plants()
    assert not (nested / ".git").exists()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/vendor/lib/.git"]


def test_an_unreadable_folder_is_opened_and_walked(tmp_path: Path, proj: Path) -> None:
    """C4: a repository under `chmod 311` (traversable, not listable): git works, so must the walk."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "sub" / "x").mkdir(parents=True)
    git("init", "-q", cwd=proj / "sub" / "x")
    (proj / "sub").chmod(0o311)
    try:
        state.sweep_plants()
        assert stat.S_IMODE((proj / "sub").stat().st_mode) & 0o700 == 0o700
        assert [k["virtual_path"] for k in quarantined(home)] == ["/work/sub/x/.git"]
    finally:
        (proj / "sub").chmod(0o755)


def test_a_path_past_path_max_is_walked(tmp_path: Path, proj: Path) -> None:
    """C4: a plant deeper than any path can name."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    fd = os.open(proj, os.O_RDONLY)
    for _ in range(400):
        os.mkdir("d" * 20, dir_fd=fd)
        child = os.open("d" * 20, os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.mkdir(".git", dir_fd=fd)
    os.close(fd)
    try:
        state.sweep_plants()
        [kept] = quarantined(home)
        assert kept["virtual_path"].endswith("/.git") and len(kept["virtual_path"]) > 8000
    finally:
        shutil.rmtree(proj / ("d" * 20))  # by descriptor: pytest's own cleanup goes by path


def test_a_folder_that_stays_unlistable_is_a_plant(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4: fail closed, and a failed listing is never remembered as an empty folder."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "sub").mkdir()
    (proj / "sub").chmod(0)
    real, tries = os.chmod, [0]

    def refuse_the_walk(*a: Any, **k: Any) -> None:  # the move's own try, later, goes through
        tries[0] += 1
        if tries[0] == 1:
            raise PermissionError(1, "no")
        real(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(plants.os, "chmod", refuse_the_walk)
        state.sweep_plants()
    moved = home / QUARANTINE / "device-t1" / "1" / "item" / "sub"
    assert [k["kind"] for k in quarantined(home)] == ["opaque"] and moved.is_dir()
    moved.chmod(0o755)


def test_a_person_s_unlistable_folder_at_the_baseline_is_kept(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (proj / "theirs").mkdir()
    (proj / "theirs").chmod(0)
    home = tmp_path / "home"

    def refuse(*_a: Any, **_k: Any) -> None:
        raise PermissionError(1, "no")

    try:
        with monkeypatch.context() as m:
            m.setattr(plants.os, "chmod", refuse)
            state = frame(home, FolderGrant(path=str(proj), mode="rw"))
            state.sweep_plants()
        assert (proj / "theirs").is_dir() and quarantined(home) == []
    finally:
        (proj / "theirs").chmod(0o755)


def test_a_failed_listing_moves_nothing_forgets_nothing_and_is_not_cached(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4/I4: out of file descriptors is no plant, the entries behind it are not forgotten, and the
    next walk lists the folder."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "vendor" / "new").mkdir()
    (proj / "vendor" / "new" / "link").symlink_to(Path.home())
    (proj / "plain").mkdir()
    (proj / "plain" / "keep.txt").write_text("mine")
    real = os.open

    def busy(path: Any, *a: Any, **k: Any) -> int:
        if path in ("vendor", "plain"):
            raise OSError(errno.EMFILE, "no")
        return real(path, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(plants.os, "open", busy)
        state.sweep_plants()
    assert quarantined(home) == []
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/vendor/new/link"]
    assert (proj / "vendor" / "lib" / ".git").is_dir()  # known all along
    assert (proj / "plain" / "keep.txt").read_text() == "mine"  # no descriptors is no plant


def test_a_sweeps_own_move_is_not_hidden_by_a_ctime_that_stayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder's ctime comes from a coarse clock (a Linux jiffy, a second on HFS+): the sweep's own
    move can leave it as the pass before saw it. The next pass lists the folder again anyway, so
    what moved is not tried again and reported as could not be moved."""
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(root), mode="rw"))
    store = root / "store"
    (store / "objects").mkdir(parents=True)
    (store / "refs").mkdir()
    (store / "HEAD").write_text("ref: refs/heads/main\n")
    real = os.stat

    def still(*args: Any, **kwargs: Any) -> os.stat_result:
        """Every folder's ctime stays one value for the whole sweep: a clock that never ticks."""
        st = real(*args, **kwargs)
        if not stat.S_ISDIR(st.st_mode):
            return st
        fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
        return os.stat_result(st[:10], {**fields, "st_ctime_ns": 1, "st_ctime": 1e-9})

    monkeypatch.setattr(os, "stat", still)
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/store"]
    assert state.denials and all("could not be moved" not in d["reason"] for d in state.denials)
