# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Fenced commands on the device, against the real sandbox.

Each test runs a frame's command through `exec.run` (or the helper's route) under the frame's own
Seatbelt profile and its own proxy credential. Frames are destroyed after each test, which also
removes their cache folder under `~/Library/Caches/Prismoid`.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import logging
import os
import pwd
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from prismoid.conf import settings
from prismoid.device import exec as device_exec
from prismoid.device import files
from prismoid.device.egress import DeviceEgress
from prismoid.device.fence_data import CACHE_ENV
from prismoid.device.frames import DeviceFrameState, FrameRegistry, worktree_git
from prismoid.device.helper import KEY_HEADER
from prismoid.device.seatbelt import cache_root
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import ExecIn, FileIn, FolderGrant, FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.markers import darwin_only

USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
EPERM = "Operation not permitted"


class Device:
    def __init__(self, home: Path) -> None:
        self.registry = FrameRegistry(home)
        self.proxy = DeviceEgress(self.registry.frames)

    def frame(self, *grants: FolderGrant, allow: list[str] | None = None) -> DeviceFrameState:
        spec = FrameSpecIn(
            task_id=f"b3-{uuid.uuid4().hex[:8]}",
            kind=FrameKind.MACHINE,
            grants=list(grants),
            allow_domains=allow or [],
        )
        return self.registry.create(spec)

    async def run(self, frame: DeviceFrameState, command: str, limit: float = 30, system: bool = False):
        req = ExecIn(exec_id=uuid.uuid4().hex, command=command, timeout=limit, system=system)
        return await device_exec.run(frame, req, proxy=self.proxy)


@pytest.fixture
async def device(tmp_path: Path) -> AsyncIterator[Device]:
    d = Device(tmp_path / "home")
    await d.proxy.serve()
    try:
        yield d
    finally:
        for ref in list(d.registry.frames):
            d.registry.destroy(ref)
        await d.proxy.close()


@pytest.fixture
def user_folder() -> Iterator[Path]:
    """A folder in the user's real home: a grant must be in the home (or on an external disk)."""
    path = Path(tempfile.mkdtemp(prefix="prismoid-b3-", dir=USER_HOME))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _pgrep(pattern: str) -> int:
    proc = await asyncio.create_subprocess_exec("pgrep", "-f", pattern, stdout=asyncio.subprocess.DEVNULL)
    return await proc.wait()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_api_port_denied_in_fence(tmp_path: Path) -> None:
    frame = FrameRegistry(tmp_path / "home").create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE))
    spec = device_exec.fence_for(frame, system=False, proxy_port=40123, deny_ports=(40124,))
    assert spec.deny_ports == (settings.local.api_port, 40124)
    assert spec.proxy_port == 40123 and spec.cache_root == cache_root(frame.ref)


def test_fence_roots_per_owner(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    ro = tmp_path / "ro"
    ro.mkdir()
    grants = [FolderGrant(path=str(proj), mode="rw"), FolderGrant(path=str(ro), mode="ro")]
    frame = FrameRegistry(tmp_path / "home").create(
        FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=grants)
    )
    base = Path(frame.paths.task).parent
    agent = device_exec.fence_for(frame, system=False, proxy_port=40123)
    real = str(proj.resolve())
    assert set(agent.write_roots) == {real, frame.paths.out, str(base / "tmp")}
    assert agent.user_roots == (real,)
    assert agent.protected_git == (f"{real}/.git",)
    p = frame.paths
    gitconfig = str(base / "gitconfig")
    assert set(agent.read_only_roots) == {
        str(ro.resolve()),
        p.task,
        p.artifacts,
        p.skills,
        p.verify,
        gitconfig,
        str(Path(frame.home) / "runtime"),
    }
    system = device_exec.fence_for(frame, system=True, proxy_port=40123)
    assert set(system.write_roots) == {*agent.write_roots, p.artifacts}
    assert p.artifacts not in system.read_only_roots
    runtime = str(Path(frame.home) / "runtime")
    assert runtime in system.read_only_roots and runtime in agent.read_only_roots
    assert runtime not in system.write_roots and runtime not in agent.write_roots


