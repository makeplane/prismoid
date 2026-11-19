# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid up`'s process tree (ADR-077): children started in order, each waited for, stopped in reverse.

One supervisor per data home. The lock is `run/prismoid.lock`, an `flock` held for the supervisor's life
with its pid, that pid's start time and its role (`up`, or a `helper` started on its own) written
inside. A lock nobody holds is stale whatever it names, and the next `up` takes it over. Every child
inherits the lock's descriptor, so a stack whose supervisor was killed outright still holds it while
any child lives: a second `up` attaches instead of starting a second helper beside the orphaned one
(a second helper on the same home adopts, and kills, the first one's process groups).

**Nothing is signalled on a pid alone.** Pids are reused; a pid and the start time recorded beside
it name one process, and only a process whose start time still matches is ever sent a signal.

Each child gets its own session, so the terminal's Ctrl-C (or its closing: SIGHUP) reaches the
supervisor alone and the children stop in the order it chooses; a second Ctrl-C (only Ctrl-C) kills
what is left, but for a child that `keeps_its_stop`: the helper, whose commands live in sessions of
their own, so killing it early would leave them running under nobody.
Their output goes to `run/<name>.log` (0600), which is also where readiness is read from. Every child
runs in the data home (no stray `.env` from wherever `up` was typed). No child is ever restarted: one
that exits stops the rest (v1).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from prismoid.device import procs

LOCK_FILE = "prismoid.lock"
STATE_FILE = "prismoid.state"
ROLE_UP = "up"
ROLE_HELPER = "helper"
_TICK_S = 0.1
_TAIL_LINES = 20
_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@dataclass(frozen=True)
class ChildSpec:
    """One supervised process. `ready` sees each new line of its output, and `None` once per tick (a
    readiness that is asked, not printed: a health check)."""

    name: str
    argv: list[str]
    env: dict[str, str]
    ready: Callable[[str | None], bool]
    ready_timeout_s: float = 60.0
    keeps_its_stop: bool = False  # a second Ctrl-C does not cut its `stop_timeout_s` short


class AlreadyRunning(RuntimeError):
    """Another supervisor (or a helper started on its own) holds this home's lock."""

    def __init__(self, pid: int | None) -> None:
        super().__init__(f"Prismoid is already running (pid {pid})")
        self.pid = pid


class ChildFailed(RuntimeError):
    """A child exited while the stack was meant to be up. `tail` is the end of its log."""

    def __init__(self, name: str, code: int | None, tail: str, log: Path) -> None:
        super().__init__(f"{name} exited unexpectedly (code {code})")
        self.name, self.code, self.tail, self.log = name, code, tail, log


class ReadyTimeout(ChildFailed):
    def __init__(self, name: str, timeout_s: float, tail: str, log: Path) -> None:
        RuntimeError.__init__(self, f"{name} was not ready after {timeout_s:.0f}s")
        self.name, self.code, self.tail, self.log = name, None, tail, log


def run_dir(home: Path) -> Path:
    """`<home>/run`, made if missing and private (0700) even if it existed looser."""
    run = home / "run"
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = run.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"{run} is not a directory owned by this user; refusing to use it")
    if st.st_mode & 0o077:
        run.chmod(0o700)
    return run


