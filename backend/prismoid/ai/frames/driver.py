# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The frame driver - a Protocol, so the sandbox technology is swappable.

Every caller speaks this interface and nothing else, so a new provider is one driver file and no
agent notices.

**Every `exec` is journalled** as a `tool.called` event - including the ones that
were refused. An egress denial that is invisible is an egress denial that gets debugged for an
hour; one that shows up on the flight recorder as `tool.called ok=false` is a five-second fix.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, ClassVar, Final, Literal, Protocol, runtime_checkable

from prismoid.ai.frames.layout import FramePaths
from prismoid.schemas.device import FolderGrant, InterruptedOut

# Who owns a file the system copies into the frame. **The two are not interchangeable.**
#
#   "agent"  - its workshop. It must be able to edit what it wrote, with a shell as well as a tool.
#   "system" - what it was *given*: sources, the plan, answers. Read-only, and root's.
#
# The distinction is load-bearing for verification, not for tidiness: the document suite asks "does
# this number appear in a source?", so a source the agent can write to makes the answer always yes.
Owner = Literal["agent", "system"]


class EgressUnavailable(RuntimeError):
    """The frame was born without an egress principal, so its allowlist can never be widened. The
    Machine must be re-created with the list. Which frames are born so is on each driver's
    `create`."""


class FrameUnreachable(ConnectionError):
    """The provider could not say whether a frame runs: an unreadable state, a reply that is not its
    record, a daemon or an API that did not answer. Deliberately NOT a RuntimeError: the lifecycle
    reads RuntimeError from `bind()` as "the frame is gone, rebuild it", and a transient provider
    error must cost one infra attempt, never a Machine's unsaved work. Every driver raises this one."""


# The most a driver reads of one file into the worker, for a model's read and a harvested artifact
# alike: a task can make a file of any apparent size (`truncate -s 64G` costs it no disk), and the
# worker that reads it serves every tenant.
READ_MAX_BYTES: Final = 64 * 1024 * 1024


class FileTooLarge(ValueError):
    """A file past the read cap, refused instead of buffered whole. A `ValueError`, so a tool's
    refusal path hands the model this message, which names the way to read part of it."""

    def __init__(self, path: str, limit: int) -> None:
        super().__init__(
            f"{path} is over {limit:,} bytes, more than one read takes - read part of it with the "
            "shell (head, tail, sed -n) or split it"
        )
        self.path, self.limit = path, limit


async def read_cap(path: str, read: Callable[[int], Awaitable[bytes]]) -> bytes:
    """`read(limit)` asked for one byte past `READ_MAX_BYTES`, never the whole file; a file past
    the cap raises `FileTooLarge`. A driver whose daemon takes a read length reads through this."""
    data = await read(READ_MAX_BYTES + 1)
    if len(data) > READ_MAX_BYTES:
        raise FileTooLarge(path, READ_MAX_BYTES)
    return data


class ArchiveTooLarge(Exception):
    """An `archive` passed its `limit`: `seen` is how many bytes it read before it gave up. The
    checkpoint store reads it as its own `Oversize` (`ai/` never imports `operations/`)."""

    def __init__(self, seen: int) -> None:
        super().__init__(f"the archive passed {seen} bytes")
        self.seen = seen


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class RunsSystemPython(Protocol):
    """A frame whose system Python is not the agent's (the device): the image's `python3` is the
    system's own on a cloud frame, but on the user's Mac it would be theirs, so the system's scripts
    go through this door instead. Paths are given apart from the command so that the frame can spell
    and quote them; `stdin` is a file."""

    async def system_python(
        self,
        script: str,
        *args: str,
        stdin: str | None = None,
        timeout: float = 120.0,  # noqa: ASYNC109
    ) -> ExecResult: ...