@darwin_only
async def test_exec_runs_in_work_and_returns_exit(device: Device) -> None:
    frame = device.frame()
    out = await device.run(frame, "pwd; echo err >&2; exit 3")
    assert (out.exit_code, out.stdout.strip(), out.stderr, out.truncated) == (
        3,
        frame.paths.work,
        "err\n",
        False,
    )
    out = await device.run(frame, "pwd", system=True)
    assert out.stdout.strip() == frame.paths.out
    assert frame.journal.interrupted() is None


@darwin_only
async def test_a_command_that_cannot_start_is_finished_in_the_journal(device: Device) -> None:
    frame = device.frame()
    out = await device.run(frame, "echo a\x00b")
    assert out.exit_code == 126 and "could not start" in out.stderr
    assert frame.journal.interrupted() is None


@darwin_only
async def test_a_finished_command_keeps_its_result_on_a_full_disk(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = device.frame()

    def full() -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(frame.journal, "heard", full)
    out = await device.run(frame, "echo done")
    assert (out.exit_code, out.stdout) == (0, "done\n")


def _cache_of(frame: DeviceFrameState) -> Path:
    cache = Path(cache_root(frame.ref))
    cache.mkdir(parents=True, exist_ok=True)
    return cache


@darwin_only
async def test_a_link_in_a_frames_cache_makes_nothing_at_its_target(device: Device, tmp_path: Path) -> None:
    """The helper makes each cache folder, unsandboxed: through a command's link it would make that
    folder anywhere. The command after it is told and runs nothing; the one after that runs."""
    outside = tmp_path / "outside"
    outside.mkdir()
    frame = device.frame()
    cache = _cache_of(frame)
    (cache / "go").mkdir()
    (cache / "go" / "pkg").symlink_to(outside)
    with pytest.raises(device_exec.FenceRefused):
        await device.run(frame, "true")
    assert list(outside.iterdir()) == [] and not (cache / "go" / "pkg").is_symlink()
    assert (await device.run(frame, "true")).exit_code == 0
    assert (cache / "go" / "pkg" / "mod").is_dir()


@darwin_only
async def test_a_dangling_link_in_a_frames_cache_is_a_refusal_and_never_a_500(
    device: Device, tmp_path: Path
) -> None:
    frame = device.frame()
    (_cache_of(frame) / "npm").symlink_to(tmp_path / "nowhere")
    with pytest.raises(device_exec.FenceRefused):
        await device.run(frame, "true")
    assert (await device.run(frame, "true")).exit_code == 0


@darwin_only
async def test_a_file_in_a_caches_place_is_a_refusal_too(device: Device) -> None:
    frame = device.frame()
    (_cache_of(frame) / "pip").write_text("in the way")
    with pytest.raises(device_exec.FenceRefused):
        await device.run(frame, "true")
    assert (await device.run(frame, "true")).exit_code == 0


@darwin_only
async def test_exec_timeout_kills_process_group(device: Device) -> None:
    frame = device.frame()
    pidfile = Path(frame.paths.work) / "child.pid"
    out = await device.run(frame, f"sleep 300 & echo $! > {pidfile}; sleep 300", limit=1.5)
    assert out.exit_code == 124 and "timed out after 1.5s" in out.stderr
    assert _wait_dead(int(pidfile.read_text()))
    assert not list(frame.groups)


@darwin_only
async def test_huge_and_binary_output_capped(device: Device) -> None:
    """Megabytes and non-UTF-8 are capped and decoded with replacement."""
    frame = device.frame()
    out = await device.run(frame, "head -c 5000000 /dev/urandom; head -c 3000000 /dev/zero | tr '\\0' e >&2")
    assert out.exit_code == 0 and out.truncated
    assert len(out.stdout.encode()) <= 4 * device_exec.OUTPUT_CAP  # replacement chars are 3 bytes
    assert "�" in out.stdout
    assert out.stderr == "e" * device_exec.OUTPUT_CAP
    journal = Path(frame.paths.task).parent / "journal.jsonl"
    assert journal.stat().st_size < 4096


@darwin_only
async def test_symlink_out_of_root_refused(device: Device) -> None:
    """The sandboxed half: `work/link → ~/.ssh` gives nothing inside the fence."""
    frame = device.frame()
    work = Path(frame.paths.work)
    (work / "link").symlink_to(USER_HOME / ".ssh")
    out = await device.run(frame, "ls link/; cat link/known_hosts")
    assert out.exit_code != 0 and EPERM in out.stderr
    assert out.stdout == ""


@darwin_only
async def test_caffeinate_held_during_exec(device: Device) -> None:
    frame = device.frame()
    task = asyncio.create_task(device.run(frame, "sleep 2"))
    deadline = time.monotonic() + 5
    while not list(frame.groups) and time.monotonic() < deadline:  # noqa: ASYNC110 - polling what run() fills
        await asyncio.sleep(0.05)
    (pid,) = frame.groups
    await asyncio.sleep(0.3)
    assert await _pgrep(f"caffeinate -i -w {pid}") == 0, "no idle-sleep assertion while the command runs"
    await task
    await asyncio.sleep(0.2)
    assert await _pgrep(f"caffeinate -i -w {pid}") == 1, "the assertion outlived its command"


@darwin_only
async def test_a_missing_caffeinate_still_runs_and_warns_once(
    device: Device, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(device_exec, "CAFFEINATE", "/nonexistent/caffeinate")
    monkeypatch.setattr(device_exec, "_caffeinate_warned", False)
    frame = device.frame()
    with caplog.at_level(logging.WARNING, logger="prismoid.device.exec"):
        for _ in range(2):
            assert (await device.run(frame, "echo ok")).stdout == "ok\n"
    assert [r.message for r in caplog.records].count(device_exec.CAFFEINATE_MISSING) == 1


@darwin_only
async def test_the_environment_is_built_from_scratch(device: Device, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    frame = device.frame()
    out = await device.run(frame, "env")
    env = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    assert "sk-ant-leak" not in out.stdout and "sk-leak" not in out.stdout
    assert "ALL_PROXY" not in env and "all_proxy" not in env
    proxy = device.proxy.url_for(frame)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[name] == proxy
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    p, base = frame.paths, Path(frame.paths.task).parent
    assert (env["PRISMOID_TASK_DIR"], env["PRISMOID_WORK_DIR"]) == (p.task, p.work)
    assert (env["PRISMOID_ARTIFACTS_DIR"], env["PRISMOID_SKILLS_DIR"]) == (p.artifacts, p.skills)
    assert env["TMPDIR"] == str(base / "tmp")
    assert (env["HOME"], env["SHELL"], env["TERM"], env["GIT_CONFIG_NOSYSTEM"]) == (
        str(USER_HOME),
        "/bin/bash",
        "dumb",
        "1",
    )
    assert env["LC_CTYPE"] == "UTF-8"
    cache = cache_root(frame.ref)
    for name, sub in CACHE_ENV.items():
        assert env[name] == os.path.join(cache, sub)
    shell_own = {"PWD", "SHLVL", "_", "OLDPWD"}
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TMPDIR", "NO_PROXY", "no_proxy"}
    allowed |= {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
    }
    allowed |= set(CACHE_ENV)
    allowed |= {
        "PRISMOID_TASK_DIR",
        "PRISMOID_WORK_DIR",
        "PRISMOID_ARTIFACTS_DIR",
        "PRISMOID_SKILLS_DIR",
        "PRISMOID_PYTHON",
    }
    allowed.add("PRISMOID_NO_RENDITIONS")
    extra = {k for k in env if k not in allowed | shell_own and not k.startswith("LC_")}
    assert not extra, f"inherited: {extra}"


@darwin_only
async def test_sandboxed_curl_is_refused_by_the_proxy_and_recorded(device: Device) -> None:
    frame = device.frame()
    out = await device.run(frame, "curl -sS -o /dev/null https://evil.example/; echo code=$?")
    assert "403" in out.stderr and "code=56" in out.stdout
    assert [d["host"] for d in frame.denials] == ["evil.example"]
    # Straight out, around the proxy: closed by the fence, not the proxy.
    out = await device.run(frame, "curl -sS --noproxy '*' --max-time 5 https://1.1.1.1/; echo code=$?")
    assert "code=0" not in out.stdout and len(frame.denials) == 1


@darwin_only
async def test_agent_writes_only_its_roots_system_adds_artifacts(device: Device) -> None:
    frame = device.frame()
    out = await device.run(frame, 'echo x > "$PRISMOID_TASK_DIR/x"; echo x > "$PRISMOID_ARTIFACTS_DIR/x"')
    assert out.stderr.count(EPERM) == 2
    out = await device.run(
        frame, 'echo x > "$PRISMOID_ARTIFACTS_DIR/a" && echo x > a2 && echo ok', system=True
    )
    assert out.stdout == "ok\n" and Path(frame.paths.artifacts, "a").exists()  # noqa: ASYNC240 - test check
    out = await device.run(frame, 'echo x > "$TMPDIR/t" && echo x > "$PRISMOID_WORK_DIR/w" && echo ok')
    assert out.stdout == "ok\n"


@darwin_only
async def test_a_users_repo_keeps_its_config(device: Device, user_folder: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(user_folder)], check=True)  # noqa: ASYNC221 - setup
    frame = device.frame(FolderGrant(path=str(user_folder), mode="rw"))
    out = await device.run(frame, "echo x >> .git/config; echo hook > .git/hooks/pre-commit; git init -q sub")
    assert out.stderr.count(EPERM) >= 2 and not (user_folder / ".git" / "hooks" / "pre-commit").exists()
    assert not (user_folder / "sub" / ".git").exists()
    out = await device.run(
        frame,
        "echo a > a.txt && git add a.txt && git -c user.name=t -c user.email=t@t commit -qm a && echo ok",
    )
    assert out.stdout == "ok\n", out.stderr


@darwin_only
async def test_leftovers_run_on_and_die_at_destroy(device: Device) -> None:
    frame = device.frame()
    out = await device.run(frame, "sleep 300 > /dev/null 2>&1 & echo $!")
    leftover = int(out.stdout)
    assert _alive(leftover) and list(frame.groups)
    out = await device.run(frame, "sleep 300 & echo $!")  # this one holds the output pipe
    held = int(out.stdout)
    assert _alive(held)
    device.registry.destroy(frame.ref)
    assert _wait_dead(leftover) and _wait_dead(held)


@darwin_only
async def test_stop_services_kills_leftovers_and_keeps_the_frame(tmp_path: Path) -> None:
    """A device Machine's sleep: `stop-services` kills what the frame left running and
    leaves the frame, its folders and its files; a frame the helper never heard of is a 404."""
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as c:
        ref = (
            await c.post("/frames", json={"task_id": f"b5-{uuid.uuid4().hex[:8]}", "kind": "machine"})
        ).json()["ref"]
        try:
            paths = (await c.get(f"/frames/{ref}")).json()["paths"]
            out = await c.post(
                f"/frames/{ref}/exec",
                json={
                    "exec_id": "e1",
                    "command": "echo kept > kept.txt; sleep 300 > /dev/null 2>&1 & echo $!",
                    "timeout": 10,
                },
            )
            leftover = int(out.json()["stdout"])
            assert _alive(leftover)
            assert (await c.post(f"/frames/{ref}/stop-services", json={})).status_code == 204
            assert _wait_dead(leftover)
            assert (await c.get(f"/frames/{ref}")).status_code == 200, "the frame is kept"
            assert (Path(paths["work"]) / "kept.txt").read_text() == "kept\n"
            again = await c.post(
                f"/frames/{ref}/exec", json={"exec_id": "e2", "command": "echo up", "timeout": 10}
            )
            assert again.json()["stdout"] == "up\n", "and it still runs commands"
            assert (await c.post("/frames/device-nobody/stop-services", json={})).status_code == 404
        finally:
            await c.delete(f"/frames/{ref}")
            await app.state.egress.close()


@darwin_only
async def test_cache_and_tmp_deleted_at_destroy_even_read_only(device: Device) -> None:
    frame = device.frame()
    out = await device.run(
        frame,
        'mkdir -p "$GOMODCACHE/m" "$TMPDIR/ro" && touch "$GOMODCACHE/m/f" "$TMPDIR/ro/f"'
        ' && chmod -R a-w "$GOMODCACHE/m" "$TMPDIR/ro" && echo ok',
    )
    assert out.stdout == "ok\n", out.stderr
    cache, base = Path(cache_root(frame.ref)), Path(frame.paths.task).parent
    assert cache.is_dir()  # noqa: ASYNC240 - test check
    device.registry.destroy(frame.ref)
    assert not cache.exists() and not base.exists()  # noqa: ASYNC240 - test check


@darwin_only
async def test_the_helper_routes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as c:
        ref = (
            await c.post("/frames", json={"task_id": f"b3-{uuid.uuid4().hex[:8]}", "kind": "machine"})
        ).json()["ref"]
        try:
            paths = (await c.get(f"/frames/{ref}")).json()["paths"]
            res = await c.post(
                f"/frames/{ref}/exec", json={"exec_id": "e1", "command": "echo hi", "timeout": 10}
            )
            assert res.json() == {"exit_code": 0, "stdout": "hi\n", "stderr": "", "truncated": False}
            assert (await c.get(f"/frames/{ref}/interrupted")).status_code == 204

            data = base64.b64encode(b"hello").decode()
            assert (
                await c.post(
                    f"/frames/{ref}/files/write", json={"path": f"{paths['work']}/h.txt", "data_b64": data}
                )
            ).status_code == 204
            read = await c.post(f"/frames/{ref}/files/read", json={"path": f"{paths['work']}/h.txt"})
            assert base64.b64decode(read.json()["data_b64"]) == b"hello"
            assert (await c.post(f"/frames/{ref}/files/list", json={"path": paths["work"]})).json() == {
                "names": ["h.txt"]
            }
            refused = await c.post(
                f"/frames/{ref}/files/write", json={"path": f"{paths['task']}/x", "data_b64": data}
            )
            assert refused.status_code == 403
            missing = await c.post(f"/frames/{ref}/files/read", json={"path": f"{paths['work']}/nope"})
            assert missing.status_code == 404

            put = await c.put(f"/frames/{ref}/egress", json={"domains": ["example.com"], "version": 2})
            assert put.status_code == 200
            stale = await c.put(f"/frames/{ref}/egress", json={"domains": ["late.example"], "version": 2})
            assert stale.status_code == 409 and stale.json()["error"] == "stale_version"
            await c.post(
                f"/frames/{ref}/exec",
                json={"exec_id": "e2", "command": "curl -s https://evil.example", "timeout": 10},
            )
            denials = (await c.get(f"/frames/{ref}/denials")).json()["denials"]
            assert [d["host"] for d in denials] == ["evil.example"]

            assert (await c.post(f"/frames/{ref}/signal", json={"signal": "stop"})).status_code == 204
            assert (await c.post(f"/frames/{ref}/signal", json={"signal": "cont"})).status_code == 204
            assert (await c.post(f"/frames/{ref}/signal", json={"signal": "kill"})).status_code == 422
        finally:
            await c.delete(f"/frames/{ref}")
            await app.state.egress.close()


@darwin_only
async def test_a_separate_git_dirs_hooks_are_fenced(device: Device, user_folder: Path) -> None:
    """Review finding 2, the fence half: a pointer-file repo's real gitdir keeps its hooks."""
    (user_folder / "store").mkdir()
    gitdir = user_folder / "store" / "sep"
    subprocess.run(  # noqa: ASYNC221 - setup
        ["git", "init", "-q", "-b", "main", f"--separate-git-dir={gitdir}", str(user_folder / "p2")],
        check=True,
    )
    frame = device.frame(FolderGrant(path=str(user_folder), mode="rw"))
    assert str(gitdir.resolve()) in frame.protected_git
    out = await device.run(frame, "echo evil > store/sep/hooks/pre-commit; echo x >> store/sep/config")
    assert out.stderr.count(EPERM) == 2
    assert not (gitdir / "hooks" / "pre-commit").exists()
    out = await device.run(frame, "cd p2 && echo a > a && git add a && git commit -qm a && echo ok")
    assert out.stdout == "ok\n", out.stderr
    with pytest.raises(PermissionError):
        files.write(frame, FileIn(path=str(gitdir / "hooks" / "pre-commit"), data_b64="eA=="))


@darwin_only
async def test_a_worktree_grant_runs_git_and_keeps_the_main_repo_read_only(
    device: Device, user_folder: Path
) -> None:
    """A granted worktree's `.git` points outside it, so its git folders ride
    the grant - git works there, and the repository's hooks, config and files stay read-only."""
    main, wt = user_folder / "main", user_folder / "wt"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(main)], check=True)  # noqa: ASYNC221 - setup
    (main / "a.txt").write_text("a\n")
    subprocess.run([*git, "-C", str(main), "add", "a.txt"], check=True)  # noqa: ASYNC221 - setup
    subprocess.run([*git, "-C", str(main), "commit", "-qm", "a"], check=True)  # noqa: ASYNC221 - setup
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", "-b", "wt", str(wt)], check=True)  # noqa: ASYNC221 - setup

    blind = device.frame(FolderGrant(path=str(wt), mode="rw"))  # the grant without its git folders
    out = await device.run(blind, "echo b > b.txt && git add b.txt")
    assert out.exit_code != 0, "git cannot write the worktree's index outside the grant"

    frame = device.frame(FolderGrant(path=str(wt), mode="rw", git=worktree_git(str(wt))))
    out = await device.run(
        frame,
        "echo b > b.txt && git add b.txt && git status --porcelain && git commit -qm b && echo ok",
    )
    assert out.stdout.endswith("ok\n") and "A  b.txt" in out.stdout, out.stderr
    out = await device.run(
        frame,
        f"echo evil > {main}/.git/hooks/pre-commit; echo x >> {main}/.git/config; "
        f"echo x >> {main}/.git/worktrees/wt/config.worktree; echo x > {main}/a.txt",
    )
    assert out.stderr.count(EPERM) == 4, out.stderr
    assert not (main / ".git" / "hooks" / "pre-commit").exists()
    assert (main / "a.txt").read_text() == "a\n"


