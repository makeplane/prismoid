# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device frame driver: the user's own Mac as a task's Machine.

The worker talks to `prismoid device helper` (`prismoid/device/`) over its Unix socket; the helper fences
every command in Seatbelt and keeps every file through its file door. This is the `FrameDriver` on
the worker's side of that wire.

**The model sees real paths here** (`FramePaths.on_host`): prompts and tool docs are rendered with
them, so `write_file`, `read_file` and its `shell` arrive spelled really. The system still spells
the virtual roots (`/work`, `/task/inputs`, `/artifacts`, `/out`, the task's own `out/`, and
`/grants/<name>` for each other folder the task was granted). So every verb that takes a path takes
either spelling - a virtual path maps through `to_real`, a path already under one of the frame's real
roots is used as it is, anything else is `OutsideFrame` - and nothing that LEAVES the driver into a
journal, a manifest or a tool result carries a real path: refusals and spawn errors from the helper
are mapped back to virtual roots, and `manifest()` is virtual. A command is text, not a path:
`exec` is the system's and spells its roots, `agent_exec` is the model's and leaves its words alone.

A device is never "gone", only away: an unreachable socket, a refused key or a helper that errs is
`FrameUnreachable`; `RuntimeError` is only the helper saying it has never heard of the frame.

**The browser** is the task's own window in the Prismoid Chrome, driven from this process as
a remote driver's is (`remote_browser.RemoteBrowser`): the CDP connection is the helper's relay
(`/frames/{ref}/cdp`), dialled over its Unix socket with the key in the handshake header, and the
step's recording is written into the task's folder through the file door (`FrameSink`). It stays on
the device; only what the engine publishes from it leaves, as on every driver.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import posixpath
import re
import shlex
import uuid
from collections.abc import Iterable, Mapping, Sequence
from functools import partial
from typing import Any, ClassVar, Final, Literal

import httpx
import logfire

from prismoid.ai import config
from prismoid.ai.frames import layout
from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.ai.frames.device_client import (
    HELPER_HOST,
    KEY_HEADER,
    call,
    detail,
    helper_key,
    helper_name,
    helper_socket,
)
from prismoid.ai.frames.device_tar import tar_tree
from prismoid.ai.frames.driver import ExecResult, FrameUnreachable, Owner
from prismoid.ai.frames.image_browser.sidecar.cdp import CdpConnection
from prismoid.ai.frames.layout import EPOCHS, MANIFEST, FramePaths, OutsideFrame, to_real
from prismoid.ai.frames.product import artifact_target, manifest_entry
from prismoid.ai.frames.publish import validate_publish
from prismoid.ai.frames.remote_browser import (
    RemoteBrowser,
    close_session,
    remote_for,
    reserve_epochs,
    run_command,
)
from prismoid.errors import PolicyDenied
from prismoid.schemas.device import (
    RUNTIME_DIR,
    RUNTIME_MISSING,
    RUNTIME_SHOWN,
    RUNTIME_STAMP,
    SYSTEM_PYTHON,
    FolderGrant,
    FrameOut,
    FrameSpecIn,
    GrantsIn,
    InterruptedOut,
    runtime_python,
)
from prismoid.schemas.machines import LABEL_INSTANCE, LABEL_KIND, FrameInfo, FrameKind
from prismoid.schemas.outputs import ArtifactType

_TRUNCATED_NOTE = "[output truncated by the device]"
_MARGIN_S = 15.0  # what the HTTP call waits past a command's own timeout (the helper kills at it)
_BIG_S = 120.0  # a file up to the door's 64 MiB, base64-encoded, or a grant's first walk of its folder
_CLOSE_BOUND_S: Final = 10.0  # a browser session's close may not hold a step's teardown longer
# What only Prismoid's own Python reads: previews, the suites that verify them.
_NEEDS_RUNTIME: Final = (".docx", ".xlsx", ".pptx", ".pdf")
# Where a path may start and end in free text: a whole path, never the tail or the head of a longer name.
_PATH_START: Final = r"(?<![\w.-])"
_PATH_END: Final = r"(?=/|$|[^\w.-]|\.(?:\s|$))"
# After a root: nothing of the path follows, so the command names the folder itself. A trailing `/`
# counts; a glob does not (`/work*` and `/work/*` stay patterns).
_BARE: Final = re.compile(r"/?(?:$|[^/\w.*?\[-])")