@runtime_checkable
class FrameDriver(Protocol):
    """One sandbox, one task. The frame IS the computer."""

    sleep_kind: ClassVar[Literal["archive", "native", "none"]]
    """How this provider sleeps (ADR-063): `none` - nothing is archived; sleep keeps the frame and
    wake binds it; `archive` - `operations/machines` composes sleep and wake from
    `freeze`/`archive`/`destroy`/`create`/`unpack` (`Checkpoints`); `native` - the provider keeps the
    frame's memory: `suspend` after the archive, `resume` at the wake (`NativeSleep`). A native
    driver is `Checkpoints` too: a fork, and a wake after the memory is gone, always need the
    portable `/work` tar. `operations/machines` reads this in exactly two places, the sleep and the
    wake."""

    driver: ClassVar[str]
    """This driver's name, as `PRISMOID_WORKER_FRAME_DRIVER` and a Machine row's `driver` spell it: what
    a wake records on the row that names this frame, and what the screen door and the meter read."""

    egress_version: int
    egress_hosts: list[str]

    @property
    def ref(self) -> str:
        """The provider's opaque reference to this frame. What the `machines` row stores in
        `frame_ref`."""
        ...

    @property
    def recording_dir(self) -> str:
        """The folder this frame's browser records the current step into, `recording-<tag>`: where
        a `Recorder` writes and what `finalize` indexes. The agent may read it and never writes it."""
        ...

    paths: FramePaths
    """Where this frame's virtual roots (`/work`, `/task`, ...) really are - like `recording_dir`,
    a fact the driver owns. Cloud drivers are `STANDARD_PATHS` (the path is the virtual path)."""

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        """Boot the sandbox with **no default network route** - only the egress path, which knows
        only `allow_domains` (versioned `egress_version`). A frame born without an egress principal
        can never be widened: its `allow` raises `EgressUnavailable`. `labels` let the reconciler
        judge it."""
        ...

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        """Run `command` as the agent's uid in `/work`, through a shell that reads NO startup
        file. The system's own commands (the manifest read, the recording copy, a skill's mkdir, the
        publish and preview runners) and the agent's `shell` share this door, unless the frame gives
        the agent one of its own (`agent_exec`), and HOME is `/work`, the agent's: a login shell
        would run a `.bash_profile` the agent wrote inside every one of the system's commands."""
        ...

    async def write_file(self, path: str, content: str) -> None: ...
    async def read_file(self, path: str) -> str: ...
    async def read_bytes(self, path: str) -> bytes:
        """The **binary** read - `read_file` is `cat` and mangles non-UTF-8 bytes. A harvested `.xlsx`
        or `.pptx` must come out through here, or its hash will not match what was published."""
        ...

    async def list_dir(self, path: str) -> list[str]:
        """Entry names, directories with a trailing `/`. A directory that is not there lists as
        `[]` (every driver, and the fake frame)."""
        ...

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent") -> None:
        """Bytes, a mode, and an owner. **An attachment is not always text** - a source PDF that
        round-trips through `str` is corrupted into something that still looks like a file, and
        only the parser three steps later finds out. `owner="system"` + `mode=0o444` is how a
        source stays a source: an agent that can rewrite what it was given can launder a
        hallucination into a verified fact."""
        ...

    async def destroy(self) -> None: ...

    async def close(self) -> None:
        """Let go of what this process holds open on the frame, such as a browser session opened on
        a step's first browser verb and closed when the step releases the Machine. Not a
        lifecycle verb - the frame runs on, unchanged - and it never raises. A driver that holds
        nothing past a call does nothing."""
        ...

    async def bind(self, *, start: bool = True) -> bool:
        """Attach to the frame named `ref` - a Machine's frame between steps, or a frame a parked
        step left. A frame the provider still has but that is not running (a host or daemon
        restart, an operator's stop) is STARTED and bound, and the call answers True: only its
        processes are lost - a browser page among them - while `/work`, `/browser` and every file
        it wrote are still its own, and a caller that took it for gone would throw away everything
        since the last checkpoint. A started frame may have a new address: it reaches nothing until
        its caller pushes the list (`allow`). `start=False` binds only a running frame - for a
        caller that would only narrow or thaw one, which a restart has already made moot.

        Raises `RuntimeError` only when the provider says the frame is gone, or there and unable
        to run (or not running, with `start=False`) - then the caller wakes the Machine from its
        checkpoint. The rule for any driver: a provider that cannot say (an unreadable state, a
        reply that is not its record, an Engine that did not answer) raises something other than
        RuntimeError (`FrameUnreachable`), which costs one infra attempt and never a
        rebuild. A record from a provider that reports no state at all binds as running."""
        ...

    async def reset_mounts(self) -> None:
        """Take away what the system mounted for the last step - `/task/inputs`, the instruction
        files beside it (`layout.INSTRUCTIONS`) and `/skills` - before a step in a Machine mounts
        its own: a later step, turn or author must never read an earlier one's sources, memory or
        draft skills. `/task` stays root's, and the agent can write there at no point. A provider
        whose exec cannot run as root removes root's files through a route of its own, never an
        exec. Raises when the mounts are still there."""
        ...

    async def freeze(self) -> None:
        """SIGSTOP every process the agent uid owns. An archive taken while frozen is
        crash-consistent, the way a power cut is (SQLite survives that by design)."""
        ...

    async def thaw(self) -> None:
        """SIGCONT every process the agent uid owns."""
        ...

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        """REPLACE the running frame's egress allowlist. Unpinned (`version=None`), the push is one
        past the driver's synced `egress_version` - or, when the driver has synced none
        (`egress_version == 0`, e.g. just after `bind()`), an UNVERSIONED push (`"version": null`)
        that the proxy applies without touching its version, so the row's version stays the truth.
        A versioned push not newer than the driver's own version, or refused by the proxy as stale
        (409), returns False, is logged, and is never retried by the driver. On a 409 the driver
        adopts the version the transport says it holds, so a caller that owns the list can push
        once past it (`operations/machines`' holder does; a pinned push that lost stays lost). Raises
        `EgressUnavailable` when the frame was born without an egress principal (it can never be
        widened), and RuntimeError when the frame is gone (never created, destroyed, or the control
        plane no longer knows it): the caller rebuilds. How a driver reads its control plane's other
        refusals is on its own `allow`."""
        ...

    async def manifest(self) -> dict[str, Any]: ...

    async def denials(self) -> list[dict[str, str]]:
        """What the egress path refused for this frame - journalled as `tool.called ok=false`.
        A frame with no egress path returns an empty list; an invisible denial is one debugged for
        an hour."""
        ...

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ...) -> None:
        """**The only door**. Nothing outside `/artifacts` + the manifest is
        harvestable, so `publish` is not a convenience method - it is the entire boundary between
        "a file the agent made" and "a deliverable the product will sign"."""
        ...

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        """One command to the frame's browser, answered as one JSON result. A frame without the
        browser profile answers `{"ok": False, "error": "no browser in this frame"}`.
        Every error is a result; the caller journals keys, never the command. `timeout` defaults
        to `PRISMOID_COMPUTER_USE_COMMAND_TIMEOUT_S`."""
        ...