@darwin_only
async def test_git_reads_only_the_frames_own_global_config(device: Device, user_folder: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(user_folder)], check=True)  # noqa: ASYNC221 - setup
    frame = device.frame(FolderGrant(path=str(user_folder), mode="rw"))
    out = await device.run(frame, "git config --show-origin --list")
    gitconfig = str(Path(frame.paths.task).parent / "gitconfig")
    origins = {line.split("\t", 1)[0] for line in out.stdout.splitlines()}
    assert origins <= {f"file:{gitconfig}", "file:.git/config"}, origins
    assert f"file:{gitconfig}\tcore.hookspath=/dev/null" in out.stdout
    assert f"file:{gitconfig}\tsafe.barerepository=explicit" in out.stdout
    assert "insteadof" not in out.stdout.lower()
    assert Path(gitconfig).stat().st_mode & 0o777 == 0o600  # noqa: ASYNC240 - test check
    out = await device.run(frame, "git config --global x.y z")
    assert out.exit_code != 0  # the frame's config is read-only inside the fence


@darwin_only
async def test_every_command_is_told_there_are_no_renditions(device: Device) -> None:
    frame = device.frame()
    for system in (False, True):
        out = await device.run(frame, 'echo "[$PRISMOID_NO_RENDITIONS]"', system=system)
        assert out.stdout == "[1]\n"


