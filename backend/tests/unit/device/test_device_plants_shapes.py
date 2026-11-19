# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The shapes git takes for a repository: a folder shaped like a
`.git`, a `HEAD` that dangles, a `commondir` that is followed as git follows it, the table of every
shape git takes, and what a moved shape hides. The rig is `tests/support/rigs/plants_rig.py`."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from prismoid.device.frames import DeviceFrameState
from prismoid.device.plants import QUARANTINE
from prismoid.schemas.device import FolderGrant
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import frame, git, is_repository, quarantined
from tests.support.rigs.plants_rig import proj as proj


def _shape(folder: Path, *, head: str = "file") -> None:
    (folder / "objects").mkdir(exist_ok=True)
    (folder / "refs").mkdir(exist_ok=True)
    if head == "file":
        (folder / "HEAD").write_text("ref: refs/heads/main\n")
    else:
        (folder / "HEAD").symlink_to("refs/heads/main")


def test_a_shape_at_the_grant_root_loses_its_new_files(tmp_path: Path) -> None:
    """C2: the review's escape by plain files: at the root of a grant with no repository. The new
    shape files go one by one; the person's own files stay."""
    root = (tmp_path / "plain").resolve()
    root.mkdir()
    (root / "notes.md").write_text("mine")
    (root / "config").write_text("[mine]\n\tfirst = yes\n")
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(root), mode="rw"))
    _shape(root)
    (root / "ev.sh").write_text("#!/bin/sh\n")
    assert is_repository(root)
    state.sweep_plants()
    assert not is_repository(root)
    assert (root / "notes.md").read_text() == "mine"
    assert "first" in (root / "config").read_text()  # not new: not moved
    [kept] = quarantined(home)
    assert kept["virtual_path"] == "/work" and kept["kind"] == "bare"
    assert sorted(kept["files"]) == ["HEAD", "objects", "refs"]
    moved = (home / QUARANTINE / "device-t1" / "1" / "item").iterdir()
    assert sorted(p.name for p in moved) == sorted(kept["files"])


def test_head_as_a_dangling_link_is_a_shape(tmp_path: Path, proj: Path) -> None:
    """C2: `HEAD -> refs/heads/main`: git reads the link's text, and it dangles."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    store = proj / "store"
    store.mkdir()
    _shape(store, head="link")
    assert is_repository(store)
    state.sweep_plants()
    assert not is_repository(store)
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/store"]


def test_commondir_is_followed_as_git_follows_it(tmp_path: Path, proj: Path) -> None:
    """C2: `HEAD` and a `commondir` naming where `objects/` and `refs/` really are."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    store = proj / "store"
    (store / ".x" / "objects").mkdir(parents=True)
    (store / ".x" / "refs").mkdir()
    (store / "HEAD").write_text("ref: refs/heads/main\n")
    (store / "commondir").write_text(".x\n")
    assert is_repository(store)
    state.sweep_plants()
    assert not is_repository(store)


def test_a_commondir_repository_completed_later_is_seen(tmp_path: Path, proj: Path) -> None:
    """What makes it a repository lies outside the folder, whose own ctime never moves."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    store = proj / "store"
    store.mkdir()
    (store / "HEAD").write_text("ref: refs/heads/main\n")
    (store / "commondir").write_text("../.x\n")
    state.sweep_plants()
    (proj / ".x" / "objects").mkdir(parents=True)
    (proj / ".x" / "refs").mkdir()
    assert not is_repository(store)  # caught by its names, before it was ever a repository
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/store"]


def test_shape_files_gathered_from_other_folders_are_new(tmp_path: Path, proj: Path) -> None:
    """Each harmless alone, walked and known; together, in one folder, a repository."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    for folder in ("a", "b", "c", "d"):
        (proj / folder).mkdir()
    (proj / "a" / "HEAD").write_text("ref: refs/heads/main\n")
    (proj / "b" / "objects").mkdir()
    (proj / "c" / "refs").mkdir()
    state.sweep_plants()
    assert quarantined(home) == []
    (proj / "a" / "HEAD").rename(proj / "d" / "HEAD")
    (proj / "b" / "objects").rename(proj / "d" / "objects")
    (proj / "c" / "refs").rename(proj / "d" / "refs")
    state.sweep_plants()
    assert not is_repository(proj / "d")


def _head(folder: Path) -> None:
    (folder / "HEAD").write_text("ref: refs/heads/main\n")


def _dirs(folder: Path) -> None:
    (folder / "objects").mkdir()
    (folder / "refs").mkdir()


def _executables(folder: Path) -> None:
    for name in ("objects", "refs"):
        (folder / name).write_text("")
        (folder / name).chmod(0o755)


def _outside(folder: Path) -> Path:
    """A real repository beside the grant, for what points at one."""
    there = folder.parent.parent / "outside" / folder.name
    there.mkdir(parents=True)
    git("init", "-q", "--bare", str(there), cwd=there)
    return there


def _s_plain(f: Path) -> None:
    _head(f)
    _dirs(f)


