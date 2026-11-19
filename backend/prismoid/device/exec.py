# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Fenced commands on the device: `run`, one command of a frame.

`sandbox-exec -p <the frame's profile> /bin/bash --noprofile --norc -c <command>`, in its own
process group, in the work folder (`out/` for a system command). The environment is built from
nothing: the helper holds the model providers' keys, and a command inherits none of them. It gets
the user's own HOME (toolchains read their config there), the frame's proxy credential, its real
folders as `PRISMOID_*_DIR`, a TMPDIR in the frame folder and every toolchain cache pointed at the
frame's own cache folder.

Output is capped per stream and decoded with replacement. At the timeout the whole group is
killed. What a command leaves running (`cmd &`) runs on, as on every driver; its group is kept on
the frame and killed when the frame is destroyed. While a command runs, `caffeinate` holds an
idle-sleep assertion on it. A stopping helper stops its commands (`FrameRegistry.stop`): one cut
that way has no result and no `finish`, so the journal still names it as cut off.

After each command its servers are looked at (`ports`: one listening beyond loopback is stopped)
and its rw grants walked for planted repositories and outward links (`plants`); what either did is
told to the agent at the end of the command's stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pwd
import signal
import stat
import time
from collections.abc import Awaitable, Callable
from typing import Final

from prismoid.conf import settings
from prismoid.device import docker_proxy
from prismoid.device.egress import DeviceEgress
from prismoid.device.fence_data import CACHE_ENV
from prismoid.device.frames import GIT_CONFIG_FILE, DeviceFrameState, HelperStopping
from prismoid.device.ports import FramePorts
from prismoid.device.seatbelt import FenceSpec, cache_root, profile_for
from prismoid.schemas.device import (
    NO_RENDITIONS,
    RUNTIME_DIR,
    SYSTEM_PYTHON,
    TOOL_DIRS,
    ExecIn,
    ExecOut,
    runtime_python,
)

LOG = logging.getLogger(__name__)


class FenceRefused(ValueError):
    """The frame's fence would not be safe: nothing ran."""


SANDBOX_EXEC: Final = "/usr/bin/sandbox-exec"
CAFFEINATE = "/usr/bin/caffeinate"  # a seam for tests
CAFFEINATE_MISSING: Final = "caffeinate is missing: the Mac may idle-sleep during a command"
OUTPUT_CAP: Final = 1024 * 1024  # bytes per stream
# After the command exits, how long its output may take to reach EOF before a leftover that holds
# the pipe (`cmd &` without a redirect) is drained in the background instead.
_SETTLE_S: Final = 0.25
_NO_PROXY: Final = "localhost,127.0.0.1,::1"
_DEFAULT_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
# A system command's PATH: Prismoid's runtime venv first (`system_path`), then the OS's own folders and
# the optional tools', never one an agent can write (its caches' bins). Its cwd, `out/`, IS
# agent-writable, so the system's Python is `"$PRISMOID_PYTHON" -I`: an absolute path, and nothing of
# the cwd, the script's folder or the environment on its import path.
SYSTEM_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"

_caffeinate_warned = False


def system_path(frame: DeviceFrameState) -> str:
    return ":".join((str(runtime_python(frame.home).parent), SYSTEM_PATH, *TOOL_DIRS))


def fence_for(
    frame: DeviceFrameState, *, system: bool, proxy_port: int, deny_ports: tuple[int, ...] = ()
) -> FenceSpec:
    """The fence for one command of `frame`. Its rw grants are user roots (write roots), its ro
    grants and its given folders are read-only; Prismoid's own write roots are the work folder (when
    no grant is `/work`), `out/`, `tmp/` and, with the Docker grant, its own Docker config. A
    system command also writes `artifacts/`; neither ever writes `task/`, `skills/`, `verify/` or
    Prismoid's runtime, which both read (public packages and the house style, nothing of anyone's).
    A granted worktree's git folders outside the grant are writable for git's state files only.
    With the Docker grant, its one Unix socket is the helper's filtering Docker socket."""
    p, rw = frame.paths, frame.granted("rw")
    own = [p.out, str(frame.base / "tmp")]
    if p.work not in rw:  # no grant is `/work`: the frame's own fresh folder is
        own.insert(0, p.work)
    if frame.spec.docker:
        own.append(str(frame.docker_config))
    writes = [*rw, *own, *([p.artifacts] if system else [])]
    given = [
        p.task,
        p.skills,
        p.verify,
        str(frame.home / RUNTIME_DIR),
        *([] if system else [p.artifacts]),
        str(frame.base / GIT_CONFIG_FILE),
    ]
    return FenceSpec(
        write_roots=tuple(writes),
        read_only_roots=(*frame.granted("ro"), *given),
        user_roots=tuple(rw),
        protected_git=frame.protected_git,
        pinned_git=frame.pinned_git,
        linked_git=frame.linked_git,
        cache_root=cache_root(frame.ref),
        proxy_port=proxy_port,
        deny_ports=(settings.local.api_port, *deny_ports),
        docker_socket=str(docker_proxy.socket_path(frame.home, frame.ref)) if frame.spec.docker else None,
    )