@darwin_only
async def test_a_system_exec_gets_only_the_os_path(device: Device) -> None:
    frame = device.frame()
    out = await device.run(frame, 'echo "$PATH"', system=True)
    venv = Path(frame.home) / "runtime" / "venv" / "bin"
    assert out.stdout == f"{venv}:{device_exec.SYSTEM_PATH}:/opt/homebrew/bin:/usr/local/bin\n"
    out = await device.run(frame, 'echo "$PATH"')
    assert cache_root(frame.ref) in out.stdout


@darwin_only
async def test_a_stale_group_number_is_never_signalled(device: Device, tmp_path: Path) -> None:
    """Review finding 3a: a group number the frame once had, now leading a stranger's group."""
    frame = device.frame()
    # A stranger's group, outside any frame: its pid is its group's id.
    stranger = subprocess.Popen(["sleep", "300"], start_new_session=True)  # noqa: ASYNC220 - setup
    try:
        floor = time.time_ns() // 1000 - 10_000_000
        frame.groups.spawned(stranger.pid, floor)
        frame.groups._groups[stranger.pid].members = {stranger.pid: floor}  # pyright: ignore[reportPrivateUsage] - a stale start time
        dead = subprocess.Popen(["true"], start_new_session=True)  # noqa: ASYNC220 - setup
        dead.wait()
        frame.groups.spawned(dead.pid, floor)
        await device.run(frame, "true")  # every exec prunes
        assert stranger.pid not in list(frame.groups) and dead.pid not in list(frame.groups)
        frame.groups.spawned(stranger.pid, floor)
        frame.groups._groups[stranger.pid].members = {stranger.pid: floor}  # pyright: ignore[reportPrivateUsage]
        device.registry.destroy(frame.ref)
        assert stranger.poll() is None, "destroy killed a group that was not the frame's"
    finally:
        stranger.kill()
        stranger.wait()