def started(pid: int) -> int | None:
    """When `pid` started (an opaque number, equal for the same process), or None when it is gone or
    cannot be read - and then it is never signalled."""
    if sys.platform == "darwin":
        return procs.started(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except OSError:
        return None
    return int(fields[19]) if len(fields) > 19 else None  # field 22, `starttime`


@dataclass(frozen=True)
class Process:
    """A pid and the start time recorded beside it: one process, never whoever reuses the pid."""

    pid: int
    started: int | None

    @classmethod
    def of(cls, pid: int) -> Process:
        return cls(pid, started(pid))

    def alive(self) -> bool:
        return self.started is not None and started(self.pid) == self.started

    def signal(self, sig: int, *, group: bool = False) -> None:
        """Signal it (its own group with `group`, when it leads one) only while it is provably itself."""
        if not self.alive():
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if group and os.getpgid(self.pid) == self.pid:
                os.killpg(self.pid, sig)
            else:
                os.kill(self.pid, sig)


@dataclass(frozen=True)
class Holder:
    process: Process
    role: str

    @property
    def pid(self) -> int:
        return self.process.pid


class InstanceLock:
    """`run/prismoid.lock`: one stack per home."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / "run" / LOCK_FILE  # made only by `acquire`: asking never creates the home
        self._fd: int | None = None
        self._role = ROLE_UP

    @property
    def held(self) -> bool:
        return self._fd is not None

    def fileno(self) -> int:
        if self._fd is None:
            raise RuntimeError("the lock is not held")
        return self._fd

    def acquire(self, role: str = ROLE_UP) -> bool:
        """Take the lock (and write who we are in it), or say no when another process holds it."""
        run_dir(self.home)
        for _ in range(3):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            if self._is_the_file(fd):
                self._own(fd, role)
                return True
            os.close(fd)  # unlinked or replaced while we waited: lock the file that is there now
        return False

    def adopt(self, fd: int, role: str = ROLE_UP) -> None:
        """Take over a descriptor of the held lock handed down by `prismoid up --background`. It must be
        this lock file and it must hold the lock (re-locking our own description cannot block)."""
        if not self._is_the_file(fd):
            raise RuntimeError(f"descriptor {fd} is not {self.path}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._own(fd, role)

    def _is_the_file(self, fd: int) -> bool:
        try:
            here = self.path.lstat()
        except FileNotFoundError:
            return False
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino) == (here.st_dev, here.st_ino) and stat.S_ISREG(here.st_mode)

    def _own(self, fd: int, role: str) -> None:
        os.fchmod(fd, 0o600)  # a file left by an earlier run keeps its old mode otherwise
        os.ftruncate(fd, 0)
        me = Process.of(os.getpid())
        os.pwrite(fd, f"{me.pid} {me.started or 0} {role}\n".encode(), 0)
        self._fd, self._role = fd, role

    def release(self) -> None:
        """Close our descriptor. What it names stays in the file; with nobody holding the lock it is stale."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def taken(self) -> bool:
        """Whether anyone (us included) holds the lock now."""
        return self._fd is not None or self._peek() is not None

    def holder(self) -> Holder | None:
        """Who wrote the lock that is held now; None when nobody holds it, or its holder has not
        written itself (yet) in a form we read. Asked of the lock, never of a pid."""
        if self._fd is not None:
            return Holder(Process.of(os.getpid()), self._role)
        text = self._peek()
        return None if text is None else _parse_holder(text)

    def _peek(self) -> str | None:
        """The lock file's text while someone holds the lock; None when nobody does."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return os.read(fd, 128).decode(errors="replace")
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        finally:
            os.close(fd)


def _parse_holder(text: str) -> Holder | None:
    parts = text.split()
    if not parts or not all(p.isdigit() for p in parts[:2]):
        return None
    if len(parts) == 1:  # a lock written before start times were: its holder is never provable
        return Holder(Process(int(parts[0]), None), ROLE_UP)
    return Holder(Process(int(parts[0]), int(parts[1]) or None), parts[2] if len(parts) > 2 else ROLE_UP)


class _Child:
    def __init__(self, spec: ChildSpec, log: Path, cwd: Path, lock_fd: int) -> None:
        self.spec, self.log = spec, log
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        try:
            self.proc = subprocess.Popen(
                spec.argv,
                env=spec.env,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # Ctrl-C reaches the supervisor, which picks the order
                pass_fds=(lock_fd,),  # the stack holds the lock while any part of it lives
            )
        finally:
            os.close(fd)
        self.process = Process.of(self.proc.pid)
        self._reader = log.open("rb")
        self._partial = b""

    def new_lines(self) -> list[str]:
        data = self._partial + self._reader.read()
        *lines, self._partial = data.split(b"\n")
        return [ln.decode(errors="replace").rstrip("\r") for ln in lines]

    def tail(self) -> str:
        try:
            lines = self.log.read_bytes().decode(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-_TAIL_LINES:])

    def close(self) -> None:
        self._reader.close()


class Supervisor:
    stop_timeout_s = 15.0

    def __init__(
        self,
        home: Path,
        specs: list[ChildSpec],
        *,
        lock: InstanceLock | None = None,
        facts: dict[str, Any] | None = None,
    ) -> None:
        """`lock` is passed by a caller that took it before doing work of its own under it (`prismoid up`
        runs migrations and the first run first); that caller releases it. `facts` are written to
        the state file beside the children's pids (the web URL), for `status` in another process."""
        self.home = home
        self.specs = specs
        self.lock = lock or InstanceLock(home)
        self._own_lock = lock is None
        self.facts = facts or {}
        self._state = home / "run" / STATE_FILE
        self._started: list[_Child] = []
        self._stopping = False
        self._interrupted = False
        self._kill_now = False

    # ── the supervisor itself ────────────────────────────────────────────────────────────────

    def up(self, on_ready: Callable[[], None] | None = None) -> None:
        """Start every child in order, each waited for; call `on_ready`; then hold until SIGINT,
        SIGTERM or SIGHUP (normal return) or a child's exit (`ChildFailed`). Either way every started
        child is stopped, in reverse. Raises `AlreadyRunning` when the home's lock is someone else's."""
        if not self.lock.held and not self.lock.acquire():
            holder = self.lock.holder()
            raise AlreadyRunning(holder.pid if holder else None)
        previous = {s: signal.signal(s, self._on_signal) for s in _SIGNALS}
        try:
            for spec in self.specs:
                if self._stopping:
                    return
                self._start(spec)
                if not self._await_ready(self._started[-1]):
                    return
            self._write_state(ready=True)
            if on_ready is not None:
                on_ready()
            self._watch()
        finally:
            for child in reversed(self._started):
                self._stop(child)
            self._state.unlink(missing_ok=True)
            for s, handler in previous.items():
                signal.signal(s, handler)
            if self._own_lock:
                self.lock.release()

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        # Only a second Ctrl-C kills what has not stopped yet: a closing terminal sends SIGHUP more
        # than once, and a SIGTERM may be repeated by whoever sent it - neither is a person insisting.
        if signum == signal.SIGINT:
            self._kill_now = self._interrupted
            self._interrupted = True
        self._stopping = True

    def _start(self, spec: ChildSpec) -> None:
        log = run_dir(self.home) / f"{spec.name}.log"
        self._started.append(_Child(spec, log, self.home, self.lock.fileno()))
        self._write_state(ready=False)

    def _await_ready(self, child: _Child) -> bool:
        """True when ready, False when a signal came first; raises if the child exits or times out."""
        deadline = time.monotonic() + child.spec.ready_timeout_s
        while not self._stopping:
            self._check_alive()
            if any(child.spec.ready(line) for line in child.new_lines()) or child.spec.ready(None):
                return True
            if time.monotonic() > deadline:
                raise ReadyTimeout(child.spec.name, child.spec.ready_timeout_s, child.tail(), child.log)
            time.sleep(_TICK_S)
        return False

    def _watch(self) -> None:
        while not self._stopping:
            self._check_alive()
            time.sleep(_TICK_S)

    def _check_alive(self) -> None:
        for child in self._started:
            code = child.proc.poll()
            if code is not None:
                raise ChildFailed(child.spec.name, code, child.tail(), child.log)

    def _stop(self, child: _Child) -> None:
        """SIGINT, not SIGTERM: the worker shuts DBOS down on KeyboardInterrupt, and uvicorn (the API,
        the helper) treats both alike. A child not gone by the timeout, or at a second Ctrl-C unless
        it `keeps_its_stop`, is killed with its whole group (its own session: the group is its own).
        Our own unreaped child keeps its pid, so these signals need no proof."""
        if child.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                child.proc.send_signal(signal.SIGINT)
            deadline = time.monotonic() + self.stop_timeout_s
            while child.proc.poll() is None and not self._hurried(child) and time.monotonic() < deadline:
                time.sleep(_TICK_S)
            if child.proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(child.proc.pid, signal.SIGKILL)
                child.proc.wait()
        child.close()

    def _hurried(self, child: _Child) -> bool:
        return self._kill_now and not child.spec.keeps_its_stop

    def _write_state(self, *, ready: bool) -> None:
        state = {
            "pid": os.getpid(),
            "ready": ready,
            "children": {c.spec.name: [c.process.pid, c.process.started] for c in self._started},
            **self.facts,
        }
        tmp = run_dir(self.home) / (STATE_FILE + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        tmp.replace(self._state)

    # ── seen from another process ────────────────────────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        """What the running supervisor wrote, or {} when nothing runs (or it has not written yet)."""
        if not self.lock.taken():
            return {}
        try:
            data = json.loads(self._state.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _recorded(self) -> dict[str, Process]:
        children = self.state().get("children", {})
        out: dict[str, Process] = {}
        if isinstance(children, dict):
            for name, entry in children.items():
                if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], int):
                    out[str(name)] = Process(entry[0], entry[1] if isinstance(entry[1], int) else None)
        return out

    def status(self) -> dict[str, str]:
        """Each child, `running` or `stopped`. The names are this supervisor's specs, else what the
        running one recorded."""
        recorded = self._recorded()
        names = [s.name for s in self.specs] or list(recorded)
        return {n: "running" if n in recorded and recorded[n].alive() else "stopped" for n in names}

    def down(self, timeout_s: float = 120.0) -> None:
        """Ask the running stack to stop and wait for the lock to come free; nothing running is a
        no-op. The holder gets SIGTERM (it stops its children in reverse) - if it is provably the one
        that wrote the lock. A holder that is gone (killed outright, or its pid now someone else's)
        leaves the lock to its orphaned children: each one provably still itself gets SIGINT, then
        its group SIGKILL if the lock is not free in `stop_timeout_s`."""
        if not self.lock.taken():
            return
        holder = self.lock.holder()
        if holder is not None and holder.process.alive():
            holder.process.signal(signal.SIGTERM)
            if self._wait_free(timeout_s):
                return
            raise TimeoutError(f"Prismoid (pid {holder.pid}) did not stop within {timeout_s:.0f}s")
        orphans = list(self._recorded().values())
        for child in reversed(orphans):
            child.signal(signal.SIGINT)
        if self._wait_free(self.stop_timeout_s):
            return
        for child in orphans:
            child.signal(signal.SIGKILL, group=True)
        if not self._wait_free(10.0):
            raise TimeoutError(
                "the stack's lock is still held by a process that is not Prismoid's; see `prismoid status`"
            )

    def _wait_free(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while self.lock.taken():
            if time.monotonic() > deadline:
                return False
            time.sleep(_TICK_S)
        return True