__all__ = ["DeviceFrame", "helper_name", "inventory"]


class _TooLarge(OSError):
    """The file door's 413: a read or a write past its cap. Its text is the fix."""


def _spellings(roots: Iterable[str]) -> str:
    """`roots` as a regex alternation, longest first. A space may come backslash-escaped, as a model
    fluent in shell types it (`Application\\ Support`). Plain substitution still: nothing else of the
    command is read, and quotes need nothing, since the root is the same text inside them."""
    ordered = sorted(roots, key=len, reverse=True)
    return "|".join(re.escape(root).replace("\\ ", "\\\\? ") for root in ordered)


def _root_of[V](roots: Mapping[str, V], spelled: str) -> V:
    """What `roots` holds for a match of `_spellings`: as spelled, then unescaped, because a root's
    own name may hold a backslash before a space."""
    return roots[spelled] if spelled in roots else roots[spelled.replace("\\ ", " ")]


def _runtime_ready() -> bool:
    """Prismoid's own Python is built (its stamp is written last), whatever this Prismoid would build now:
    a stale one still reads a `.docx`, and only `prismoid up` replaces it."""
    home = config.current().local.home
    return (home / RUNTIME_DIR / RUNTIME_STAMP).is_file() and runtime_python(home).exists()


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class DeviceFrame:
    """One task's folder on the user's Mac. Satisfies `FrameDriver`."""

    sleep_kind: ClassVar[Literal["archive", "native", "none"]] = "none"
    driver: ClassVar[str] = "device"

    def __init__(
        self,
        task_id: str,
        profile: str = "base",
        attempt: int = 1,
        *,
        name: str | None = None,
        recording: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.task_id = task_id
        self.profile = profile
        self.attempt = attempt
        self._name = name or ""  # the helper names the frame: `device-{task_id}` for a Machine's
        self._recording = recording or str(attempt)
        self._transport = transport
        self.egress_version = 0
        self.egress_hosts: list[str] = []
        self.paths = FramePaths(on_host=True)  # replaced by the helper's own at create / bind
        self.device_name = ""
        self._browser: RemoteBrowser | None = None
        # The task's grants as the server holds them (`grant`); None: this frame was told none, and
        # a bind leaves the helper's as they are.
        self._grants: GrantsIn | None = None
        self._granted: tuple[str, ...] = ()  # the folders the helper holds for this frame
        self._aliases: dict[str, str] = {}  # real root -> its link, for a root that needs quoting

    @property
    def ref(self) -> str:
        return self._name

    @property
    def recording_dir(self) -> str:
        return f"{layout.TASK}/recordings/recording-{self._recording}"

    @property
    def staging_dir(self) -> str | None:
        """Where the engine stages a copy before publishing it (the step's recording,
        `execution._recording_stage`): `/out`, the task's own `out/` - Prismoid's folder on the device,
        never `/work`, the user's real folder. None before create / bind names it."""
        return layout.OUT if self.paths.out else None

    def grant(self, folders: Sequence[FolderGrant], docker: bool) -> None:
        """The task's folder and Docker grants, for the next `create` or `bind` to tell
        the helper (`TakesGrants`)."""
        self._grants = GrantsIn(grants=list(folders), docker=docker)

    def _adopt(self, out: FrameOut) -> None:
        w = out.paths
        self._name, self.device_name = out.ref, out.device_name
        self._granted = tuple(g.path for g in out.grants)
        self._aliases = dict(out.aliases)
        self.paths = FramePaths(
            task=w.task,
            work=w.work,
            artifacts=w.artifacts,
            skills=w.skills,
            verify=w.verify,
            inputs=w.inputs,
            browser=w.browser,
            out=w.out,
            on_host=True,
            grants=tuple(w.grants.items()),
        )

    # ── paths: either spelling in, virtual out ────────────────────────────────────────────────

    def _roots(self) -> list[tuple[str, str]]:
        """(virtual, real) for every root, and again for the real path the filesystem resolves it
        to (`/tmp` is `/private/tmp`): a command's own `pwd` prints that one."""
        pairs = self.paths.pairs()
        return [*pairs, *((v, os.path.realpath(r)) for v, r in pairs if os.path.realpath(r) != r)]

    def _real(self, path: str) -> str:
        """`path` as the device has it. A virtual path maps through `to_real`; one already under a
        real root of this frame is kept; anything else is `OutsideFrame`. Normalised first, so a
        `..` cannot climb out of the root it was tested against."""
        clean = posixpath.normpath(path) if path.startswith("/") else path
        try:
            return to_real(clean, self.paths)
        except OutsideFrame:
            pass
        if any(_under(clean, real.rstrip("/")) for _v, real in self._roots()):
            return clean
        raise OutsideFrame(f"{self.shown(path)} is in none of this task's folders")

    def _aliased(self, text: str) -> str:
        """`text` with each root that needs quoting spelled by its alias (`frames.aliases`). Plain
        substitution at a path boundary, longest first, so nothing parses the shell and a path holds
        wherever the command put it. A bare root goes out as `alias/.`: the alias is a link, and
        `find`, `grep -r`, `ls -l`, `du`, `tar` and `cp -R` act on a link they are named, not on
        the folder behind it. A folder whose name extends a root's by a space (`~/My App` and
        `~/My App 2`) cannot be told apart here, and a command naming it fails loudly."""
        spelled = {s: alias for real, alias in self._aliases.items() for s in (real, os.path.realpath(real))}
        if not spelled:
            return text

        def swap(root: re.Match[str]) -> str:
            alias = _root_of(spelled, root.group(0))
            return f"{alias}/." if _BARE.match(text, root.end()) else alias

        return re.sub(f"{_PATH_START}(?:{_spellings(spelled)}){_PATH_END}", swap, text)

    def _unaliased(self, text: str) -> str:
        """`text` with the aliases folded back, `alias/.` first: `find` prints `alias/./x`."""
        for real, alias in self._aliases.items():
            text = re.sub(rf"{re.escape(alias)}(?:/\.(?=/|$|[^\w./-]))?", lambda _m, r=real: r, text)
        return text

    def virtual_text(self, text: str) -> str:
        """`text` with this frame's real roots spelled virtual - a helper's refusal, a spawn error,
        before it reaches a tool result or a journal."""
        text = self._unaliased(text)
        virtual = {real.rstrip("/"): v for v, real in self._roots() if real.rstrip("/")}
        if not virtual:
            return text
        pattern = f"{_PATH_START}(?:{_spellings(virtual)}){_PATH_END}"
        return re.sub(pattern, lambda m: _root_of(virtual, m.group(0)), text)

    def _entry(self, path: str) -> str | None:
        """A path in a manifest, as the agent knows it: a real one under a root of this frame (in
        either of its spellings) is spelled virtual, a virtual or relative one is kept. A real path
        under no root is None, and dropped: nothing stored holds a path of the user's disk."""
        if not path.startswith("/"):
            return path
        clean = posixpath.normpath(path)
        for virtual, real in sorted(self._roots(), key=lambda r: len(r[1]), reverse=True):
            if _under(clean, real.rstrip("/")):
                return virtual + clean[len(real.rstrip("/")) :]
        if any(_under(clean, virtual) for virtual, _real in self.paths.pairs()):
            return clean
        return None

    def _entries(self, paths: Sequence[object]) -> list[object]:
        return [e for p in paths if (e := self._entry(p) if isinstance(p, str) else p) is not None]

    def _refusal(self, response: httpx.Response, path: str) -> Exception:
        """The file door's 4xx as the error a driver raises, its detail mapped to virtual roots."""
        what = self.virtual_text(f"{path}: {detail(response)}")
        match response.status_code:
            case 403:
                return PermissionError(what)
            case 404:
                return FileNotFoundError(what)
            case 413:
                return _TooLarge(f"{what} - past the device's file limit: make it smaller or split it")
            case _:
                return ValueError(what)

    # ── lifecycle ─────────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        task_id: str | None = None,
        allow_domains: list[str] | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        egress_version: int = 1,
    ) -> None:
        stamped = dict(labels or {})
        instance = config.current().worker.instance
        if stamped or instance:
            stamped.setdefault(LABEL_INSTANCE, instance)
        kind = FrameKind(stamped[LABEL_KIND]) if LABEL_KIND in stamped else FrameKind.SEAT
        domains = sorted(allow_domains or [])
        granted = self._grants or GrantsIn()
        spec = FrameSpecIn(
            task_id=task_id or self.task_id,
            kind=kind,
            grants=granted.grants,
            docker=granted.docker,
            allow_domains=domains,
            egress_version=egress_version,
            profile="browser" if self.profile == "browser" else "base",
            labels=stamped,
        )
        response = await call(
            self._transport, "POST", "/frames", spec.model_dump(mode="json"), timeout=_BIG_S
        )
        if response.status_code != 200:
            raise RuntimeError(f"the device helper refused the frame: {self.virtual_text(detail(response))}")
        self._adopt(FrameOut.model_validate(response.json()))
        self.egress_version, self.egress_hosts = egress_version, domains

    async def bind(self, *, start: bool = True) -> bool:
        """The helper knows the ref -> True. It never says no for a stopped frame (a frame has no
        stopped state on the device), so `start` changes nothing; True is "bound, push the list".
        Grants the frame was told (`grant`) that differ from the helper's are pushed here: a grant
        added or revoked since the last step reaches the frame at the next bind."""
        response = await call(self._transport, "GET", f"/frames/{self._name}")
        if response.status_code == 404:
            raise RuntimeError(f"no device frame {self._name!r}")
        if response.status_code != 200:
            raise FrameUnreachable(f"the device helper answered {response.status_code} for {self._name!r}")
        out = FrameOut.model_validate(response.json())
        if self._grants is not None and (out.grants, out.docker) != (
            self._grants.grants,
            self._grants.docker,
        ):
            response = await call(
                self._transport,
                "PUT",
                f"/frames/{self._name}/grants",
                self._grants.model_dump(mode="json"),
                timeout=_BIG_S,
            )
            if response.status_code == 404:
                raise RuntimeError(f"no device frame {self._name!r}")
            if response.status_code != 200:
                raise FrameUnreachable(
                    f"the device helper refused the grants: {self.virtual_text(detail(response))}"
                )
            out = FrameOut.model_validate(response.json())
        self._adopt(out)
        return True

    async def destroy(self) -> None:
        if not self._name:
            return
        # Its containers stop and its folders get a last full walk before the frame goes.
        response = await call(self._transport, "DELETE", f"/frames/{self._name}", timeout=_BIG_S)
        if response.status_code not in (204, 404):
            raise RuntimeError(
                f"the device helper would not destroy {self._name!r}: {self.virtual_text(detail(response))}"
            )
        self.egress_version, self.egress_hosts = 0, []

    async def close(self) -> None:
        """The step let go (`FrameDriver.close`): its browser session ends; the window stays."""
        browser, self._browser = self._browser, None
        await close_session(browser, bound=_CLOSE_BOUND_S, log=logfire, label="device", ident=self._name)

    async def reset_mounts(self) -> None:
        response = await call(self._transport, "POST", f"/frames/{self._name}/reset_mounts", {})
        if response.status_code != 204:
            raise RuntimeError(f"could not reset the mounts: {self.virtual_text(detail(response))}")

    async def _signal(self, name: Literal["stop", "cont"]) -> None:
        response = await call(self._transport, "POST", f"/frames/{self._name}/signal", {"signal": name})
        if response.status_code == 404:
            raise RuntimeError(f"no device frame {self._name!r}")

    async def freeze(self) -> None:
        await self._signal("stop")

    async def thaw(self) -> None:
        await self._signal("cont")

    async def stop_services(self) -> None:
        """The sleep's whole work on a device: kill what the task left running, keep
        the frame and its folders. A frame the helper has never heard of has nothing running."""
        response = await call(
            self._transport, "POST", f"/frames/{self._name}/stop-services", {}, timeout=_BIG_S
        )
        if response.status_code not in (204, 404):
            raise RuntimeError(f"could not stop the frame's services: {self.virtual_text(detail(response))}")

    async def forget_quarantine(self) -> None:
        """The task was deleted: what its frames moved out of the person's folders goes too."""
        response = await call(self._transport, "DELETE", f"/frames/{self._name}/quarantine")
        if response.status_code not in (204, 404):
            raise RuntimeError(f"could not forget the frame's quarantine: {detail(response)}")

    # ── egress ────────────────────────────────────────────────────────────────────────────────

    async def allow(self, domains: list[str], version: int | None = None) -> bool:
        """The versioned replace of `FrameDriver.allow`. A 409 carries the version the helper holds,
        which the driver adopts."""
        if version is None and self.egress_version == 0:
            if not await self._put_allow(domains, None):
                return False
            self.egress_hosts = sorted(domains)
            return True
        version = self.egress_version + 1 if version is None else version
        if version <= self.egress_version:
            return False
        if not await self._put_allow(domains, version):
            return False
        self.egress_version, self.egress_hosts = version, sorted(domains)
        return True

    async def _put_allow(self, domains: list[str], version: int | None) -> bool:
        response = await call(
            self._transport, "PUT", f"/frames/{self._name}/egress", {"domains": domains, "version": version}
        )
        if response.status_code == 409:
            held = response.json().get("version", 0)
            self.egress_version = max(self.egress_version, int(held or 0))
            return False
        if response.status_code == 404:
            raise RuntimeError(f"no device frame {self._name!r}")
        if response.status_code != 200:
            raise RuntimeError(
                f"the device helper refused the allowlist: {self.virtual_text(detail(response))}"
            )
        return True

    async def denials(self) -> list[dict[str, str]]:
        response = await call(self._transport, "GET", f"/frames/{self._name}/denials")
        if response.status_code != 200:
            return []
        return [dict(d) for d in response.json().get("denials", [])]

    # ── work ──────────────────────────────────────────────────────────────────────────────────

    async def exec(self, command: str, timeout: float = 120.0, *, cwd: str | None = None) -> ExecResult:  # noqa: ASYNC109
        """One of the system's commands, fenced, in the work folder (or `cwd`, either spelling). A
        virtual root in it (`find /work -newer /task/.step-start`) is spelled really: the system
        wrote it, so that text is a path wherever it stands."""
        return await self._command(self.paths.render(command), timeout, cwd)

    async def agent_exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        """The model's own command, fenced, in the work folder. It was told real paths, so `/task`
        here is its words: a route in a heredoc, a `sed` pattern, a URL's path, and any of them
        rewritten would land a path of this disk in the user's repository."""
        return await self._command(command, timeout)

    async def _command(self, text: str, timeout: float, cwd: str | None = None) -> ExecResult:  # noqa: ASYNC109
        """`text` run with each root that would need quoting spelled by its alias, whatever the
        command does with the path, inside quotes or out. Its output comes back spelled as the agent
        knows it, never the alias."""
        text = self._aliased(text)
        if cwd is not None:
            text = f"cd {shlex.quote(self._real(cwd))} && {text}"
        done = await self._run(text, timeout, system=False)
        return ExecResult(done.exit_code, self._unaliased(done.stdout), self._unaliased(done.stderr))

    async def system_python(
        self,
        script: str,
        *args: str,
        stdin: str | None = None,
        timeout: float = 120.0,  # noqa: ASYNC109
    ) -> ExecResult:
        """One of the system's scripts under Prismoid's own Python, never the user's: by absolute path,
        `-I` (nothing of the cwd, which is `out/`, the agent's, nor of the script's folder or the
        environment on its import path), inside the fence, because what it parses is the agent's.
        What it prints leaves spelled virtual: a suite's checks are stored."""
        if not _runtime_ready():
            return ExecResult(exit_code=127, stdout="", stderr=f"{RUNTIME_MISSING}\n")
        words = [f'"${SYSTEM_PYTHON}"', "-I", *(shlex.quote(self._real(p)) for p in (script, *args))]
        if stdin is not None:
            words += ["<", shlex.quote(self._real(stdin))]
        done = await self._run(" ".join(words), timeout, system=True)
        return ExecResult(done.exit_code, self._stored(done.stdout), self._stored(done.stderr))

    def _stored(self, text: str) -> str:
        """`text` as a stored check may hold it: frame roots virtual, and the runtime by its word,
        for a traceback through its site-packages. A path of the user's own toolchain stays."""
        runtime = config.current().local.home / RUNTIME_DIR
        text = self.virtual_text(text)
        for spelled in dict.fromkeys((str(runtime), os.path.realpath(runtime))):
            text = text.replace(spelled, RUNTIME_SHOWN)
        return text

    async def _run(self, text: str, timeout: float, *, system: bool) -> ExecResult:  # noqa: ASYNC109
        body = {"exec_id": uuid.uuid4().hex, "command": text, "timeout": timeout, "system": system}
        response = await call(
            self._transport, "POST", f"/frames/{self._name}/exec", body, timeout=timeout + _MARGIN_S
        )
        if response.status_code == 404:
            raise RuntimeError(f"no device frame {self._name!r}")
        if response.status_code != 200:  # the fence would not be safe: the command never ran
            return ExecResult(exit_code=126, stdout="", stderr=self.virtual_text(detail(response)) + "\n")
        out = response.json()
        stdout, stderr = str(out["stdout"]), str(out["stderr"])
        if out["exit_code"] == 126 or stderr.startswith("could not start the command"):
            stderr = self.virtual_text(stderr)  # a spawn error names a real path
        if out["truncated"]:
            stderr = stderr if stderr.endswith("\n") or not stderr else f"{stderr}\n"
            stderr += f"{_TRUNCATED_NOTE}\n"
        return ExecResult(exit_code=int(out["exit_code"]), stdout=stdout, stderr=stderr)

    async def write_file(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def write_bytes(
        self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent", confined: bool = False
    ) -> None:
        real = self._real(path)
        body: dict[str, object] = {
            "path": real,
            "data_b64": base64.b64encode(data).decode(),
            "mode": mode,
            "owner": owner,
        }
        if confined:
            body["confined"] = True
        response = await call(
            self._transport, "POST", f"/frames/{self._name}/files/write", body, timeout=_BIG_S
        )
        if response.status_code == 404 and detail(response) == "no such frame":
            raise RuntimeError(f"no device frame {self._name!r}")
        if response.status_code != 204:
            raise self._refusal(response, path)

    async def hand_over(self) -> None:
        response = await call(self._transport, "POST", f"/frames/{self._name}/hand-over")
        if response.status_code != 204:
            raise RuntimeError(f"the device helper refused a hand-off: {detail(response)}")

    async def save_output(self, path: str, data: bytes) -> str:
        """`data` at `path`, which the helper resolves: only the work folder or `out/` take it, so
        a link or a `..` into anywhere else, a read-only grant among them, is refused there. Returns
        the real path, the one the agent's own commands reach (`SavesOutput`)."""
        try:
            await self.write_bytes(path, data, confined=True)
            return self._real(path)
        except OutsideFrame:  # its text keeps a path of the user's disk, and a journal would too
            raise PermissionError("that path is in neither folder") from None

    async def read_file(self, path: str) -> str:
        return (await self.read_bytes(path)).decode(errors="replace")

    async def read_bytes(self, path: str) -> bytes:
        return await self._read(path)

    async def _read(self, path: str, *, nofollow: bool = False) -> bytes:
        body: dict[str, object] = {"path": self._real(path)}
        if nofollow:
            body["nofollow"] = True
        response = await call(
            self._transport, "POST", f"/frames/{self._name}/files/read", body, timeout=_BIG_S
        )
        if response.status_code != 200:
            raise self._refusal(response, path)
        return base64.b64decode(response.json()["data_b64"])

    async def list_dir(self, path: str) -> list[str]:
        real = self._real(path)
        response = await call(self._transport, "POST", f"/frames/{self._name}/files/list", {"path": real})
        if response.status_code == 404:
            return []  # a directory that is not there lists as []
        if response.status_code != 200:
            raise self._refusal(response, path)
        return [str(n) for n in response.json()["names"]]

    # ── publish ───────────────────────────────────────────────────────────────────────────────

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None:
        """The only door, through the system file door: the bytes land in `artifacts/` and the
        manifest gains an entry spelled with virtual paths. A directory travels as a tar of its tree.
        A source sits in the work folder or `out/` alike, and no process runs for it: `out/` is the
        agent's to write, and a runner started there would be one it could plant things for."""
        try:
            validate_publish(type, path, on_host=True)
        except PolicyDenied as e:
            raise PolicyDenied(self.virtual_text(str(e).replace(path, self.shown(path)))) from None
        kind = ArtifactType(type)
        if kind is ArtifactType.CHANGE and not self._in_out(path):
            raise PolicyDenied(
                f"{self.shown(path)} is not in the out folder, and a change is published only from "
                f"there: write the deliverable as a directory under {layout.OUT} and publish that"
            )
        if kind in _PROJECT_TYPES and self._touches_a_grant(path):
            raise PolicyDenied(
                f"{self.shown(path)} is the user's own folder, and it is never tarred: write the "
                f"change as a directory under {layout.OUT} and publish that as type 'change'"
            )
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(_NEEDS_RUNTIME) and not _runtime_ready():
            raise PolicyDenied(f"publish failed: {RUNTIME_MISSING}")
        target = artifact_target(name)
        data = await self._publishable(
            path, directory=kind is ArtifactType.CHANGE, skip_links=kind in _PROJECT_TYPES
        )
        try:
            await self.write_bytes(target, data, mode=0o644, owner="system")
        except FrameUnreachable:
            raise
        except (OSError, ValueError) as refused:
            raise PolicyDenied(f"publish failed: {refused}") from None
        shown = [e for p in sources if (e := self._entry(p)) is not None]
        entry = manifest_entry(target, kind, title, data, shown).model_dump(mode="json")
        manifest = await self._manifest_raw()
        manifest["entries"].append(entry)
        await self.write_bytes(MANIFEST, json.dumps(manifest, indent=2).encode(), owner="system")

    def shown(self, path: str) -> str:
        """`path` as a refusal or a label says it: virtual, its alias folded, or nothing of it when
        it is a path of the user's disk under no root of this frame. Both reach the journal."""
        return self._entry(self._unaliased(path)) or "that path"

    def _resolved(self, path: str) -> str | None:
        """Where `path` really goes, links followed, folded as APFS compares; None off the frame."""
        try:
            return os.path.realpath(self._real(path)).casefold()
        except OutsideFrame:
            return None

    def _in_out(self, path: str) -> bool:
        """`path` really lies under `out/` (a link into a grant does not)."""
        real = self._resolved(path)
        out = os.path.realpath(self.paths.out or "").casefold()
        return bool(real is not None and self.paths.out and _under(real, out))

    def _touches_a_grant(self, path: str) -> bool:
        """`path` is a folder the task granted, sits inside one, or holds one, wherever a link
        leads (the repo itself is never tarred). A frame with no folder grant has its
        own fresh work folder, and that is not the user's. A path off the frame touches nothing, and
        `_publishable` says so."""
        real = self._resolved(path)
        if real is None:
            return False
        folders = (os.path.realpath(folder).casefold() for folder in self._granted)
        return any(_under(real, folder) or _under(folder, real) for folder in folders)

    async def _publishable(self, path: str, *, directory: bool = False, skip_links: bool = False) -> bytes:
        """The bytes a publish carries. `directory`: only a tar `_tar` builds will do, so what the
        verifier and the viewer read is a tree of plain files, never a tar the agent wrote."""
        try:
            if names := await self._list_or_none(path):
                return await self._tar(path, names, skip_links=skip_links)
            if directory:
                raise PolicyDenied(f"publish failed: {self.shown(path)} must be a directory")
            return await self.read_bytes(path)
        except OutsideFrame:
            raise PolicyDenied(
                "publish failed: that path is in none of this task's folders - write it (e.g. under "
                "/work) first, then publish"
            ) from None
        except FrameUnreachable:  # an OSError by class, and no answer about the file at all
            raise
        except (PermissionError, _TooLarge) as refused:
            raise PolicyDenied(f"publish failed: {refused}") from None
        except (OSError, ValueError):
            raise PolicyDenied(
                f"publish failed: {self.virtual_text(path)}: no such file - write it (e.g. under /work) first, then publish"
            ) from None

    async def _list_or_none(self, path: str) -> list[str] | None:
        try:
            return await self.list_dir(path) or None
        except FrameUnreachable:
            raise
        except (OSError, ValueError):
            return None  # a file, or a folder the door will not list

    async def _tar(self, path: str, names: list[str], *, skip_links: bool = False) -> bytes:
        """A directory artifact (`tar_tree`), the door refusing every link it meets."""
        tarred, skipped = await tar_tree(
            path,
            names,
            list_or_none=self._list_or_none,
            read=partial(self._read, nofollow=True),
            skip_links=skip_links,
        )
        if skipped:
            logfire.info("publish left out {links} link(s) of a project", links=skipped)
        return tarred

    async def _manifest_raw(self) -> dict[str, Any]:
        try:
            raw = await self.read_bytes(MANIFEST)
        except FileNotFoundError:
            return {"entries": []}
        loaded = json.loads(raw or b'{"entries": []}')
        return {"entries": list(loaded.get("entries", []))}

    async def manifest(self) -> dict[str, Any]:
        """`/artifacts/manifest.json`, every path in it virtual. What harvest reads."""
        manifest = await self._manifest_raw()
        kept = []
        for entry in manifest["entries"]:
            if isinstance(entry.get("path"), str):
                if (path := self._entry(entry["path"])) is None:
                    continue  # under no root: not a deliverable, and never stored
                entry["path"] = path
            entry["sources"] = self._entries(entry.get("sources", []))
            kept.append(entry)
        manifest["entries"] = kept
        return manifest

    # ── the browser, driven from the worker ───────────────────────────────────────────────────

    async def browser(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        """One command, answered in this process (`RemoteBrowser`) over the helper's relay to the
        task's own window: the session opens on the step's first browser verb and closes when the
        step releases the Machine (`close`). Only a frame made with the browser profile has one (the
        helper refuses the relay for any other): a base frame answers without dialling. Every
        error is a result; a verb past `timeout` (default PRISMOID_COMPUTER_USE_COMMAND_TIMEOUT_S) is
        cut, said, and the next one reconnects."""
        if self.profile != "browser":
            return {"ok": False, "error": "no browser in this frame"}
        return await run_command(
            self._remote(),
            command,
            timeout,
            bound=_CLOSE_BOUND_S,
            log=logfire,
            label="device",
            ident=self._name,
        )

    def _remote(self) -> RemoteBrowser:
        if self._browser is None:
            self._browser = remote_for(self, self._dial, self._reserve_epochs)
        return self._browser

    async def _dial(self) -> CdpConnection:
        """The task's window through the helper's relay: over its Unix socket, the key in the
        handshake header - never a port, never the key in a URL."""
        return await WsCdp.connect_unix(
            helper_socket(), f"ws://{HELPER_HOST}/frames/{self._name}/cdp", headers={KEY_HEADER: helper_key()}
        )

    async def _reserve_epochs(self, block: int) -> int:
        """Reserve `block` snapshot epochs in the task's ledger (`remote_browser.reserve_epochs`):
        the window outlives the step, so a later session starts past every epoch an earlier one used."""
        return await reserve_epochs(self, EPOCHS, block, log=logfire, ident=self._name)

    # ── what only a device frame has ──────────────────────────────────────────────────────────

    async def interrupted(self) -> InterruptedOut | None:
        """The command a helper restart or a worker restart cut off, if any (B3's journal). Its head
        is the command as the device ran it, so its real roots are spelled virtual here: a gate and
        a prompt record it (a bound frame's roots; an unbound one's head is left as it is)."""
        response = await call(self._transport, "GET", f"/frames/{self._name}/interrupted")
        if response.status_code == 404:
            raise RuntimeError(f"no device frame {self._name!r}")
        if response.status_code == 204:
            return None
        cut = InterruptedOut.model_validate(response.json())
        return cut.model_copy(update={"command_head": self.virtual_text(cut.command_head)})

    async def ack_interrupted(self, exec_id: str | None = None) -> None:
        """The worker has parked or resumed on the cut-off command `exec_id` (None: the one reported
        now): it, and every older entry still open, stops being reported."""
        if exec_id is None:
            cut = await self.interrupted()
            exec_id = cut.exec_id if cut is not None else None
        if exec_id is not None:
            await call(self._transport, "POST", f"/frames/{self._name}/interrupted/ack", {"exec_id": exec_id})


# The types whose directory is a whole project: fine from a folder the frame made, never the user's.
_PROJECT_TYPES: Final = frozenset({ArtifactType.CODE, ArtifactType.WEBAPP})


async def inventory(*, transport: httpx.AsyncBaseTransport | None = None) -> list[FrameInfo]:
    """What the helper runs - the reconciler's listing. A helper that cannot answer raises
    `FrameUnreachable` (the reconciler reads a driver that cannot be listed as silent), never `[]`."""
    response = await call(transport, "GET", "/frames")
    if response.status_code != 200:
        raise FrameUnreachable(f"the device helper answered {response.status_code} to a listing")
    now = dt.datetime.now(dt.UTC)
    return [
        FrameInfo(ref=out.ref, labels=out.labels, created_at=out.created_at or now, driver=DeviceFrame.driver)
        for out in (FrameOut.model_validate(item) for item in response.json())
    ]
