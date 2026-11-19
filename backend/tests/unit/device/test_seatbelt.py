# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Seatbelt profile's text: what it allows, and in what order.

SBPL is last-match-wins, so these tests judge file access with `_verdict`, a small model of that
rule over the profile's one-line rules. The darwin tests (tests/integration/device/test_seatbelt_darwin.py)
prove the same profile against the real sandbox.
"""

from __future__ import annotations

import os
import pwd
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from prismoid.device import seatbelt
from prismoid.device.fence_data import (
    CACHE_ENV,
    CACHE_ROOT,
    HOME_PROTECT_WRITE,
    PROTECT_WRITE,
    READ_DENY,
    REGISTRIES,
)
from prismoid.device.seatbelt import FenceSpec, sbpl_string
from prismoid.schemas.device_grants import write_root_refusal

home_dir = pwd.getpwuid(os.getuid()).pw_dir  # rebound per test to a home under tmp_path, below


@pytest.fixture(autouse=True)
def per_test_home(tmp_path: Path) -> Iterator[None]:
    """Each test's user home is its own tmp_path, so tmp roots count as folders in the home (a user
    root must be in the home or on an external disk). Tests that need another pass `user_home`."""
    global home_dir
    real, home_dir = home_dir, os.path.realpath(tmp_path)
    try:
        yield
    finally:
        home_dir = real


def profile_for(spec: FenceSpec, home: Path, *, user_home: str | None = None) -> str:
    return seatbelt.profile_for(spec, home, user_home=user_home or home_dir)


def cache_root(ref: str) -> str:
    return seatbelt.cache_root(ref, user_home=home_dir)


_BARE = re.compile(r"^\((allow|deny) ([a-z*-]+(?: [a-z*-]+)*)\)$")
_STR = r'("(?:[^"\\]|\\.)*")'
_ONE = re.compile(rf"^\((allow|deny) ([a-z*-]+(?: [a-z*-]+)*) \((subpath|literal|regex) {_STR}\)\)$")
_BOTH = re.compile(rf"^\((allow|deny) ([a-z*-]+) \(require-all \(subpath {_STR}\) \(regex {_STR}\)\)\)$")


def _expand(entry: str) -> str:
    return home_dir + entry[1:] if entry.startswith("~/") else entry


def _unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text[1:-1])


Rule = tuple[str, list[str], str | None, str | None, str | None]


def _rules(profile: str) -> list[Rule]:
    """(allow|deny, operations, filter, value, regex) for every file rule, in profile order; a
    `require-all` of a subpath and a regex is filter "subpath" with the regex alongside."""
    out: list[Rule] = []
    for line in profile.splitlines():
        if m := _BARE.match(line):
            out.append((m.group(1), m.group(2).split(), None, None, None))
        elif m := _ONE.match(line):
            out.append((m.group(1), m.group(2).split(), m.group(3), _unquote(m.group(4)), None))
        elif m := _BOTH.match(line):
            out.append((m.group(1), [m.group(2)], "subpath", _unquote(m.group(3)), _unquote(m.group(4))))
    return [r for r in out if any(op.startswith("file-") for op in r[1])]


def _verdict(profile: str, op: str, path: str) -> str:
    """allow|deny for `op` (e.g. file-read-data) on `path`: the last matching rule wins."""
    verdict = "deny"
    for verb, ops, kind, value, also in _rules(profile):
        if not any(o == op or (o.endswith("*") and op.startswith(o[:-1])) for o in ops):
            continue
        if also is not None and not re.search(also, path):
            continue
        if (
            kind is None
            or (
                kind == "subpath"
                and value is not None
                and (path == value or path.startswith(value.rstrip("/") + "/"))
            )
            or (kind == "literal" and path == value)
            or (kind == "regex" and value is not None and re.search(value, path))
        ):
            verdict = verb
    return verdict


def _reads(profile: str, path: str) -> bool:
    return _verdict(profile, "file-read-data", path) == "allow"


def _writes(profile: str, path: str) -> bool:
    return _verdict(profile, "file-write-data", path) == "allow"


def _spec(tmp_path: Path, **kw: Any) -> FenceSpec:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    writes = kw.pop("write_roots", (str(work),))
    return FenceSpec(
        write_roots=writes,
        user_roots=kw.pop("user_roots", writes),  # the folders a person granted, unless told otherwise
        read_only_roots=kw.pop("read_only_roots", ()),
        proxy_port=kw.pop("proxy_port", 3128),
        deny_ports=kw.pop("deny_ports", (5432, 8000)),
        **kw,
    )


def test_profile_starts_closed(tmp_path: Path) -> None:
    assert profile_for(_spec(tmp_path), tmp_path / "home").splitlines()[:2] == [
        "(version 1)",
        "(deny default)",
    ]


def test_profile_denies_reads_of_deny_list(tmp_path: Path) -> None:
    home = tmp_path / "Prismoid Home"
    profile = profile_for(_spec(tmp_path), home)
    assert _reads(profile, "/usr/bin/git") and _reads(profile, f"{home_dir}/code/app/main.py")
    for entry in (*READ_DENY, str(home)):
        path = _expand(entry)
        for probe in (path, f"{path}/inner/file"):
            assert not _reads(profile, probe), probe
            assert not _writes(profile, probe), probe
            assert _verdict(profile, "file-read-metadata", probe) == "deny", probe


def test_profile_write_allows_only_roots_and_caches(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    cache = cache_root("device-t1")
    profile = profile_for(_spec(tmp_path, read_only_roots=(str(ro),), cache_root=cache), tmp_path / "home")
    work = os.path.realpath(tmp_path / "work")
    assert _writes(profile, f"{work}/src/app.py")
    assert _writes(profile, f"{cache}/npm/_cacache/blob")
    for path in (
        f"{os.path.realpath(ro)}/a",
        f"{home_dir}/Documents/a",
        "/usr/local/bin/a",
        "/private/tmp/a",
    ):
        assert not _writes(profile, path), path
    assert _reads(profile, f"{os.path.realpath(ro)}/a")
    # Writing is never broad: every write allow names a root, a cache or a device.
    allowed = {
        value for verb, ops, _, value, _ in _rules(profile) if verb == "allow" and "file-write*" in ops
    }
    assert None not in allowed
    assert allowed <= {work, cache}


def test_device_files_are_writable(tmp_path: Path) -> None:
    profile = profile_for(_spec(tmp_path), tmp_path / "home")
    # /dev/stdout and /dev/stderr resolve to /dev/fd/1 and /dev/fd/2 (the darwin tests write them).
    for path in ("/dev/null", "/dev/fd/1", "/dev/fd/2", "/dev/fd/62"):
        assert _writes(profile, path), path
    for path in ("/dev/disk0", "/dev/fd/1/x", "/dev/fdx"):
        assert not _writes(profile, path), path


def test_the_users_own_caches_are_read_only(tmp_path: Path) -> None:
    mine, theirs = cache_root("device-t1"), cache_root("device-t2")
    profile = profile_for(_spec(tmp_path, cache_root=mine), tmp_path / "home")
    for path in (
        "~/.npm/_npx/x/index.js",
        "~/.cache/p10k-instant-prompt.zsh",
        "~/.cargo/registry/src/a/build.rs",
        "~/go/pkg/mod/x/y.go",
        "~/.local/share/uv/tools/x/bin/x",
        "~/Library/Caches/pip/x",
    ):
        assert _reads(profile, _expand(path)), path
        assert not _writes(profile, _expand(path)), path
    # Each frame writes its own cache only: task A cannot plant what task B builds or runs.
    assert _writes(profile, f"{mine}/cargo/registry/src/a/build.rs")
    for path in (f"{theirs}/cargo/registry/src/a/build.rs", f"{_expand(CACHE_ROOT)}/npm/x"):
        assert not _writes(profile, path), path
    other = profile_for(_spec(tmp_path, cache_root=theirs), tmp_path / "home")
    assert not _writes(other, f"{mine}/uv/wheels/x.whl") and _writes(other, f"{theirs}/uv/wheels/x.whl")


def test_cache_root_is_per_frame() -> None:
    assert cache_root("device-t1") == os.path.join(os.path.realpath(_expand(CACHE_ROOT)), "device-t1")
    for bad in ("", "..", "a/b", "../x", "a b", "x" * 81):
        with pytest.raises(ValueError, match="ref"):
            cache_root(bad)


@pytest.mark.parametrize("cache", ["/private/tmp/cache", "~/.cache", "~/Library/Caches/Prismoid"])
def test_a_frame_cache_outside_the_cache_root_is_refused(tmp_path: Path, cache: str) -> None:
    with pytest.raises(ValueError, match="cache"):
        profile_for(_spec(tmp_path, cache_root=_expand(cache)), tmp_path / "home")


def test_cache_env_points_inside_prismoids_cache_root() -> None:
    names = {
        "npm_config_cache",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_TOOL_DIR",
        "PIP_CACHE_DIR",
        "CARGO_HOME",
        "GOPATH",
        "GOMODCACHE",
        "GOCACHE",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PRE_COMMIT_HOME",
        "XDG_CACHE_HOME",
        "PNPM_HOME",
        "npm_config_store_dir",
        "YARN_CACHE_FOLDER",
        "COREPACK_HOME",
        "HOMEBREW_CACHE",
    }
    assert names <= set(CACHE_ENV)
    for sub in CACHE_ENV.values():
        assert sub and not sub.startswith("/") and ".." not in sub.split("/"), sub


def test_a_cache_overlapping_a_denied_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cache"):
        profile_for(
            _spec(tmp_path, cache_root=cache_root("device-t1")), Path(_expand("~/Library/Caches/Prismoid"))
        )


def test_roots_resolve_through_symlinks_above_them(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "tasks" / "t1" / "work").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    home = tmp_path / "link"  # a Prismoid home reached through a link: the root itself is none
    root = str(home / "tasks" / "t1" / "work")
    profile = profile_for(_spec(tmp_path, write_roots=(root,), user_roots=()), home)
    assert _writes(profile, f"{os.path.realpath(real)}/tasks/t1/work/a")
    if os.path.realpath(tmp_path) != str(tmp_path):  # macOS: /var is /private/var
        assert not _writes(profile, f"{tmp_path}/real/tasks/t1/work/a")


def test_root_inside_prismoid_home_is_reopened(tmp_path: Path) -> None:
    home = tmp_path / "home"
    task = home / "tasks" / "t1"
    (task / "work").mkdir(parents=True)
    (task / "task" / "inputs").mkdir(parents=True)
    spec = _spec(
        tmp_path,
        write_roots=(str(task / "work"),),
        user_roots=(),
        read_only_roots=(str(task / "task" / "inputs"),),
    )
    profile = profile_for(spec, home)
    real = Path(os.path.realpath(task))
    assert _writes(profile, f"{real}/work/a") and _reads(profile, f"{real}/work/a")
    assert _reads(profile, f"{real}/task/inputs/a") and not _writes(profile, f"{real}/task/inputs/a")
    for path in (f"{real}/task/answers.json", f"{real}/out/a", f"{real.parent / 't2'}/work/a"):
        assert not _reads(profile, path), path
    # The folders between the denied home and a reopened root can be looked up, never listed.
    for folder in (real.parent.parent, real.parent, real, real / "task"):
        assert _verdict(profile, "file-read-metadata", str(folder)) == "allow", folder
        assert not _reads(profile, str(folder)), folder
    assert not _reads(profile, f"{real.parent.parent}/helper.key")


REFUSED_ROOTS = (
    "Library",
    "library/LaunchAgents",  # case does not matter on macOS
    "Library/Scripts",
    "Library/Services",
    "Library/Application Support/Code - Insiders/User",
    "Library/Mobile Documents",
    "Library/CloudStorage",
    ".config",
    ".config/nvim",
    ".local/lib/python3.12/site-packages",
    ".vscode",
    ".tmux.conf",
    ".ssh",
)
ALLOWED_ROOTS = (
    "code/app",
    "Library/Mobile Documents/com~apple~CloudDocs/app",
    "Library/CloudStorage/Dropbox/app",
    "dotfiles",
    "bin",
)


@pytest.mark.parametrize("rel", REFUSED_ROOTS)
def test_a_user_root_in_library_or_a_dot_folder_is_refused(tmp_path: Path, rel: str) -> None:
    home = "/Users/me"
    assert write_root_refusal(f"{home}/{rel}", home), rel
    with pytest.raises(ValueError, match=r"Library|hidden|denied"):
        profile_for(_spec(tmp_path, write_roots=(f"{home_dir}/{rel}",)), tmp_path / "home")


@pytest.mark.parametrize("rel", ALLOWED_ROOTS)
def test_ordinary_user_roots_pass(rel: str) -> None:
    assert write_root_refusal(f"/Users/me/{rel}", "/Users/me") is None


def test_a_folder_on_an_external_disk_passes() -> None:
    assert write_root_refusal("/Volumes/Work/proj", "/Users/me") is None
    assert write_root_refusal("/volumes/Work/proj/src", "/Users/me") is None


@pytest.mark.parametrize(
    "path",
    [
        "/opt/homebrew",
        "/opt/homebrew/lib/node_modules",
        "/usr/local",
        "/usr/local/bin",
        "/Applications",
        "/Applications/Code.app",
        "/tmp/proj",
        "/private/tmp/proj",
        "/private/var/folders/x/T/proj",
        "/Volumes",
        "/Volumes/Work",
        "/Users/other/code",
        "/Users/me/Applications",
        "/Users/me/applications/Tool.app",
        "/USERS/ME/Applications",
    ],
)
def test_a_user_root_outside_the_home_and_external_disks_is_refused(path: str) -> None:
    assert write_root_refusal(path, "/Users/me"), path


def test_a_root_containing_the_prismoid_home_does_not_reopen_it(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    home = proj / "Library" / "Application Support" / "Prismoid"
    profile = profile_for(_spec(tmp_path, write_roots=(str(proj),)), home)
    real = os.path.realpath(home)
    assert _writes(profile, f"{os.path.realpath(proj)}/a")
    assert not _reads(profile, f"{real}/helper.key") and not _writes(profile, f"{real}/helper.key")


def test_a_user_root_must_be_a_write_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="user root"):
        profile_for(_spec(tmp_path, user_roots=(str(tmp_path / "elsewhere"),)), tmp_path / "home")


def test_a_prismoid_root_outside_the_prismoid_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Prismoid write root"):
        profile_for(_spec(tmp_path, user_roots=()), tmp_path / "home")


@pytest.mark.parametrize("which", ["home", "its parent", "/"])
def test_a_write_root_holding_the_home_is_refused(tmp_path: Path, which: str) -> None:
    root = {"home": home_dir, "its parent": os.path.dirname(home_dir), "/": "/"}[which]
    with pytest.raises(ValueError, match="home"):
        profile_for(_spec(tmp_path, write_roots=(root,)), tmp_path / "home")


def test_a_root_inside_a_denied_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="denied"):
        profile_for(_spec(tmp_path, read_only_roots=(f"{home_dir}/.ssh",)), tmp_path / "home")


def test_denied_folders_cannot_be_moved_from_under_their_rule(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    home = proj / "Library" / "Application Support" / "Prismoid"
    profile = profile_for(_spec(tmp_path, write_roots=(str(proj),)), home)
    real = os.path.realpath(proj)
    for path in (f"{real}/Library", f"{real}/Library/Application Support"):
        assert _verdict(profile, "file-write-unlink", path) == "deny", path
    assert _verdict(profile, "file-write-unlink", f"{real}/src") == "allow"


def test_profile_protects_hooks_and_rc_files(tmp_path: Path) -> None:
    profile = profile_for(_spec(tmp_path), tmp_path / "home")
    for regex in PROTECT_WRITE:
        assert f"(deny file-write* (regex {sbpl_string(regex)}))" in profile.splitlines(), regex
    work = os.path.realpath(tmp_path / "work")
    for rel in (
        ".envrc",
        ".vscode",
        ".vscode/tasks.json",
        ".vscode/settings.json",
        ".idea",
        ".idea/workspace.xml",
        ".zshrc",
        "sub/.bashrc",
        ".bash_profile",
        ".profile",
        ".zprofile",
        ".zshenv",
        ".zlogout",
        ".bash_logout",
    ):
        assert not _writes(profile, f"{work}/{rel}"), rel
    for rel in (
        ".gitignore",
        ".github/workflows/ci.yml",
        ".vscode/launch.json",
        "src/zshrc.py",
        "notes.profile",
    ):
        assert _writes(profile, f"{work}/{rel}"), rel
    for entry in HOME_PROTECT_WRITE:
        assert not _writes(profile, _expand(entry)), entry
        assert not _writes(profile, f"{_expand(entry)}/inner"), entry


def test_no_git_dir_is_made_in_a_user_root(tmp_path: Path) -> None:
    profile = profile_for(_spec(tmp_path), tmp_path / "home")
    work = os.path.realpath(tmp_path / "work")
    # git finds the nearest .git: one planted in a user's folder runs its config in their prompt.
    for rel in (".git", ".git/config", ".git/hooks/pre-commit", "sub/.git", "sub/.git/config", "deep/a/.git"):
        assert not _writes(profile, f"{work}/{rel}"), rel
    for rel in (".gitignore", ".github/workflows/ci.yml", ".gitattributes", "src/git.py"):
        assert _writes(profile, f"{work}/{rel}"), rel


def test_new_repositories_live_in_prismoids_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    scratch = home / "scratch" / "s1"
    scratch.mkdir(parents=True)
    spec = _spec(
        tmp_path, write_roots=(str(tmp_path / "work"), str(scratch)), user_roots=(str(tmp_path / "work"),)
    )
    profile = profile_for(spec, home)
    real = os.path.realpath(scratch)
    for rel in (".git/config", ".git/hooks/pre-commit", "clone/.git/config", "c/.git"):
        assert _writes(profile, f"{real}/{rel}"), rel
    assert not _writes(profile, f"{os.path.realpath(tmp_path / 'work')}/clone/.git/config")


def _repo(tmp_path: Path) -> tuple[FenceSpec, str]:
    work = tmp_path / "work"
    for sub in ("objects/ab", "refs/heads", "logs", "hooks", "info", "modules/s/hooks", "worktrees/w"):
        (work / ".git" / sub).mkdir(parents=True, exist_ok=True)
    git = os.path.realpath(work / ".git")
    return _spec(tmp_path, protected_git=(str(work / ".git"),)), git


def test_a_pre_existing_repository_keeps_its_config_and_hooks(tmp_path: Path) -> None:
    spec, git = _repo(tmp_path)
    profile = profile_for(spec, tmp_path / "home")
    for rel in (
        "objects/ab/cdef",
        "objects/pack/p.pack",
        "refs/heads/main",
        "refs/heads/main.lock",
        "logs/HEAD",
        "index",
        "index.lock",
        "HEAD",
        "HEAD.lock",
        "ORIG_HEAD",
        "FETCH_HEAD",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "COMMIT_EDITMSG",
        "packed-refs",
        "packed-refs.lock",
        "packed-refs.new",
        "MERGE_MSG",
        "MERGE_MODE",
        "MERGE_RR",
        "AUTO_MERGE",
        "SQUASH_MSG",
        "gc.pid",
        "gc.pid.lock",
        "gc.log",
        "index.stash.4242",
        "index.stash.4242.lock",
    ):
        assert _writes(profile, f"{git}/{rel}"), rel
    for rel in (
        "",
        "config",
        "config.worktree",
        "hooks/pre-commit",
        "info/exclude",
        "info/attributes",
        "commondir",
        "gitdir",
        "modules/s/config",
        "modules/s/hooks/x",
        "worktrees/w/config.worktree",
        "description",
        "shallow",
        "rebase-merge/git-rebase-todo",
        "rebase-apply/patch",
        "sequencer/todo",
        "index.stash.x",
        "index.stash.1/x",
    ):
        assert not _writes(profile, f"{git}/{rel}".rstrip("/")), rel
    # Nor can the repository, or the folder above it, be renamed out from under the rule.
    assert _verdict(profile, "file-write-unlink", os.path.dirname(git)) == "deny"
    assert _reads(profile, f"{git}/config")


def test_a_worktree_git_pointer_is_read_only(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: /elsewhere/.git/worktrees/w\n")
    profile = profile_for(_spec(tmp_path, protected_git=(str(work / ".git"),)), tmp_path / "home")
    assert not _writes(profile, os.path.realpath(work / ".git"))
    assert _writes(profile, f"{os.path.realpath(work)}/src.py")


def test_a_protected_git_outside_the_write_roots_is_refused(tmp_path: Path) -> None:
    (tmp_path / "other" / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="git"):
        profile_for(_spec(tmp_path, protected_git=(str(tmp_path / "other" / ".git"),)), tmp_path / "home")


def _fake_home(tmp_path: Path) -> Path:
    """A home of our own, so links and dotfiles can be planted without touching the real one."""
    home = tmp_path / "user"
    (home / ".ssh").mkdir(parents=True)
    (home / "dotfiles").mkdir()
    (home / "Dropbox" / "keys").mkdir(parents=True)
    (home / "Dropbox" / "keys" / "id_ed25519").write_text("key")
    (home / ".ssh" / "id_ed25519").symlink_to(home / "Dropbox" / "keys" / "id_ed25519")
    (home / "dotfiles" / "zshrc").write_text("")
    (home / ".zshrc").symlink_to(home / "dotfiles" / "zshrc")
    (home / "Library" / "Group Containers" / "2BUA8C4S2C.com.1password").mkdir(parents=True)
    (home / "Library" / "Application Support" / "Code" / "User").mkdir(parents=True)
    (home / "code").mkdir()
    return Path(os.path.realpath(home))


def test_symlinked_secrets_deny_their_targets(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    profile = profile_for(
        _spec(tmp_path, write_roots=(str(home / "Dropbox"),)), tmp_path / "prismoid", user_home=str(home)
    )
    assert not _reads(profile, f"{home}/Dropbox/keys/id_ed25519")
    assert not _writes(profile, f"{home}/Dropbox/keys/id_ed25519")
    assert _reads(profile, f"{home}/Dropbox/keys/other") and _writes(profile, f"{home}/Dropbox/keys/other")


def test_symlinked_rc_files_protect_their_targets(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    profile = profile_for(
        _spec(tmp_path, write_roots=(str(home / "dotfiles"),)), tmp_path / "prismoid", user_home=str(home)
    )
    assert not _writes(profile, f"{home}/dotfiles/zshrc")
    assert _writes(profile, f"{home}/dotfiles/vimrc")


def test_password_manager_containers_are_denied(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    profile = profile_for(
        _spec(tmp_path, write_roots=(str(home / "code"),)), tmp_path / "prismoid", user_home=str(home)
    )
    for name in ("2BUA8C4S2C.com.1password", "2BUA8C4S2C.com.agilebits.onepassword-osx", "x.1Password7"):
        assert not _reads(profile, f"{home}/Library/Group Containers/{name}/data.sqlite"), name
    assert _reads(profile, f"{home}/Library/Group Containers/group.com.apple.notes/x")


def test_home_protection_holds_inside_a_write_root(tmp_path: Path) -> None:
    home = _fake_home(tmp_path)
    # `~/bin` may be granted (it is no dot-folder), yet it is on PATH: the protect list still holds.
    (home / "bin").mkdir()
    profile = profile_for(
        _spec(tmp_path, write_roots=(str(home / "bin"),)), tmp_path / "prismoid", user_home=str(home)
    )
    assert not _writes(profile, f"{home}/bin/git")
    with pytest.raises(ValueError, match="Library"):
        profile_for(
            _spec(tmp_path, write_roots=(str(home / "Library"),)), tmp_path / "prismoid", user_home=str(home)
        )
    profile = profile_for(
        _spec(tmp_path, write_roots=(str(home / "code"),)), tmp_path / "prismoid", user_home=str(home)
    )
    for path in ("Library/LaunchAgents/x.plist", "Library/Application Support/Code/User/settings.json"):
        assert not _writes(profile, f"{home}/{path}"), path
    assert _verdict(profile, "file-write-unlink", f"{home}/Library/Application Support/Code") == "deny"


def test_profile_network_is_proxy_and_loopback_minus_deny_ports(tmp_path: Path) -> None:
    lines = profile_for(
        _spec(tmp_path, proxy_port=3128, deny_ports=(5432, 8000)), tmp_path / "home"
    ).splitlines()
    network = [line for line in lines if line.startswith(("(allow network", "(deny network"))]
    loopback = '(allow network-outbound (remote ip4 "localhost:*") (remote ip6 "localhost:*"))'
    for port in (5432, 8000):
        deny = f'(deny network-outbound (remote ip4 "localhost:{port}") (remote ip6 "localhost:{port}"))'
        assert lines.index(deny) > lines.index(loopback)
    proxy = '(allow network-outbound (remote ip4 "localhost:3128") (remote ip6 "localhost:3128"))'
    assert lines.index(proxy) > lines.index(loopback)
    squat = '(deny network-bind (local ip4 "localhost:3128") (local ip6 "localhost:3128"))'
    bind = '(allow network-bind network-inbound (local ip4 "localhost:*") (local ip6 "localhost:*"))'
    assert lines.index(squat) > lines.index(bind)
    # Nowhere but loopback: no wildcard host, no bare network allow, no unix socket without the grant.
    assert all("localhost:" in line for line in network), network
    assert "unix-socket" not in "\n".join(lines)


def test_fence_spec_refuses_bad_ports() -> None:
    with pytest.raises(ValueError, match="proxy"):
        FenceSpec(write_roots=(), read_only_roots=(), proxy_port=5432, deny_ports=(5432,))
    with pytest.raises(ValueError, match="port"):
        FenceSpec(write_roots=(), read_only_roots=(), proxy_port=0, deny_ports=())
    with pytest.raises(ValueError, match="port"):
        FenceSpec(write_roots=(), read_only_roots=(), proxy_port=3128, deny_ports=(70000,))


def test_docker_socket_only_when_granted(tmp_path: Path) -> None:
    sock = tmp_path / "run" / "docker.sock"
    assert "unix-socket" not in profile_for(_spec(tmp_path), tmp_path / "home")
    granted = profile_for(_spec(tmp_path, docker_socket=str(sock)), tmp_path / "home")
    unix = [line for line in granted.splitlines() if "unix-socket" in line]
    assert unix == [f"(allow network-outbound (remote unix-socket (path-literal {sbpl_string(str(sock))})))"]


def test_sbpl_string_escapes_quotes_and_backslashes() -> None:
    assert (
        sbpl_string("/Users/me/Library/Application Support/Prismoid")
        == '"/Users/me/Library/Application Support/Prismoid"'
    )
    assert sbpl_string('/a "b"\\c') == '"/a \\"b\\"\\\\c"'


@pytest.mark.parametrize("bad", ["/a\nb", "/a\x00b", "/a\rb", "/a\tb", "/a\x7fb"])
def test_sbpl_string_refuses_control_characters(bad: str) -> None:
    with pytest.raises(ValueError, match="control"):
        sbpl_string(bad)


def test_registries_are_hosts() -> None:
    assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]+", h) for h in REGISTRIES)
    for host in ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "github.com", "crates.io"):
        assert host in REGISTRIES
    assert {"proxy.golang.org", "formulae.brew.sh"} <= set(REGISTRIES)


def test_a_pinned_repository_cannot_be_renamed_away_and_costs_no_regex(tmp_path: Path) -> None:
    """`pinned_git` (past the full rules' cost): every parent of it is held, and it adds literal
    rules only, whatever the count: the regexes are what the sandbox compiles at every command."""
    root = tmp_path / "proj"
    git = root / "node_modules" / "pkg" / ".git"
    git.mkdir(parents=True)
    bare = profile_for(_spec(tmp_path, write_roots=(str(root),), user_roots=(str(root),)), tmp_path / "home")
    spec = _spec(tmp_path, write_roots=(str(root),), user_roots=(str(root),), pinned_git=(str(git),))
    profile = profile_for(spec, tmp_path / "home")
    real = os.path.realpath(git)
    assert _verdict(profile, "file-write-unlink", os.path.dirname(real)) == "deny"
    assert _verdict(profile, "file-write-unlink", os.path.dirname(os.path.dirname(real))) == "deny"
    assert _verdict(profile, "file-write*", f"{real}/config") == "deny"  # the name rule
    assert profile.count("(regex") == bare.count("(regex")
    outside = tmp_path / "elsewhere" / ".git"
    with pytest.raises(ValueError, match="pinned"):
        profile_for(_spec(tmp_path, pinned_git=(str(outside),)), tmp_path / "home")


def test_a_write_root_that_became_a_link_is_refused(tmp_path: Path) -> None:
    """A swap the sandbox could not stop (a container's rename, a process outside it) still never
    reaches a profile: the root is refused, and the fence follows no link to a new home."""
    home = tmp_path / "Prismoid"
    tmp, secret = home / "tasks" / "t1" / "tmp", home / "runtime"
    tmp.parent.mkdir(parents=True)
    secret.mkdir()
    tmp.symlink_to(secret)
    with pytest.raises(ValueError, match="a link"):
        profile_for(_spec(tmp_path, write_roots=(str(tmp),), user_roots=()), home)


def test_a_granted_root_must_be_the_real_path_it_was_pinned_to(tmp_path: Path) -> None:
    real, way = tmp_path / "real", tmp_path / "way"
    (real / "notes").mkdir(parents=True)
    way.symlink_to(real)
    notes = str(way / "notes")  # a folder above it moved, the root itself is no link
    with pytest.raises(ValueError, match="a link"):
        profile_for(_spec(tmp_path, write_roots=(notes,), user_roots=(notes,)), tmp_path / "home")


@pytest.mark.parametrize("field", ["write_roots", "read_only_roots"])
def test_no_root_may_be_the_prismoid_home_itself(tmp_path: Path, field: str) -> None:
    home = tmp_path / "Prismoid"
    home.mkdir()
    with pytest.raises(ValueError):
        profile_for(_spec(tmp_path, **{field: (str(home),)}, user_roots=()), home)


def test_every_root_is_held_in_place(tmp_path: Path) -> None:
    """Seatbelt lets a root's own entry go with its `subpath`: named here, it cannot be re-aimed."""
    home = tmp_path / "Prismoid"
    work, ro = home / "tasks" / "t1" / "tmp", tmp_path / "ro"
    work.mkdir(parents=True)
    ro.mkdir()
    profile = profile_for(
        _spec(tmp_path, write_roots=(str(work),), read_only_roots=(str(ro),), user_roots=()), home
    )
    for root in (os.path.realpath(work), os.path.realpath(ro)):
        assert _verdict(profile, "file-write-unlink", root) == "deny", root
    assert _verdict(profile, "file-write-unlink", f"{os.path.realpath(work)}/scratch") == "allow"


@pytest.mark.parametrize(
    "secret",
    [".claude", ".codex", ".pgpass", ".vault-token", ".config/rclone", ".terraform.d", ".m2/settings.xml"],
)
def test_more_credentials_are_never_read(tmp_path: Path, secret: str) -> None:
    home = tmp_path / "me"
    home.mkdir()
    assert f"{home.resolve()}/{secret}" in seatbelt.read_denied(user_home=str(home))


@pytest.mark.parametrize(
    ("path", "runs"),
    [
        ("/p/.claude/settings.json", True),
        ("/p/.claude", True),
        ("/p/.claude/settings.local.json", True),
        ("/p/.claude/hooks/a.sh", True),
        ("/p/.claude/commands/a.md", True),
        ("/p/.mcp.json", True),
        ("/p/.husky", True),
        ("/p/.husky/pre-commit", True),
        ("/p/.mise.toml", True),
        ("/p/.cursor", True),
        ("/p/.cursor/rules/x.md", True),
        ("/p/src/mise.toml", False),
        ("/p/husky/x", False),
    ],
)
def test_what_a_tool_runs_on_opening_a_folder_is_protected(path: str, runs: bool) -> None:
    assert any(re.search(pattern, path) for pattern in PROTECT_WRITE) is runs


def test_a_read_only_root_inside_a_write_root_is_denied_after_every_allow(tmp_path: Path) -> None:
    proj, ref = tmp_path / "proj", tmp_path / "proj" / "ref"
    ref.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    spec = FenceSpec(
        write_roots=(str(proj),),
        read_only_roots=(str(ref),),
        user_roots=(str(proj),),
        proxy_port=4000,
        deny_ports=(),
    )
    lines = profile_for(spec, home, user_home=str(tmp_path)).splitlines()
    deny = f'(deny file-write* (subpath "{ref.resolve()}"))'
    assert lines.index(deny) > lines.index(f'(allow file-write* (subpath "{proj.resolve()}"))')


def test_launchctl_is_refused_after_the_rule_that_lets_commands_run(tmp_path: Path) -> None:
    root, home = tmp_path / "proj", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    spec = FenceSpec(
        write_roots=(str(root),), read_only_roots=(), user_roots=(str(root),), proxy_port=4000, deny_ports=()
    )
    lines = profile_for(spec, home, user_home=str(tmp_path)).splitlines()
    assert lines.index("(allow process-exec)") < lines.index(seatbelt.NO_LAUNCHCTL)