def _agent_path(frame: DeviceFrameState) -> str:
    """An agent command's PATH. In a granted folder the toolchain is the user's, untouched (spec O4).
    In the frame's own fresh `/work` there is no project to have one, so `python3` is Prismoid's runtime,
    as the shell tool's doc promises. Never a PYTHONPATH: its compiled wheels are for its own Python."""
    cache = cache_root(frame.ref)
    # What a command installs into its frame's cache (`cargo install`, `uv tool install`) runs.
    bins = [
        os.path.join(cache, CACHE_ENV["CARGO_HOME"], "bin"),
        os.path.join(cache, CACHE_ENV["UV_TOOL_BIN_DIR"]),
    ]
    fresh = frame.paths.work not in frame.granted("rw")
    first = [str(runtime_python(frame.home).parent)] if fresh else []
    return ":".join([*first, os.environ.get("PATH") or _DEFAULT_PATH, *bins])


def environment(frame: DeviceFrameState, proxy_url: str, *, system: bool = False) -> dict[str, str]:
    """The command's whole environment, from an allowlist: nothing else of the helper's leaks."""
    user = pwd.getpwuid(os.getuid())
    cache = cache_root(frame.ref)
    env = {
        "PATH": system_path(frame) if system else _agent_path(frame),
        "HOME": user.pw_dir,
        "USER": user.pw_name,
        "LOGNAME": user.pw_name,
        "SHELL": "/bin/bash",
        "TERM": "dumb",
    }
    env |= {k: v for k, v in os.environ.items() if k == "LANG" or k.startswith("LC_")}
    env |= dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"), proxy_url)
    env |= {"NO_PROXY": _NO_PROXY, "no_proxy": _NO_PROXY}
    p = frame.paths
    env |= {
        "PRISMOID_TASK_DIR": p.task,
        "PRISMOID_WORK_DIR": p.work,
        "PRISMOID_ARTIFACTS_DIR": p.artifacts,
        "PRISMOID_SKILLS_DIR": p.skills,
        "TMPDIR": str(frame.base / "tmp"),
    }
    env[SYSTEM_PYTHON] = str(runtime_python(frame.home))
    if not settings.local.has_office:
        env[NO_RENDITIONS] = "1"  # the house's `to_pdf` asks: geometry checks only, no `soffice`
    env |= {name: os.path.join(cache, sub) for name, sub in CACHE_ENV.items()}
    # The system gitconfig is not the user's to vouch for, and the user's global one is replaced by
    # the frame's own: their name and email, none of their `insteadOf`, helpers or hooks.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(frame.base / GIT_CONFIG_FILE)
    if frame.spec.docker:
        # The user's `~/.docker/config.json` holds registry credentials and is read-denied: the
        # Docker CLI keeps the frame's own, and talks to the helper's filtering socket only.
        env["DOCKER_CONFIG"] = str(frame.docker_config)
        env["DOCKER_HOST"] = f"unix://{docker_proxy.socket_path(frame.home, frame.ref)}"
    return env


class _Capture:
    """One output stream: the first `OUTPUT_CAP` bytes kept, the rest read and dropped, so the
    command never blocks on a full pipe. A plain `os.pipe`, not asyncio's: asyncio's
    `Process.wait()` resolves only once every pipe it owns has closed, so a leftover holding one
    would stall the command's own exit until the timeout killed both."""

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.data = bytearray()
        self.truncated = False
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """After the spawn: the child holds the write end now, so ours closes (EOF is its exit)."""
        os.close(self.write_fd)
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(self.read_fd, "rb", buffering=0)
        )
        self.task = asyncio.create_task(self._read(reader, transport))

    async def _read(self, reader: asyncio.StreamReader, transport: asyncio.BaseTransport) -> None:
        try:
            while chunk := await reader.read(65536):
                room = OUTPUT_CAP - len(self.data)
                if len(chunk) > room:
                    self.truncated = True
                if room > 0:
                    self.data += chunk[:room]
        finally:
            transport.close()

    def close(self) -> None:
        """Only when the spawn failed: neither end was handed on."""
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)

    def text(self) -> str:
        return bytes(self.data).decode(errors="replace")