def _s_executable_files(f: Path) -> None:
    """The re-review's Critical: git asks `access(X_OK)`, and a regular +x file answers."""
    _head(f)
    _executables(f)


def _s_plain_files(f: Path) -> None:
    _head(f)
    (f / "objects").write_text("")
    (f / "refs").write_text("")


def _s_links_to_folders(f: Path) -> None:
    _head(f)
    there = _outside(f)
    (f / "objects").symlink_to(there / "objects")
    (f / "refs").symlink_to(there / "refs")


def _s_links_to_files(f: Path) -> None:
    _head(f)
    (f.parent / "x-file").write_text("")
    (f.parent / "x-file").chmod(0o755)
    (f / "objects").symlink_to("../x-file")
    (f / "refs").symlink_to("../x-file")


def _s_dangling_links(f: Path) -> None:
    (f / "HEAD").symlink_to("refs/heads/main")
    _dirs(f)
    (f / "objects2").symlink_to("nowhere")


def _s_dangling_objects(f: Path) -> None:
    _head(f)
    (f / "objects").symlink_to("nowhere")
    (f / "refs").symlink_to("nowhere-either")


def _s_detached_head(f: Path) -> None:
    (f / "HEAD").write_text("0" * 40 + "\n")
    _dirs(f)


def _s_head_folder(f: Path) -> None:
    (f / "HEAD").mkdir()
    _dirs(f)


def _s_lower_case(f: Path) -> None:
    (f / "head").write_text("ref: refs/heads/main\n")
    (f / "OBJECTS").mkdir()
    (f / "Refs").mkdir()


def _s_commondir_one(f: Path) -> None:
    _head(f)
    (f / ".x" / "objects").mkdir(parents=True)
    (f / ".x" / "refs").mkdir()
    (f / "commondir").write_text(".x\n")


def _s_commondir_absolute(f: Path) -> None:
    _head(f)
    (f / "commondir").write_text(f"{_outside(f)}\n")


def _s_commondir_executables(f: Path) -> None:
    _head(f)
    (f / ".x").mkdir()
    _executables(f / ".x")
    (f / "commondir").write_text(".x\n")


def _s_commondir_two(f: Path) -> None:
    _head(f)
    (f / ".a").mkdir()
    (f / ".a" / "commondir").write_text("../.b\n")
    (f / ".b" / "objects").mkdir(parents=True)
    (f / ".b" / "refs").mkdir()
    (f / "commondir").write_text(".a\n")


def _s_commondir_with_objects(f: Path) -> None:
    """One of the pair beside `HEAD`, the other through `commondir`."""
    _head(f)
    (f / "objects").mkdir()
    (f / ".x" / "refs").mkdir(parents=True)
    (f / "commondir").write_text(".x\n")


def _s_commondir_link(f: Path) -> None:
    _head(f)
    there = _outside(f)
    (f / "commondir").symlink_to(there / "HEAD")  # any entry of the name: its content is not read


def _s_pointer_file(f: Path) -> None:
    (f / ".git").write_text(f"gitdir: {_outside(f)}\n")


def _s_pointer_chain(f: Path) -> None:
    """A pointer to a folder that is a linked worktree's: a git folder with its own `commondir`."""
    there = _outside(f)
    (there.parent / "wt").mkdir()
    (there.parent / "wt" / "HEAD").write_text("ref: refs/heads/main\n")
    (there.parent / "wt" / "commondir").write_text(f"{there}\n")
    (f / ".git").write_text(f"gitdir: {there.parent / 'wt'}\n")


def _s_git_link(f: Path) -> None:
    (f / ".git").symlink_to(_outside(f))


def _s_git_folder(f: Path) -> None:
    git("init", "-q", cwd=f)


def _s_git_upper_case(f: Path) -> None:
    (f / ".GIT").write_text(f"gitdir: {_outside(f)}\n")


def _s_git_fifo(f: Path) -> None:
    os.mkfifo(f / ".git")


def _s_git_socket(f: Path) -> None:
    """A socket's path must fit `sun_path`: bound in a short folder, then moved in."""
    short = tempfile.mkdtemp(prefix="pg", dir="/tmp")
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(f"{short}/s")
        os.rename(f"{short}/s", f / ".git")
    finally:
        shutil.rmtree(short, ignore_errors=True)


SHAPES: Final = [
    _s_plain,
    _s_executable_files,
    _s_plain_files,
    _s_links_to_folders,
    _s_links_to_files,
    _s_dangling_links,
    _s_dangling_objects,
    _s_detached_head,
    _s_head_folder,
    _s_lower_case,
    _s_commondir_one,
    _s_commondir_absolute,
    _s_commondir_executables,
    _s_commondir_two,
    _s_commondir_with_objects,
    _s_commondir_link,
    _s_pointer_file,
    _s_pointer_chain,
    _s_git_link,
    _s_git_folder,
    _s_git_upper_case,
    _s_git_fifo,
    _s_git_socket,
]


ALWAYS_PLANTS: Final = (_s_git_fifo, _s_git_socket, _s_head_folder, _s_plain_files, _s_commondir_two)