@darwin_only
async def test_a_new_helper_life_kills_what_the_last_one_left(device: Device) -> None:
    """Review finding 3b, and ruling A: the leftover dies, the cut-off command stays reported."""
    frame = device.frame()
    out = await device.run(frame, "sleep 300 > /dev/null 2>&1 & echo $!")
    leftover = int(out.stdout)
    groups_file = Path(frame.paths.task).parent / "groups.json"
    assert groups_file.stat().st_mode & 0o777 == 0o600
    frame.journal.start("cut", "make deploy")  # as if the helper died while this ran
    reborn = FrameRegistry(device.registry.home).frames[frame.ref]
    assert _wait_dead(leftover)
    cut = reborn.journal.interrupted()
    assert cut is not None and (cut.exec_id, cut.still_running) == ("cut", False)


@darwin_only
async def test_helper_shutdown_kills_every_frames_leftovers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://helper", headers={KEY_HEADER: key}) as c,
    ):
        ref = (
            await c.post("/frames", json={"task_id": f"b3-{uuid.uuid4().hex[:8]}", "kind": "machine"})
        ).json()["ref"]
        out = await c.post(
            f"/frames/{ref}/exec",
            json={"exec_id": "e1", "command": "sleep 300 >/dev/null 2>&1 & echo $!", "timeout": 10},
        )
        leftover = int(out.json()["stdout"])
        assert _alive(leftover)
    assert _wait_dead(leftover)
    FrameRegistry(home).destroy(ref)