async def _hold_awake(pid: int) -> asyncio.subprocess.Process | None:
    """`caffeinate -i -w <pid>`: an idle-sleep assertion that ends with the command."""
    global _caffeinate_warned
    try:
        return await asyncio.create_subprocess_exec(
            CAFFEINATE,
            "-i",
            "-w",
            str(pid),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        if not _caffeinate_warned:
            _caffeinate_warned = True
            LOG.warning(CAFFEINATE_MISSING)
        return None


async def _release(awake: asyncio.subprocess.Process | None) -> None:
    if awake is None:
        return
    if awake.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            awake.kill()
    await awake.wait()


async def _here() -> bool:
    return False


def _refused(why: str) -> ExecOut:
    return ExecOut(exit_code=126, stdout="", stderr=f"{why}\n", truncated=False)


def _unspeakable(req: ExecIn) -> str | None:
    """What no argv and no journal line can hold. A NUL: uvloop would cut the command there and run
    the rest of nothing. A lone surrogate: past the journal it would fail the spawn, and leave an
    entry that cannot be read back."""
    if "\0" in req.command:
        return "a NUL byte"
    try:
        (req.command + req.exec_id).encode()
    except UnicodeEncodeError:
        return "text that is not Unicode"
    return None


def _own_folders(tmp: str, cache: str) -> None:
    """The frame's scratch and each of its caches, as folders. The helper is not sandboxed and a
    command writes its cache, so no link there is followed. `FenceRefused` for anything else in a
    cache's place: nothing is made through it, and it goes, or no command could ever clear it."""
    try:
        os.makedirs(tmp, exist_ok=True)
        os.makedirs(cache, exist_ok=True)
        top = os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise FenceRefused(f"the frame's own folders: {e.strerror}") from e
    try:
        for sub in CACHE_ENV.values():
            _cache_folder(top, sub.split("/"))
    except OSError as e:
        raise FenceRefused(f"the frame's cache: {e.strerror}") from e
    finally:
        os.close(top)


def _cache_folder(top: int, parts: list[str]) -> None:
    fd = os.dup(top)
    try:
        for part in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=fd)
            child = _open_or_clear(fd, part)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def _open_or_clear(fd: int, part: str) -> int:
    try:
        return os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as e:
        if stat.S_ISDIR(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode):
            os.chmod(part, stat.S_IRWXU, dir_fd=fd, follow_symlinks=False)
        else:
            os.unlink(part, dir_fd=fd)
        raise FenceRefused(f"the frame's cache had no folder at {part}: put right, run it again") from e


async def _fenced(
    frame: DeviceFrameState,
    req: ExecIn,
    proxy: DeviceEgress,
    docker: docker_proxy.DockerProxies | None,
) -> tuple[str, dict[str, str]]:
    """The command's profile and environment, with all they name there: its folders, its proxy, its
    Docker socket. `FenceRefused` when any of that says the fence would not be safe."""
    try:
        fence = fence_for(frame, system=req.system, proxy_port=proxy.port, deny_ports=(proxy.socket_port,))
        profile = profile_for(fence, frame.home)
        env = environment(frame, proxy.url_for(frame), system=req.system)
        _own_folders(env["TMPDIR"], cache_root(frame.ref))
        frame.link_aliases()
        await proxy.serve()
        if docker is not None:
            await docker.ensure(frame)
    except ValueError as e:
        raise FenceRefused(str(e)) from e
    return profile, env


async def _exited(
    frame: DeviceFrameState,
    req: ExecIn,
    proc: asyncio.subprocess.Process,
    out: _Capture,
    err: _Capture,
) -> tuple[int, bool]:
    """Wait the command out, awake, and its output with it: (its return code, whether it timed out).
    What is cancelled is killed, nobody waiting for it, and the journal shows it cut off."""
    awake = await _hold_awake(proc.pid)
    timed_out = False
    try:
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=req.timeout)
        except TimeoutError:
            timed_out = True
            frame.groups.signal(signal.SIGKILL, only=proc.pid)
            returncode = await proc.wait()
        readers = {t for t in (out.task, err.task) if t is not None}
        _, pending = await asyncio.wait(readers, timeout=_SETTLE_S)
        for task in pending:  # a leftover holds the pipe: keep it drained, keep what we have
            frame.drains.add(task)
            task.add_done_callback(frame.drains.discard)
    except asyncio.CancelledError:
        frame.groups.signal(
            signal.SIGKILL, only=proc.pid
        )  # nobody waits for it; the journal shows it cut off
        raise
    finally:
        await _release(awake)
        frame.groups.exited(proc.pid)
    return returncode, timed_out


