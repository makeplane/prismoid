# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's file door: real paths only, never outside the roots.

The door is not sandboxed, so it mirrors the fence: a symlink out is refused, an agent never
writes `task/`, `artifacts/`, `skills/` or `verify/`, and nothing lands in a user's `.git`.
"""

from __future__ import annotations

import base64
import os
import pwd
import subprocess
from pathlib import Path

import pytest

from prismoid.device import files
from prismoid.device.files import resolve_inside
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.schemas.device import FileIn, FolderGrant, FrameSpecIn
from prismoid.schemas.machines import FrameKind

USER_HOME = pwd.getpwuid(os.getuid()).pw_dir


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _frame(tmp_path: Path, *grants: FolderGrant) -> DeviceFrameState:
    spec = FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=list(grants))
    return FrameRegistry(tmp_path / "home").create(spec)


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_resolve_inside_returns_the_real_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    assert resolve_inside(str(root / "a" / ".." / "a" / "f"), [str(root)]) == (root / "a" / "f").resolve()
    with pytest.raises(PermissionError):
        resolve_inside(str(root / ".." / "elsewhere"), [str(root)])
    with pytest.raises(PermissionError):
        resolve_inside("relative/path", [str(root)])


def test_a_listing_marks_folders_and_never_a_link(tmp_path: Path, proj: Path) -> None:
    """`FrameDriver.list_dir`'s promise, which the directory tar of a publish walks by."""
    (proj / "sub").mkdir()
    (proj / "f").write_text("x")
    (proj / "link").symlink_to(proj / "sub")
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw", name="/work"))
    assert files.list_dir(frame, str(proj)) == ["f", "link", "sub/"]


def test_a_nofollow_read_refuses_a_link_the_plain_read_follows(tmp_path: Path, proj: Path) -> None:
    (proj / "secret").write_text("x")
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw", name="/work"))
    link = Path(frame.paths.out, "link")
    link.symlink_to(proj / "secret")
    assert files.read(frame, str(link)) == b"x"
    with pytest.raises(PermissionError):
        files.read(frame, str(link), nofollow=True)
    Path(frame.paths.out, "plain").write_text("y")
    assert files.read(frame, f"{frame.paths.out}/plain", nofollow=True) == b"y"


def test_a_nofollow_read_refuses_a_link_along_the_path(tmp_path: Path, proj: Path) -> None:
    (proj / "d").mkdir()
    (proj / "d" / "f").write_text("x")
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw", name="/work"))
    Path(frame.paths.out, "dir").symlink_to(proj / "d")
    with pytest.raises(PermissionError):
        files.read(frame, f"{frame.paths.out}/dir/f", nofollow=True)


def test_containment_ignores_case(tmp_path: Path) -> None:
    """APFS and Seatbelt are case-insensitive: `Root/x` IS `root/x`, and `root2` is not `root`."""
    root = tmp_path / "Root"
    root.mkdir()
    assert resolve_inside(str(tmp_path / "ROOT" / "x"), [str(root)])
    (tmp_path / "Root2").mkdir()
    with pytest.raises(PermissionError):
        resolve_inside(str(tmp_path / "Root2" / "x"), [str(root)])