@runtime_checkable
class Checkpoints(Protocol):
    """A frame whose `/work` and `/browser` can leave it as a tar (`sleep_kind` `archive` and
    `native`). A `none` frame has none of this: `/work` is not the frame's to carry."""

    async def archive(self, path: str, out: IO[bytes], *, limit: int | None = None) -> None:
        """Write a plain tar of one directory (`/work` or `/browser`) into `out`, through
        `archives.checkpoint_filter`. An absent directory writes an empty archive. Past `limit`
        bytes of the provider's stream it stops reading and raises `ArchiveTooLarge` - counted as
        the stream arrives, so a driver that spools before it writes never holds more than one
        chunk past the limit."""
        ...

    async def unpack(self, path: str, src: IO[bytes]) -> None:
        """Extract a plain or gzip tar of one directory at `/`, through
        `archives.restore_filter` - the trust boundary."""
        ...


@runtime_checkable
class NativeSleep(Protocol):
    """A provider that keeps a sleeping frame's memory (`sleep_kind = "native"`). Only such a driver
    has these verbs."""

    async def suspend(self) -> None:
        """Keep the frame's memory - its processes, its open pages - with the row still naming it.
        Called by `operations/machines` only, after `/work` is archived and the frame thawed.
        Idempotent: a frame already suspended stays so (DBOS replays a step)."""
        ...

    async def resume(self) -> None:
        """Wake a natively suspended frame; the caller binds it next. RuntimeError when the
        provider says the frame is gone (the wake then rebuilds from the checkpoint), as `bind`'s
        rule. Idempotent: a frame already running stays so (DBOS replays a step)."""
        ...