@darwin_only
async def test_interrupted_reports_a_running_exec_and_ack_clears_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as c:
        ref = (
            await c.post("/frames", json={"task_id": f"b3-{uuid.uuid4().hex[:8]}", "kind": "machine"})
        ).json()["ref"]
        try:
            running = asyncio.create_task(
                c.post(f"/frames/{ref}/exec", json={"exec_id": "e1", "command": "sleep 1", "timeout": 10})
            )
            await asyncio.sleep(0.3)
            cut = (await c.get(f"/frames/{ref}/interrupted")).json()
            assert (cut["exec_id"], cut["command_head"], cut["still_running"]) == ("e1", "sleep 1", True)
            assert (await c.post(f"/frames/{ref}/interrupted/ack", json={"exec_id": "e1"})).status_code == 204
            assert (await c.get(f"/frames/{ref}/interrupted")).status_code == 204
            await running
        finally:
            await c.delete(f"/frames/{ref}")
            await app.state.egress.close()


@darwin_only
async def test_git_over_https_authenticates_to_the_frames_proxy(device: Device) -> None:
    """git's libcurl sends the proxy credential only when challenged: its refusal is the frame's."""
    frame = device.frame()
    out = await device.run(frame, "git ls-remote https://evil.example/x.git")
    assert out.exit_code != 0 and "403" in out.stderr, out.stderr
    assert [d["host"] for d in frame.denials] == ["evil.example"]