def test_symlink_out_of_root_refused(tmp_path: Path) -> None:
    """`work/link → ~/.ssh` (or any folder outside the roots) is refused by the door;
    the sandboxed half is `test_device_exec.test_symlink_out_of_root_refused`."""
    frame = _frame(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("secret")
    work = Path(frame.paths.work)
    (work / "link").symlink_to(outside)
    (work / "ssh").symlink_to(Path(USER_HOME) / ".ssh")
    (work / "file").symlink_to(outside / "x")
    for path in ("link/x", "ssh/known_hosts", "file"):
        with pytest.raises(PermissionError):
            files.read(frame, str(work / path))
    with pytest.raises(PermissionError):
        files.write(frame, FileIn(path=str(work / "link" / "y"), data_b64=_b64(b"planted")))
    with pytest.raises(PermissionError):
        files.list_dir(frame, str(work / "link"))
    assert not (outside / "y").exists()


def test_a_granted_folder_reached_through_a_symlink_is_its_real_path(tmp_path: Path, proj: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(proj)
    frame = _frame(tmp_path, FolderGrant(path=str(alias), mode="rw"))
    files.write(frame, FileIn(path=str(alias / "a.txt"), data_b64=_b64(b"hi")))
    assert (proj / "a.txt").read_bytes() == b"hi"
    assert files.read(frame, str(proj / "a.txt")) == b"hi"


def test_agent_cannot_write_task_or_artifacts(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    p = frame.paths
    for root in (p.task, p.artifacts, p.skills, p.verify, p.inputs):
        with pytest.raises(PermissionError):
            files.write(frame, FileIn(path=f"{root}/x.txt", data_b64=_b64(b"forged")))
        assert not Path(root, "x.txt").exists()
    # The frame's own bookkeeping (frame.json, the journal) sits beside the roots, never in one.
    with pytest.raises(PermissionError):
        files.write(frame, FileIn(path=str(Path(p.task).parent / "frame.json"), data_b64=_b64(b"{}")))
    for root in (p.work, p.out):
        files.write(frame, FileIn(path=f"{root}/sub/ok.txt", data_b64=_b64(b"ok")))
        assert Path(root, "sub", "ok.txt").read_bytes() == b"ok"
    # The agent still reads what it was given.
    Path(p.task, "plan.md").write_text("the plan")
    assert files.read(frame, f"{p.task}/plan.md") == b"the plan"


def test_system_write_lands_in_task(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    p = frame.paths
    for root in (p.task, p.inputs, p.artifacts, p.skills, p.verify):
        files.write(frame, FileIn(path=f"{root}/s.txt", data_b64=_b64(b"sys"), owner="system", mode=0o600))
        written = Path(root, "s.txt")
        assert written.read_bytes() == b"sys"
        assert written.stat().st_mode & 0o777 == 0o600
    with pytest.raises(PermissionError):  # system writes still stay inside the frame
        files.write(frame, FileIn(path=str(tmp_path / "x"), data_b64=_b64(b"x"), owner="system"))


def test_a_ro_grant_reads_but_never_writes(tmp_path: Path, proj: Path) -> None:
    (proj / "r.txt").write_text("read me")
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="ro"))
    assert files.read(frame, str(proj / "r.txt")) == b"read me"
    assert files.list_dir(frame, str(proj)) == ["r.txt"]
    with pytest.raises(PermissionError):
        files.write(frame, FileIn(path=str(proj / "w.txt"), data_b64=_b64(b"x")))


def test_a_write_never_follows_a_hard_link_or_symlink_at_the_leaf(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    work = Path(frame.paths.work)
    victim = tmp_path / "victim.txt"
    victim.write_text("theirs")
    (work / "ln").symlink_to(victim)
    with pytest.raises(PermissionError):
        files.write(frame, FileIn(path=str(work / "ln"), data_b64=_b64(b"mine")))
    inside = work / "a.txt"
    inside.write_text("a")
    os.link(inside, work / "b.txt")
    files.write(frame, FileIn(path=str(work / "b.txt"), data_b64=_b64(b"b")))
    assert inside.read_text() == "a"  # the entry was replaced, not written through
    assert victim.read_text() == "theirs"


def test_no_agent_write_lands_in_a_users_git(tmp_path: Path, proj: Path) -> None:
    (proj / ".git" / "hooks").mkdir(parents=True)
    (proj / ".git" / "objects").mkdir()
    (proj / "sub").mkdir()
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw"))
    assert frame.protected_git == (str((proj / ".git").resolve()),)
    for bad in (".git/hooks/pre-commit", ".git/config", ".GIT/config", "sub/.git/HEAD", "new/.git"):
        with pytest.raises(PermissionError):
            files.write(frame, FileIn(path=str(proj / bad), data_b64=_b64(b"x")))
    for bad in (".envrc", ".vscode/tasks.json", "sub/.zshrc", ".idea/x.xml"):
        with pytest.raises(PermissionError):
            files.write(frame, FileIn(path=str(proj / bad), data_b64=_b64(b"x")))
    # The state files everyday git writes stay writable, as in the fence.
    for ok in (".git/HEAD", ".git/index.lock", ".git/objects/ab/cdef", ".git/index.stash.12"):
        files.write(frame, FileIn(path=str(proj / ok), data_b64=_b64(b"ok")))
    files.write(frame, FileIn(path=str(proj / ".gitignore"), data_b64=_b64(b"*.pyc\n")))
    assert not (proj / ".git" / "hooks" / "pre-commit").exists()


def test_secrets_in_a_granted_folder_stay_unread(tmp_path: Path) -> None:
    """A read-only grant of the home itself still never reads `~/.ssh`: the door mirrors READ_DENY."""
    frame = _frame(tmp_path, FolderGrant(path=USER_HOME, mode="ro"))
    for secret in (".ssh/known_hosts", ".aws/credentials", "Library/Keychains/login.keychain-db"):
        with pytest.raises(PermissionError):
            files.read(frame, f"{USER_HOME}/{secret}")
    with pytest.raises(PermissionError):
        files.list_dir(frame, f"{USER_HOME}/.ssh")


def test_the_repo_walk_is_bounded_and_persisted(tmp_path: Path, proj: Path) -> None:
    (proj / "a" / ".git").mkdir(parents=True)
    (proj / "wt").mkdir()
    (proj / "wt" / ".git").write_text("gitdir: /elsewhere\n")  # a worktree's pointer file
    (proj / "node_modules" / "dep" / ".git").mkdir(parents=True)
    (proj / ".venv" / ".git").mkdir(parents=True)
    deep = proj.joinpath(*"1234567")
    (deep / ".git").mkdir(parents=True)
    six = proj.joinpath(*"123456")
    (six / ".git").mkdir()
    (proj / "alias").symlink_to(proj / "a")  # not followed: `a/.git` is found once
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw"))
    real = proj.resolve()
    assert set(frame.protected_git) == {
        str(real / "a" / ".git"),
        str(real / "wt" / ".git"),
        str(six.resolve() / ".git"),
    }
    # A restarted helper reads the list from frame.json; it never walks again.
    (proj / "b" / ".git").mkdir(parents=True)
    again = FrameRegistry(tmp_path / "home").frames["device-t1"]
    assert again.protected_git == frame.protected_git


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def test_a_linked_git_folder_is_refused_by_its_real_path(tmp_path: Path, proj: Path) -> None:
    """Review finding 1: `p2/.git → store/realgit` must not let `p2/.git/hooks/...` land in the store."""
    store = proj / "store" / "realgit"
    (store / "hooks").mkdir(parents=True)
    (store / "objects").mkdir()
    (proj / "p2").mkdir()
    (proj / "p2" / ".git").symlink_to(store)
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw"))
    assert str(store.resolve()) in frame.protected_git
    for bad in ("p2/.git/hooks/pre-commit", "p2/.git/config", "store/realgit/hooks/pre-commit"):
        with pytest.raises(PermissionError):
            files.write(frame, FileIn(path=str(proj / bad), data_b64=_b64(b"#!/bin/sh\nevil\n")))
    assert not (store / "hooks" / "pre-commit").exists()
    files.write(frame, FileIn(path=str(proj / "p2/.git/objects/ab/cd"), data_b64=_b64(b"obj")))


def test_a_separate_git_dir_and_a_bare_repo_are_protected(tmp_path: Path, proj: Path) -> None:
    """Review finding 2: a pointer file's gitdir, its commondir, and a bare repository's hooks."""
    (proj / "store").mkdir()
    _git("init", "-q", f"--separate-git-dir={proj / 'store' / 'sep'}", str(proj / "p2"))
    _git(
        "-C",
        str(proj / "p2"),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "a",
    )
    _git("-C", str(proj / "p2"), "worktree", "add", "-q", "-b", "wt", str(proj / "wt"))
    _git("init", "-q", "--bare", str(proj / "remote.git"))
    frame = _frame(tmp_path, FolderGrant(path=str(proj), mode="rw"))
    real = proj.resolve()
    assert {str(real / "store" / "sep"), str(real / "remote.git"), str(real / "p2" / ".git")} <= set(
        frame.protected_git
    )
    assert str(real / "store" / "sep" / "worktrees" / "wt") in frame.protected_git
    for bad in ("store/sep/hooks/pre-commit", "store/sep/config", "remote.git/hooks/pre-receive", "p2/.git"):
        with pytest.raises(PermissionError):
            files.write(frame, FileIn(path=str(proj / bad), data_b64=_b64(b"x")))
    files.write(frame, FileIn(path=str(proj / "store/sep/refs/heads/x"), data_b64=_b64(b"0" * 40)))


def test_reads_and_writes_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 10)
    frame = _frame(tmp_path)
    work = Path(frame.paths.work)
    with pytest.raises(files.FileTooLarge):
        files.write(frame, FileIn(path=str(work / "big"), data_b64=_b64(b"x" * 11)))
    (work / "big").write_bytes(b"x" * 11)
    with pytest.raises(files.FileTooLarge):
        files.read(frame, str(work / "big"))
    files.write(frame, FileIn(path=str(work / "ok"), data_b64=_b64(b"x" * 10)))


def test_a_rewrite_without_a_mode_keeps_the_files_own(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    script = Path(frame.paths.work) / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    files.write(frame, FileIn(path=str(script), data_b64=_b64(b"#!/bin/sh\necho hi\n")))
    assert script.stat().st_mode & 0o777 == 0o755
    files.write(frame, FileIn(path=str(script), data_b64=_b64(b"x"), mode=0o600))
    assert script.stat().st_mode & 0o777 == 0o600
    files.write(frame, FileIn(path=str(Path(frame.paths.work) / "new.txt"), data_b64=_b64(b"x")))
    assert (Path(frame.paths.work) / "new.txt").stat().st_mode & 0o777 == 0o644


def test_a_nul_in_a_path_is_a_value_error_not_a_crash(tmp_path: Path) -> None:
    frame = _frame(tmp_path)
    with pytest.raises(ValueError):
        files.read(frame, f"{frame.paths.work}/a\x00b")


def test_a_read_only_grant_inside_a_read_write_one_refuses_every_write(tmp_path: Path, proj: Path) -> None:
    """The fence refuses it, so the door does: neither an agent's write nor a system's lands there."""
    vendor = proj / "vendor"
    (vendor / "sub").mkdir(parents=True)
    frame = _frame(
        tmp_path,
        FolderGrant(path=str(proj), mode="rw"),
        FolderGrant(path=str(vendor), mode="ro", name="/grants/vendor"),
    )
    for path in (vendor / "new", vendor / "sub" / "new"):
        with pytest.raises(PermissionError, match="read-only"):
            files.write(frame, FileIn(path=str(path), data_b64=_b64(b"x")))
    with pytest.raises(PermissionError, match="read-only"):
        files.write(frame, FileIn(path=str(vendor / "new"), data_b64=_b64(b"x"), owner="system"))
    files.write(frame, FileIn(path=str(proj / "beside"), data_b64=_b64(b"x")))


def test_a_read_write_grant_inside_a_read_only_one_is_still_written(tmp_path: Path, proj: Path) -> None:
    """As in the fence: the deny is for a read-only root under a write root, not the other way."""
    inner = proj / "inner"
    inner.mkdir()
    frame = _frame(
        tmp_path,
        FolderGrant(path=str(proj), mode="ro", name="/grants/proj"),
        FolderGrant(path=str(inner), mode="rw", name="/work"),
    )
    files.write(frame, FileIn(path=str(inner / "new"), data_b64=_b64(b"x")))
    assert (inner / "new").read_bytes() == b"x"