@runtime_checkable
class StagesCopies(Protocol):
    """A frame whose engine stages a copy somewhere other than `/work`, which here is the user's
    real folder (the step's recording). Only the device driver has one."""

    @property
    def staging_dir(self) -> str | None:
        """The folder to stage in; None before `create` or `bind` names it."""
        ...


@runtime_checkable
class NamesItsDevice(Protocol):
    """A frame on a computer of the user's: what the Machine's row calls it, once a `create` or
    `bind` has said. Only the device driver has one."""

    device_name: str


@runtime_checkable
class KeepsQuarantine(Protocol):
    """A frame whose helper keeps what it moved out of the person's folders past the frame's end:
    a deleted task's `forget` removes it. Only the device driver has one."""

    async def forget_quarantine(self) -> None: ...


@runtime_checkable
class StopsServices(Protocol):
    """A frame that is kept through its sleep (`sleep_kind = "none"`):
    the sleep stops what its task left running - background servers, watchers - and keeps the
    frame and its folders. Only the device driver has one; the sleep asks `isinstance` rather than
    every driver carrying a no-op."""

    async def stop_services(self) -> None: ...


@runtime_checkable
class KeepsJournal(Protocol):
    """A frame whose provider journals its commands: it names the
    command a worker or helper restart cut off, until the worker says it parked or resumed on it
    (`ack_interrupted`, which also retires every older entry still open).
    Only the device driver has one - on a cloud driver a recovered step starts a fresh attempt."""

    async def interrupted(self) -> InterruptedOut | None: ...

    async def ack_interrupted(self, exec_id: str | None = None) -> None: ...


@runtime_checkable
class TakesGrants(Protocol):
    """A frame on the user's own disk: it is told the task's folder and
    Docker grants, which Postgres holds, before its `create` or `bind`, and tells its helper. Only
    the device driver has one; a cloud frame is granted nothing."""

    def grant(self, folders: Sequence[FolderGrant], docker: bool) -> None: ...


@runtime_checkable
class SavesOutput(Protocol):
    """A frame that lands bytes an agent asked to keep, in the folders its deliverables are made
    in (a screenshot), and says where by a path the agent's own commands
    reach. Only the device driver has one: it keeps the cloud's browser tools as they are."""

    async def save_output(self, path: str, data: bytes) -> str: ...


@runtime_checkable
class HandsOver(Protocol):
    """A frame whose browser is on the person's own screen, so a hand-off gives them the window
    (ADR-078). Only the device driver has one: elsewhere a person drives a lease."""

    async def hand_over(self) -> None: ...


async def run_python(
    frame: FrameDriver,
    script: str,
    *args: str,
    stdin: str | None = None,
    timeout: float,  # noqa: ASYNC109
    interpreter: str = "python3",
) -> ExecResult:
    """Run one of the system's own scripts (the preview runner, a QA precheck, a verifier suite): a
    fixed command line on a cloud frame, the frame's own door where it has one."""
    if isinstance(frame, RunsSystemPython):
        return await frame.system_python(script, *args, stdin=stdin, timeout=timeout)
    line = " ".join((interpreter, script, *args))
    return await frame.exec(f"{line} < {stdin}" if stdin else line, timeout=timeout)
