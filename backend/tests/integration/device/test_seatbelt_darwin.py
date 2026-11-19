# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Seatbelt profile against the real sandbox.

Each test runs `sandbox-exec -p <profile> /bin/bash --noprofile --norc -c …` with a clean
environment (no proxy variables) and asserts on what the kernel allowed. A refusal is asserted as
EPERM ("Operation not permitted"), never as a bare failure, so a missing file or a closed port
cannot pass for the fence.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from prismoid.device import seatbelt
from prismoid.device.seatbelt import FenceSpec, cache_root, profile_for
from tests.support.markers import darwin_only

pytestmark = darwin_only

USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
EPERM = "Operation not permitted"
# The probe a sandboxed python runs to dial host:port, printing OPEN or the errno's name.
DIAL = (
    "import errno,socket,sys\n"
    "try: socket.create_connection((sys.argv[1], int(sys.argv[2])), 3); print('OPEN')\n"
    "except OSError as e: print(errno.errorcode.get(e.errno, e))"
)
DIAL_UNIX = (
    "import errno,socket,sys\n"
    "try: socket.socket(socket.AF_UNIX).connect(sys.argv[1]); print('OPEN')\n"
    "except OSError as e: print(errno.errorcode.get(e.errno, e))"
)


def _env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "HOME": str(USER_HOME), "LANG": "C"}


def _sandboxed(profile: str, script: str) -> list[str]:
    return ["sandbox-exec", "-p", profile, "/bin/bash", "--noprofile", "--norc", "-c", script]


def run(profile: str, script: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _sandboxed(profile, script), env=_env(), cwd=cwd, capture_output=True, text=True, timeout=60
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _listener(port: int) -> Generator[None]:
    """A loopback TCP listener outside the sandbox that accepts and hangs up."""
    server = socket.create_server(("127.0.0.1", port))
    server.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError, OSError):
                conn, _ = server.accept()
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        server.close()


def _dial(profile: str, port: int, host: str = "127.0.0.1") -> str:
    return run(profile, f'python3 -c "{DIAL}" {host} {port}').stdout.strip()


def _dial_unix(profile: str, path: str) -> str:
    return run(profile, f"python3 -c \"{DIAL_UNIX}\" '{path}'").stdout.strip()


current_home: Path = USER_HOME  # rebound per test, below


@pytest.fixture(autouse=True)
def per_test_home(tmp_path: Path) -> Generator[None]:
    """Each test's user home is its own tmp_path, so tmp roots count as folders in the home (a user
    root must be in the home or on an external disk). Tests of the real home pass `user_home`."""
    global current_home
    current_home = Path(os.path.realpath(tmp_path))
    try:
        yield
    finally:
        current_home = USER_HOME


def _real_home_profile(app_home: Path, **kw: Any) -> str:
    """A profile on the real home, for the tests that probe its secrets: a Prismoid-owned root only."""
    work = app_home / "tasks" / "t1" / "work"
    work.mkdir(parents=True, exist_ok=True)
    return _profile(work, app_home, user_home=USER_HOME, user_roots=(), **kw)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


@pytest.fixture
def app_home(tmp_path: Path) -> Path:
    # A space and a quote: the escape helper's whole job, on the real parser.
    home = tmp_path / 'Application Support "Prismoid"'
    home.mkdir()
    (home / "helper.key").write_text("secret")
    return home


def _profile(root: Path, home: Path, user_home: Path | None = None, **kw: Any) -> str:
    writes = kw.pop("write_roots", (str(root),))
    spec = FenceSpec(
        write_roots=writes,
        user_roots=kw.pop("user_roots", writes),  # granted by a person, unless told otherwise
        read_only_roots=kw.pop("read_only_roots", ()),
        proxy_port=kw.pop("proxy_port", _free_port()),
        deny_ports=kw.pop("deny_ports", ()),
        **kw,
    )
    return profile_for(spec, home, user_home=str(user_home or current_home))


# Git inside the fence, deterministic: no global config (signing, insteadOf), a writable TMPDIR.
GIT_ENV = "export GIT_CONFIG_GLOBAL=/dev/null GIT_AUTHOR_NAME=a GIT_AUTHOR_EMAIL=a@b GIT_COMMITTER_NAME=a GIT_COMMITTER_EMAIL=a@b"