def _git_takes(folder: Path) -> bool:
    """Does the REAL git take `folder` for a repository? Nothing of the user's config, no search
    above it, and no hook runs: `rev-parse` only reads."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(folder.parent),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(folder.parent),
    }
    for question in ("--git-dir", "--is-bare-repository"):
        done = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", question], capture_output=True, env=env, timeout=10
        )
        if done.returncode != 0:
            return False
    return True


def _built(tmp_path: Path, shape: Callable[[Path], None]) -> tuple[DeviceFrameState, Path]:
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    state = frame(tmp_path / "home", FolderGrant(path=str(root), mode="rw"))
    folder = root / "store"
    folder.mkdir()
    shape(folder)
    return state, folder


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.__name__.removeprefix("_s_"))
def test_every_shape_git_takes_is_caught(tmp_path: Path, shape: Callable[[Path], None]) -> None:
    """The conformance table: the real git is the judge. Whatever it takes for a repository is
    moved, and it takes it no longer; the scan may be looser than git, never narrower."""
    state, folder = _built(tmp_path, shape)
    takes = _git_takes(folder)
    state.sweep_plants()
    assert not _git_takes(folder), "git still takes it"
    if takes or shape in ALWAYS_PLANTS:
        assert state.denials, "nothing was refused"
        assert all("could not be moved" not in d["reason"] for d in state.denials)


def test_the_table_has_teeth(tmp_path: Path) -> None:
    """If git stopped taking these the table would prove nothing."""
    taken = []
    for n, shape in enumerate(SHAPES):
        (tmp_path / str(n)).mkdir()
        _, folder = _built(tmp_path / str(n), shape)
        if _git_takes(folder):
            taken.append(shape.__name__)
    assert {"_s_plain", "_s_executable_files", "_s_commondir_one", "_s_pointer_file"} <= set(taken)
    assert len(taken) >= 10, taken


def test_the_reviewers_fsmonitor_probe_is_quarantined(tmp_path: Path) -> None:
    """Regular +x `objects` and `refs`, `bare=false`, `worktree=.`, `core.fsmonitor`: git status in
    the folder ran the script. Nothing is left for it to run."""
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(root), mode="rw"))
    sub = root / "sub"
    sub.mkdir()
    marker = tmp_path / "ran"
    (sub / "ev.sh").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (sub / "ev.sh").chmod(0o755)
    _s_executable_files(sub)
    (sub / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n\tfsmonitor = ./ev.sh\n"
    )
    assert is_repository(sub)
    state.sweep_plants()
    assert not is_repository(sub)
    [kept] = quarantined(home)
    assert kept["virtual_path"] == "/work/sub" and sorted(kept["files"]) == [
        "HEAD",
        "config",
        "objects",
        "refs",
    ]
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(root)}
    subprocess.run(["git", "-C", str(sub), "status"], capture_output=True, env=env, timeout=30)
    assert not marker.exists() and (sub / "ev.sh").exists()


def test_a_new_folder_of_those_names_is_a_plant_with_no_look_inside(tmp_path: Path) -> None:
    """Names alone: nothing is opened, read or `access`ed, so nothing can be arranged to answer."""
    state, folder = _built(tmp_path, _s_plain_files)
    state.sweep_plants()
    assert not (folder / "HEAD").exists()


def test_a_known_shaped_folder_hides_nothing_beneath_it(tmp_path: Path) -> None:
    """A folder the person's own names shape is walked like any other: being known is no cover."""
    root = (tmp_path / "proj").resolve()
    (root / "fixture" / "objects" / "deep").mkdir(parents=True)
    (root / "fixture" / "refs").mkdir()
    (root / "fixture" / "sub").mkdir()
    _head(root / "fixture")
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(root), mode="rw"))
    (root / "fixture" / "sub" / "out").symlink_to(Path.home())
    (root / "fixture" / "objects" / "deep" / "out").symlink_to(Path.home())
    state.sweep_plants()
    assert sorted(k["virtual_path"] for k in quarantined(home)) == [
        "/work/fixture/objects/deep/out",
        "/work/fixture/sub/out",
    ]
    assert (root / "fixture" / "HEAD").exists()  # known: the person's


def test_what_is_inside_a_moved_shape_file_is_not_reported_stuck(tmp_path: Path) -> None:
    """A plant inside a new `objects/` goes with it: no false "could not be moved"."""
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(root), mode="rw"))
    store = root / "store"
    store.mkdir()
    _s_plain(store)
    (store / "objects" / "out").symlink_to(Path.home())
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/store"]
    assert all("could not be moved" not in d["reason"] for d in state.denials)


def test_a_shape_around_a_protected_repository_leaves_it_where_it_is(tmp_path: Path, proj: Path) -> None:
    """C3: plain files around the person's protected `.git` must not make Prismoid move it."""
    (proj / "sub").mkdir()
    git("init", "-q", cwd=proj / "sub")
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    inode = (proj / "sub" / ".git").stat().st_ino
    _shape(proj / "sub")
    state.sweep_plants()
    assert (proj / "sub" / ".git").stat().st_ino == inode
    assert not (proj / "sub" / "HEAD").exists()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/sub"]