async def run(
    frame: DeviceFrameState,
    req: ExecIn,
    *,
    proxy: DeviceEgress,
    docker: docker_proxy.DockerProxies | None = None,
    ports: FramePorts | None = None,
    gone: Callable[[], Awaitable[bool]] = _here,
) -> ExecOut:
    """Run one command of `frame` under its fence. Raises `FenceRefused` when the frame's fence is
    unsafe (`profile_for` refuses to write it): the command never runs unfenced. With the Docker
    grant, `docker` serves the frame's filtering socket for its grants as they are now. `ports`
    looks at the frame's servers after the command, and keeps looking while any run on. `gone` says
    whoever asked has left. Raises `HelperStopping` when the helper is stopping: the command never
    starts, or it was cut. A command the journal cannot hold never runs: it answers 126."""
    if why := _unspeakable(req):
        return _refused(f"could not start the command: {why}")
    profile, env = await _fenced(frame, req, proxy, docker)
    cwd = frame.paths.out if req.system else frame.paths.work
    argv = [SANDBOX_EXEC, "-p", profile, "/bin/bash", "--noprofile", "--norc", "-c", req.command]
    if frame.stopping.is_set():  # before the journal: nothing of this one is owed to anybody
        raise HelperStopping("the device helper is stopping")
    frame.groups.prune()  # every exec forgets the groups that are gone or no longer provably its own
    try:
        frame.journal.start(req.exec_id, req.command)
    except OSError as e:
        return _refused(f"could not record the command, so it did not run: {e}")
    out, err = _Capture(), _Capture()
    floor = time.time_ns() // 1000 - 1_000_000  # nothing of this command started before (1 s slack)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out.write_fd,
            stderr=err.write_fd,
            start_new_session=True,  # its own process group: a timeout kills every child too
        )
    except OSError as e:
        out.close()
        err.close()
        code = 127 if isinstance(e, FileNotFoundError) else 126
        frame.journal.finish(req.exec_id, code)
        return ExecOut(
            exit_code=code, stdout="", stderr=f"could not start the command: {e}\n", truncated=False
        )
    frame.groups.spawned(proc.pid, floor)
    if frame.stopping.is_set():  # the stop looked while this was spawning, and never saw its group
        frame.groups.signal(signal.SIGKILL, only=proc.pid)
    if ports is not None:
        ports.watch()  # a server in the foreground is looked at while it runs
    await out.start()
    await err.start()
    returncode, timed_out = await _exited(frame, req, proc, out, err)
    if frame.stopping.is_set():
        # Cut, not finished: its exit is the stop's signal (or, by a hair, its own, which costs a
        # resumed agent one look at the folder). A `finish` here would erase what parks the step.
        frame.journal.cut(req.exec_id)
        raise HelperStopping("the device helper stopped this command")
    code = 124 if timed_out else (returncode if returncode >= 0 else 128 - returncode)
    frame.journal.finish(req.exec_id, code)
    await _after(frame, ports)
    stderr = err.text()
    if timed_out:
        stderr = _line(stderr, f"timed out after {req.timeout}s")
    told = list(frame.notices)
    for note in told:
        stderr = _line(stderr, note)
    result = ExecOut(
        exit_code=code, stdout=out.text(), stderr=stderr, truncated=out.truncated or err.truncated
    )
    if await gone():  # nobody hears this: the journal says so, and its notes wait for a result that is
        frame.journal.unseen(req.exec_id, req.command, code)
    else:
        try:
            frame.journal.heard()
            frame.notices.told(told)
        except OSError:  # the command finished: a full disk costs the bookkeeping, never its result
            LOG.exception("could not record the result heard on frame %s", frame.ref)
    return result


async def _after(frame: DeviceFrameState, ports: FramePorts | None) -> None:
    """What the command left: a server beyond loopback stopped, a planted repository or link
    moved. Neither ever fails the command's own result."""
    try:
        if ports is not None:
            await ports.look(frame, since=time.monotonic())  # what the command left, not before
            ports.watch()
    except Exception:
        LOG.exception("could not look at the servers frame %s's command left", frame.ref)
    await frame.sweep_within()


def _line(text: str, line: str) -> str:
    sep = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{sep}{line}\n"