def _git(cwd: Path, *args: str) -> None:
    """Git outside the fence, to set a scene up."""
    env = {
        **_env(),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main", ".")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _refused(profile: str, script: str) -> None:
    out = run(profile, script)
    assert out.returncode != 0 and EPERM in out.stderr, (script, out.stderr)


def test_ssh_known_hosts_unreadable(root: Path, app_home: Path) -> None:
    if not (USER_HOME / ".ssh").exists():
        pytest.skip("no ~/.ssh on this Mac")
    out = run(_real_home_profile(app_home), "cat ~/.ssh/known_hosts; ls ~/.ssh")
    assert out.returncode != 0
    assert out.stdout == ""
    assert EPERM in out.stderr


def test_write_inside_root(root: Path, app_home: Path) -> None:
    out = run(
        _profile(root, app_home), f"echo x > '{root}/a' && mkdir -p '{root}/src' && echo y > '{root}/src/b'"
    )
    assert out.returncode == 0, out.stderr
    assert (root / "a").read_text() == "x\n"
    assert (root / "src" / "b").read_text() == "y\n"


def test_write_outside_root_refused(root: Path, app_home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    out = run(_profile(root, app_home), f"echo x > '{outside}/a'")
    assert out.returncode != 0 and EPERM in out.stderr
    assert not (outside / "a").exists()


def test_git_hook_write_refused(tmp_path: Path, app_home: Path) -> None:
    root = _repo(tmp_path / "work")
    git = root / ".git"
    profile = _profile(root, app_home, protected_git=(str(git),))
    _refused(profile, f"echo x > '{git}/hooks/pre-commit'")
    assert not (git / "hooks" / "pre-commit").exists()
    config = (git / "config").read_text()
    for script in (
        f"echo x >> '{git}/config'",
        f"cd '{root}' && {GIT_ENV} && git config core.fsmonitor /tmp/x",
        # commondir would point git at a planted config; info/exclude would hide the plant.
        f"echo '../planted' > '{git}/commondir'",
        f"echo planted >> '{git}/info/exclude'",
        f"mkdir '{git}/worktrees'",
        f"mv '{git}' '{root}/old'",
        f"echo x > '{root}/.zshrc'",
    ):
        _refused(profile, script)
    assert (git / "config").read_text() == config and not (git / "commondir").exists()
    # Everyday git still works in place.
    script = (
        f"cd '{root}' && {GIT_ENV} && echo b > b.txt && git add b.txt && git status --short"
        " && git commit -q -m b && git checkout -q -b feature && git log --oneline | wc -l"
    )
    out = run(profile, script)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["A", "b.txt", "2"]


def test_cherry_pick_and_stash_work_but_rebase_state_is_refused(tmp_path: Path, app_home: Path) -> None:
    root = _repo(tmp_path / "work")
    _git(root, "checkout", "-q", "-b", "f")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "b")
    _git(root, "checkout", "-q", "-")
    profile = _profile(root, app_home, protected_git=(str(root / ".git"),))
    script = (
        f"cd '{root}' && {GIT_ENV} && git cherry-pick f >/dev/null && git reset -q --hard HEAD~1"
        " && git merge -q --no-edit --no-ff f && echo c >> a.txt && git stash -q && git stash pop -q"
        " && git gc -q && git log --oneline | wc -l"
    )
    out = run(profile, script)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["3"]
    # A rebase's todo list runs `exec` lines on a later `--continue`: never planted.
    _refused(profile, f"mkdir '{root}/.git/rebase-merge'")
    _refused(profile, f"cd '{root}' && {GIT_ENV} && git checkout -q f && git rebase -q main")
    assert not (root / ".git" / "rebase-merge").exists()


def test_new_repositories_only_in_prismoids_roots(tmp_path: Path, app_home: Path) -> None:
    root = _repo(tmp_path / "work")
    empty = tmp_path / "plain"
    empty.mkdir()
    scratch = app_home / "scratch" / "s1"
    tmp = app_home / "tasks" / "t1" / "tmp"
    for folder in (scratch, tmp):
        folder.mkdir(parents=True)
    profile = _profile(
        root,
        app_home,
        write_roots=(str(root), str(empty), str(scratch), str(tmp)),
        user_roots=(str(root), str(empty)),
        protected_git=(str(root / ".git"),),
    )
    # In Prismoid's own roots, git init, config, hooks and clone are the agent's own.
    script = (
        f"export TMPDIR='{tmp}' && {GIT_ENV} && cd '{scratch}'"
        " && git init -q fresh && git -C fresh config user.name me && echo h > fresh/.git/hooks/h"
        f" && git clone -q 'file://{root}' clone && git clone -q 'file://{root}' \"$TMPDIR/c2\""
        ' && git -C "$TMPDIR/c2" log --oneline | wc -l'
    )
    out = run(profile, script)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1"]
    assert (scratch / "clone" / "a.txt").exists() and (tmp / "c2" / ".git" / "config").exists()
    # In a user's folder, no new git dir or pointer: nested in their repo, or where none was.
    for script in (
        f"cd '{root}' && {GIT_ENV} && git init -q sub",
        f"cd '{root}' && {GIT_ENV} && git clone -q 'file://{root}' clone",
        f"cd '{empty}' && {GIT_ENV} && git init -q .",
        f"mkdir -p '{root}/sub2' && echo 'gitdir: {scratch}/fresh/.git' > '{root}/sub2/.git'",
        f"mkdir -p '{empty}/g/hooks' && mv '{empty}/g' '{empty}/.git'",
    ):
        _refused(profile, script)
    assert not (root / "sub" / ".git").exists() and not (empty / ".git").exists()
    assert not (root / "sub2" / ".git").exists()


def test_a_worktree_pointer_cannot_be_repointed(tmp_path: Path, app_home: Path) -> None:
    main = _repo(tmp_path / "main")
    _git(main, "worktree", "add", "-q", str(tmp_path / "wt"))
    wt = tmp_path / "wt"
    pointer = (wt / ".git").read_text()
    profile = _profile(wt, app_home, protected_git=(str(wt / ".git"),))
    _refused(profile, f"echo 'gitdir: {tmp_path}/planted' > '{wt}/.git'")
    _refused(profile, f"rm '{wt}/.git'")
    assert (wt / ".git").read_text() == pointer
    assert run(profile, f"echo x > '{wt}/b.txt'").returncode == 0


def test_symlink_out_of_root_refused(root: Path, app_home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "out").symlink_to(outside)
    (root / "home-key").symlink_to(app_home / "helper.key")
    profile = _profile(root, app_home)
    out = run(profile, f"echo x > '{root}/out/a'")
    assert out.returncode != 0 and EPERM in out.stderr
    out = run(profile, f"cat '{root}/home-key'")
    assert out.returncode != 0 and EPERM in out.stderr and "secret" not in out.stdout


def test_prismoid_home_denied_but_frame_folders_reopened(app_home: Path) -> None:
    task = app_home / "tasks" / "t1"
    (task / "work").mkdir(parents=True)
    (task / "task" / "inputs").mkdir(parents=True)
    (task / "task" / "inputs" / "brief.txt").write_text("brief\n")
    (task / "task" / "answers.json").write_text("{}")
    profile = _profile(
        task / "work", app_home, user_roots=(), read_only_roots=(str(task / "task" / "inputs"),)
    )
    script = (
        f"cd '{task}/work' && echo x > a && cat a && cat '{task}/task/inputs/brief.txt'"
        " && python3 -c 'import os; print(os.path.realpath(\".\"))'"
    )
    out = run(profile, script)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[:2] == ["x", "brief"]
    # The folders above a reopened root can be looked up (node's realpath lstats each), not listed.
    out = run(profile, f"stat -f %N '{app_home}' '{app_home}/tasks' '{task}'")
    assert out.returncode == 0, out.stderr
    for script in (
        f"cat '{app_home}/helper.key'",
        f"ls '{app_home}/tasks'",
        f"cat '{task}/task/answers.json'",
        f"echo x > '{task}/task/inputs/brief.txt'",
    ):
        out = run(profile, script)
        assert out.returncode != 0 and EPERM in out.stderr, script
    assert (task / "task" / "inputs" / "brief.txt").read_text() == "brief\n"


def test_root_containing_the_prismoid_home_keeps_it_closed(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    home = proj / "Library" / "Application Support" / "Prismoid"
    home.mkdir(parents=True)
    (home / "helper.key").write_text("secret")
    profile = _profile(proj, home)
    assert run(profile, f"echo x > '{proj}/a'").returncode == 0
    for script in (
        f"echo x > '{home}/helper.key'",
        f"cat '{home}/helper.key'",
        f"mv '{home}' '{proj}/b'",
        # Renaming a parent would carry the home out from under its rule.
        f"mv '{proj}/Library' '{proj}/L2' && cat '{proj}/L2/Application Support/Prismoid/helper.key'",
    ):
        out = run(profile, script)
        assert out.returncode != 0 and EPERM in out.stderr, script
    assert (home / "helper.key").read_text() == "secret"


def test_library_is_never_a_root_whatever_its_case(tmp_path: Path, app_home: Path) -> None:
    for spelling in ("Library", "library", "LIBRARY/LaunchAgents"):
        with pytest.raises(ValueError, match="Library"):
            _profile(tmp_path, app_home, user_home=USER_HOME, write_roots=(str(USER_HOME / spelling),))
    # Seatbelt matches paths ignoring case too: the keychain deny holds against ~/LIBRARY/....
    # Probed by opening for append and writing nothing, so a broken fence changes no real file.
    targets = sorted((USER_HOME / "Library" / "Keychains").glob("*.keychain-db"))[:1]
    if not targets:
        pytest.skip("no keychain file on this Mac to probe")
    shouted = str(targets[0]).replace("/Library/Keychains/", "/LIBRARY/KEYCHAINS/")
    out = run(_real_home_profile(app_home), f"cat '{shouted}' >/dev/null")
    assert out.returncode != 0 and EPERM in out.stderr


def test_the_users_own_caches_are_read_only(root: Path, app_home: Path) -> None:
    caches = [".npm", ".cache", ".cargo", "go/pkg", ".local/share/uv", "Library/Caches"]
    files = [next((f for f in sorted((USER_HOME / c).glob("*")) if f.is_file()), None) for c in caches]
    files = [f for f in files if f is not None]
    if not files:
        pytest.skip("no cache file on this Mac to probe")
    mine, theirs = cache_root(f"device-probe-a{os.getpid()}"), cache_root(f"device-probe-b{os.getpid()}")
    profile = _real_home_profile(app_home, cache_root=mine)
    for target in files:  # append-open, nothing written
        out = run(profile, f"python3 -c \"open('{target}', 'a').close()\"")
        assert out.returncode != 0 and "Operation not permitted" in out.stderr, target
    try:
        out = run(profile, f"mkdir -p '{mine}/cargo' && echo x > '{mine}/cargo/f'")
        assert out.returncode == 0, out.stderr
        # Another frame's cache is not this frame's to plant in.
        os.makedirs(theirs)
        _refused(profile, f"echo x > '{theirs}/f'")
        _refused(_real_home_profile(app_home, cache_root=theirs), f"echo x >> '{mine}/cargo/f'")
    finally:
        shutil.rmtree(mine, ignore_errors=True)
        shutil.rmtree(theirs, ignore_errors=True)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A home of our own: links and dotfiles planted without touching the real one."""
    home = Path(os.path.realpath(tmp_path)) / "user"
    for folder in (
        ".ssh",
        "dotfiles",
        "Dropbox/keys",
        "code",
        "Library/LaunchAgents",
        "Library/Application Support/Code/User",
        "Library/Group Containers/2BUA8C4S2C.com.1password",
    ):
        (home / folder).mkdir(parents=True)
    (home / "Dropbox" / "keys" / "id_ed25519").write_text("key")
    (home / ".ssh" / "id_ed25519").symlink_to(home / "Dropbox" / "keys" / "id_ed25519")
    (home / "dotfiles" / "zshrc").write_text("")
    (home / ".zshrc").symlink_to(home / "dotfiles" / "zshrc")
    (home / "Library" / "Group Containers" / "2BUA8C4S2C.com.1password" / "vault").write_text("v")
    return home


def test_symlinked_secrets_and_rc_files_hold_through_the_link(fake_home: Path, app_home: Path) -> None:
    profile = _profile(
        fake_home / "code",
        app_home,
        user_home=fake_home,
        write_roots=(str(fake_home / "code"), str(fake_home / "dotfiles")),
    )
    for script in (
        f"cat '{fake_home}/.ssh/id_ed25519'",
        f"cat '{fake_home}/Dropbox/keys/id_ed25519'",
        f"echo x >> '{fake_home}/dotfiles/zshrc'",
        f"echo x >> '{fake_home}/.zshrc'",
        f"cat '{fake_home}/Library/Group Containers/2BUA8C4S2C.com.1password/vault'",
    ):
        _refused(profile, script)
    assert (fake_home / "dotfiles" / "zshrc").read_text() == ""
    assert run(profile, f"echo x > '{fake_home}/dotfiles/vimrc'").returncode == 0


def test_home_protection_holds_inside_a_write_root(fake_home: Path, app_home: Path) -> None:
    # `~/bin` may be granted (it is no dot-folder), yet it is on PATH: the protect list still holds.
    bin_dir = fake_home / "bin"
    bin_dir.mkdir()
    profile = _profile(bin_dir, app_home, user_home=fake_home)
    _refused(profile, f"echo x > '{bin_dir}/git'")
    # ~/Library itself can no longer be granted; its launch agents and editor settings stay closed.
    with pytest.raises(ValueError, match="Library"):
        _profile(fake_home / "Library", app_home, user_home=fake_home)
    profile = _profile(fake_home / "code", app_home, user_home=fake_home)
    _refused(profile, f"echo x > '{fake_home}/Library/LaunchAgents/evil.plist'")
    _refused(profile, f"echo '{{}}' > '{fake_home}/Library/Application Support/Code/User/settings.json'")


def test_keychain_items_unreachable(root: Path, app_home: Path, tmp_path: Path) -> None:
    # A throwaway keychain the fence CAN read: its items still need securityd, which stays closed.
    keychain = str(tmp_path / "probe.keychain")
    for args in (
        ["create-keychain", "-p", "pw", keychain],
        ["unlock-keychain", "-p", "pw", keychain],
        ["add-generic-password", "-a", "me", "-s", "prismoid-fence-probe", "-w", "hunter2", "-A", keychain],
    ):
        subprocess.run(["security", *args], check=True, capture_output=True)
    lookup = ["security", "find-generic-password", "-s", "prismoid-fence-probe", "-w", keychain]
    try:
        # The same lookup outside the fence finds the item, so the refusal inside is the fence's.
        assert subprocess.run(lookup, capture_output=True, text=True).stdout.strip() == "hunter2"
        out = run(_profile(root, app_home), " ".join(f"'{a}'" for a in lookup))
        assert out.returncode != 0 and "hunter2" not in out.stdout
        assert "could not be found in the keychain" in out.stderr, out.stderr
    finally:
        subprocess.run(["security", "delete-keychain", keychain], capture_output=True)


def test_nothing_launches_outside_the_fence(root: Path, app_home: Path) -> None:
    # LaunchServices is closed: an app launched through it would run outside the sandbox. The app
    # is on disk, so "unable to find" is the closed lookup, not a missing app.
    if not Path("/System/Applications/Calculator.app").exists():
        pytest.skip("no Calculator.app to fail to launch")
    out = run(_profile(root, app_home), "open -g -j -a Calculator")
    assert out.returncode != 0
    assert "Unable to find application named 'Calculator'" in out.stderr, out.stderr


def test_curl_without_proxy_fails(root: Path, app_home: Path) -> None:
    profile = _profile(root, app_home)
    out = run(profile, "curl -s -m 10 https://example.com")
    assert out.returncode != 0 and out.stdout == ""
    # Not a DNS accident: dialling a public address directly is refused by the sandbox itself.
    assert _dial(profile, 443, "1.1.1.1") == "EPERM"


def test_loopback_server_reachable_from_second_command(root: Path, app_home: Path) -> None:
    profile = _profile(root, app_home)
    port = _free_port()
    server = subprocess.Popen(
        _sandboxed(profile, f"exec python3 -m http.server {port} --bind 127.0.0.1"),
        env=_env(),
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
                break
            time.sleep(0.1)
        out = run(profile, f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/")
        assert out.stdout == "200", out.stderr
        out = run(profile, f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/")
        assert out.stdout == "200", out.stderr
    finally:
        server.terminate()
        server.wait(10)


def test_proxy_port_reachable_never_squatted(root: Path, app_home: Path) -> None:
    port = _free_port()
    profile = _profile(root, app_home, proxy_port=port)
    with _listener(port):
        assert _dial(profile, port) == "OPEN"
    out = run(profile, f"python3 -c \"import socket; socket.create_server(('127.0.0.1', {port}))\"")
    assert out.returncode != 0 and "PermissionError: [Errno 1] Operation not permitted" in out.stderr


def test_deny_port_refused(root: Path, app_home: Path) -> None:
    denied, other = _free_port(), _free_port()
    profile = _profile(root, app_home, deny_ports=(denied,))
    with _listener(denied), _listener(other):
        assert _dial(profile, other) == "OPEN"
        assert _dial(profile, denied) == "EPERM"
        assert _dial(profile, denied, "localhost") == "EPERM"
        assert _dial(profile, denied, "::1") == "EPERM"
    # Nor can a command stand up its own server on a Prismoid port while Prismoid is down.
    bind = f"python3 -c \"import socket; socket.create_server(('127.0.0.1', {denied}))\""
    out = run(profile, bind)
    assert out.returncode != 0 and "PermissionError" in out.stderr


def test_docker_socket_reachable_only_when_granted(root: Path) -> None:
    # AF_UNIX paths are capped at 104 bytes, so this Prismoid home is a short one under /tmp.
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        home = Path(os.path.realpath(short)) / "Prismoid"
        (home / "run").mkdir(parents=True)
        sock = str(home / "run" / "docker.sock")
        other = str(home / "run" / "other.sock")
        servers = [socket.socket(socket.AF_UNIX), socket.socket(socket.AF_UNIX)]
        for server, path in zip(servers, (sock, other), strict=True):
            server.bind(path)
            server.listen()
        try:
            without = _profile(root, home)
            granted = _profile(root, home, docker_socket=sock)
            assert _dial_unix(without, sock) == "EPERM"
            assert _dial_unix(granted, sock) == "OPEN"
            assert _dial_unix(granted, other) == "EPERM"
        finally:
            for server in servers:
                server.close()


def test_toolchains_run(root: Path, app_home: Path) -> None:
    """The system allowances are enough for everyday tools to start."""
    script = (
        f"export TMPDIR='{root}' && id -un && python3 -c 'import multiprocessing as m; print(m.Lock())' && git --version"
        " && echo out > /dev/stdout && echo err > /dev/stderr && cat <(echo sub) && echo pipe > >(cat)"
    )
    out = run(_profile(root, app_home), script)
    assert out.returncode == 0, out.stderr
    assert out.stderr == "err\n" and "sub" in out.stdout


def test_prismoids_own_config_and_credentials_unreadable(fake_home: Path, app_home: Path) -> None:
    """`~/.prismoid` holds the model keys and the CLI's session: not in the Prismoid home, so listed."""
    config = fake_home / ".prismoid"
    config.mkdir()
    (config / "config").write_text("key")
    (config / "credentials").write_text("session")
    profile = _profile(fake_home / "code", app_home, user_home=fake_home)
    for script in (f"cat '{config}/config'", f"cat '{config}/credentials'", f"ls '{config}'"):
        out = run(profile, script)
        assert out.returncode != 0 and EPERM in out.stderr, script


def _frame_in(app_home: Path) -> Path:
    """A frame's folder in the Prismoid home, as `fence_for` hands its own roots to the profile."""
    base = app_home / "tasks" / "t1"
    for sub in ("work", "tmp", "out"):
        (base / sub).mkdir(parents=True)
    (base / "tmp" / "scratch").write_text("mine")
    return base


def test_a_frames_own_root_cannot_be_swapped_for_a_link(app_home: Path) -> None:
    """The fence is written again for each command: a root re-aimed by one command would be the
    next one's way into what the first could not touch."""
    base = _frame_in(app_home)
    own = (str(base / "work"), str(base / "tmp"), str(base / "out"))
    profile = _profile(base / "work", app_home, write_roots=own, user_roots=())
    tmp = base / "tmp"
    for script in (
        f"rm -rf '{tmp}' && ln -s '{app_home}' '{tmp}'",
        f"mv '{tmp}' '{base}/work/moved'",
        f"rmdir '{tmp}'",
    ):
        out = run(profile, f"rm -f '{tmp}/scratch'; {script}")
        assert out.returncode != 0 and EPERM in out.stderr, script
        assert tmp.is_dir() and not tmp.is_symlink(), script
    # Its contents are still the command's own to clear.
    assert not (tmp / "scratch").exists()


def test_a_granted_folder_cannot_be_swapped_for_a_link(tmp_path: Path, app_home: Path) -> None:
    notes, documents = tmp_path / "notes", tmp_path / "Documents"
    notes.mkdir()
    documents.mkdir()
    (notes / "a").write_text("mine")
    profile = _profile(notes, app_home)
    out = run(profile, f"rm -rf '{notes}' && ln -s '{documents}' '{notes}'")
    assert out.returncode != 0 and EPERM in out.stderr
    out = run(profile, f"mv '{notes}' '{notes}.old' && ln -s '{documents}' '{notes}'")
    assert out.returncode != 0 and EPERM in out.stderr
    assert notes.is_dir() and not notes.is_symlink()


def test_a_read_only_root_inside_a_write_root_cannot_be_swapped(tmp_path: Path, app_home: Path) -> None:
    proj, ref = tmp_path / "proj", tmp_path / "proj" / "ref"
    ref.mkdir(parents=True)
    profile = _profile(proj, app_home, read_only_roots=(str(ref),))
    out = run(profile, f"mv '{ref}' '{proj}/ref2' && ln -s '{app_home}' '{ref}'")
    assert out.returncode != 0 and EPERM in out.stderr
    assert ref.is_dir() and not ref.is_symlink()


def test_a_read_only_root_inside_a_write_root_cannot_be_written(tmp_path: Path, app_home: Path) -> None:
    proj, ref = tmp_path / "proj", tmp_path / "proj" / "ref"
    (ref / "sub").mkdir(parents=True)
    (ref / "kept").write_text("theirs")
    profile = _profile(proj, app_home, read_only_roots=(str(ref),))
    for script in (
        f"echo new > '{ref}/new'",
        f"echo more >> '{ref}/kept'",
        f"mkdir '{ref}/made'",
        f"echo deep > '{ref}/sub/new'",
        f"rm '{ref}/kept'",
    ):
        out = run(profile, script)
        assert out.returncode != 0 and EPERM in out.stderr, script
    assert run(profile, f"cat '{ref}/kept'").stdout == "theirs"
    assert run(profile, f"echo fine > '{proj}/beside'").returncode == 0


def test_a_cache_root_cannot_be_swapped_for_another_frames(tmp_path: Path, app_home: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    mine, theirs = (
        Path(cache_root("frame1", user_home=str(tmp_path))),
        Path(cache_root("frame2", user_home=str(tmp_path))),
    )
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)
    profile = _profile(root, app_home, cache_root=str(mine))
    out = run(profile, f"rm -rf '{mine}' && ln -s '{theirs}' '{mine}'")
    assert out.returncode != 0 and EPERM in out.stderr
    assert mine.is_dir() and not mine.is_symlink()


# `link(2)` itself: `ln` stats its source first, and on a secret the read deny answers that.
HARD_LINK = (
    "import errno,os,sys\n"
    "try: os.link(sys.argv[1], sys.argv[2]); print('LINKED')\n"
    "except OSError as e: print(errno.errorcode.get(e.errno, e))"
)


def test_a_secret_cannot_be_hard_linked_into_a_root(fake_home: Path, app_home: Path) -> None:
    root = fake_home / "proj"
    root.mkdir()
    (fake_home / ".netrc").write_text("machine x password y")
    (app_home / "helper.key").write_text("key")
    profile = _profile(root, app_home, fake_home)
    for source, name in (((fake_home / ".netrc"), "one"), ((app_home / "helper.key"), "two")):
        out = run(profile, f"python3 -c {shlex.quote(HARD_LINK)} '{source}' '{root / name}'")
        assert out.stdout.strip() == "EPERM", out.stderr
        assert not (root / name).exists()


def test_what_is_read_only_cannot_be_hard_linked_into_a_root(
    root: Path, app_home: Path, tmp_path: Path
) -> None:
    """Readable, and so past the read deny: `link(2)` is what refuses it, or a write through the link
    would reach a file outside every root."""
    outside = tmp_path / "outside.txt"
    outside.write_text("theirs")
    out = run(_profile(root, app_home), f"python3 -c {shlex.quote(HARD_LINK)} '{outside}' '{root}/held'")
    assert out.stdout.strip() == "EPERM", out.stderr
    assert not (root / "held").exists()


def test_git_cannot_switch_what_the_fence_protects_and_does_not_say_so(
    tmp_path: Path, app_home: Path
) -> None:
    """A limit, pinned: the law and the known limits tell the agent, and `git` exits 0."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".envrc").write_text("one\n")

    def git(*args: str) -> None:
        who = ("-c", "user.name=t", "-c", "user.email=t@t")
        subprocess.run(["git", *who, *args], cwd=repo, env=_env(), check=True)

    for args in (
        ("init", "-q", "-b", "one"),
        ("add", "-A"),
        ("commit", "-qm", "1"),
        ("switch", "-qc", "two"),
    ):
        git(*args)
    (repo / ".envrc").write_text("two\n")
    git("commit", "-qam", "2")
    git("switch", "-q", "one")
    out = run(_profile(repo, app_home, protected_git=(str(repo / ".git"),)), "git switch two", cwd=repo)
    assert out.returncode == 0 and "unable to unlink" in out.stderr
    assert (repo / ".envrc").read_text() == "one\n"


def test_a_unix_socket_cannot_be_bound_even_in_a_frames_own_folder(root: Path, app_home: Path) -> None:
    bind = "import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])"
    out = run(_profile(root, app_home), f"python3 -c {shlex.quote(bind)} '{root}/s'")
    assert out.returncode != 0 and "Operation not permitted" in out.stderr


def test_launchd_takes_no_job_from_the_fence(root: Path, app_home: Path, tmp_path: Path) -> None:
    """What launchd starts runs outside the sandbox, so a job from inside it would end the fence. The
    profile's refusal of `launchctl` itself is taken out here: this is launchd's own answer, and what
    holds against a program that speaks its protocol directly."""
    label = f"dev.prismoid.fence-test.{os.getpid()}"
    marker = tmp_path / "escaped"
    plist = root / "job.plist"
    plist.write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        f"<key>Label</key><string>{label}</string><key>ProgramArguments</key><array>"
        f"<string>/usr/bin/touch</string><string>{marker}</string></array>"
        "<key>RunAtLoad</key><true/></dict></plist>"
    )
    profile = _profile(root, app_home).replace(seatbelt.NO_LAUNCHCTL, "")
    try:
        assert run(profile, f"launchctl submit -l {label} -- /usr/bin/touch '{marker}'").returncode != 0
        assert run(profile, f"launchctl bootstrap gui/{os.getuid()} '{plist}'").returncode != 0
        run(profile, f"launchctl load '{plist}'")
        listed = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True)
        assert listed.returncode != 0, "launchd took a job from inside the fence"
        assert not marker.exists()
    finally:
        subprocess.run(["launchctl", "remove", label], capture_output=True)


def _disabled(label: str) -> bool:
    said = subprocess.run(
        ["launchctl", "print-disabled", f"gui/{os.getuid()}"], capture_output=True, text=True
    )
    return f'"{label}" => disabled' in said.stdout


def test_launchctl_does_not_run_in_the_fence(root: Path, app_home: Path) -> None:
    """`disable` is kept by launchd past every reboot, and needs no job of ours: a person's agent
    would stay off. The label here is nobody's."""
    label = f"dev.prismoid.fence-test.disable.{os.getpid()}"
    service = f"gui/{os.getuid()}/{label}"
    try:
        out = run(_profile(root, app_home), f"launchctl disable {service}")
        assert out.returncode != 0 and EPERM in out.stderr
        assert not _disabled(label)
    finally:
        subprocess.run(["launchctl", "enable", service], capture_output=True)


@pytest.mark.parametrize("name", [".mcp.json", ".mise.toml", ".husky/pre-commit", ".cursor/rules"])
def test_what_runs_code_later_is_refused_in_a_write_root(root: Path, app_home: Path, name: str) -> None:
    out = run(
        _profile(root, app_home), f"mkdir -p \"$(dirname '{root / name}')\" && echo x > '{root / name}'"
    )
    assert out.returncode != 0 and EPERM in out.stderr


def test_a_projects_claude_settings_are_refused(root: Path, app_home: Path) -> None:
    (root / ".claude").mkdir()
    profile = _profile(root, app_home)
    (root / ".claude" / "hooks").mkdir()
    for name in ("settings.json", "settings.local.json", "hooks/a.sh"):
        out = run(profile, f"echo x > '{root}/.claude/{name}'")
        assert out.returncode != 0 and EPERM in out.stderr
