# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/recorder.py
"""The recording: one row per act, its picture
beside it, and a self-contained index harvest reads as one file - written through a sink, so the
daemon's folder under `/artifacts` (`DirSink`) and a worker writing into a frame (`frame_sink.py`) keep one
format. Evidence, never authority: a sink that fails loses a row, never the act. Stdlib only."""

from __future__ import annotations

import base64
import contextlib
import html
import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from .verbs import ACTING_VERBS


class RecordingSink(Protocol):
    """Where a recording's files go: one folder at a time."""

    @property
    def ready(self) -> bool:
        """A folder is open and may be written; False: recording is disabled."""
        ...

    async def open(self, folder: str) -> bool:
        """Record into `folder` from here on, letting go of the last one; False when it is refused."""
        ...

    async def read(self, name: str) -> bytes | None:
        """A file of the open folder; None when it is absent, or unsafe to read."""
        ...

    async def create(self, name: str, data: bytes) -> None:
        """A NEW file in the open folder: never an overwrite, never through a link."""
        ...

    async def replace(self, name: str, text: str) -> None:
        """(Re)write one file of the open folder atomically."""
        ...

    async def seal(self) -> None:
        """Open the folder for harvest."""
        ...


def _safe_basename(name: str) -> bool:
    """A plain filename `DirSink` may open in the recording directory: no path separator, not
    `.`/`..`, not empty - never a name that could walk anywhere but straight into that directory."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


class DirSink:
    """The daemon's sink. `SIDECAR_RECORDING` lives under `/artifacts`, which the AGENT uid (1000)
    OWNS - so before this daemon (uid 1001) ever runs, the agent can pre-create `recording-N` as a
    symlink (this process would then write through it, as 1001, wherever it points) or as a
    plain directory it owns (this process would then be writing files an attacker-controlled
    uid can read/replace); and since the sticky bit never binds a directory's owner, it can
    rename the approved folder away and plant a symlink at its name at ANY later moment.

    So the folder is opened exactly once (`O_DIRECTORY|O_NOFOLLOW`), its `fstat` must say a
    directory this uid owns, and every later write, rename, chmod and listing is relative to
    that fd (`dir_fd=`, `fchmod`): what happens to the NAME afterwards redirects nothing.
    Refuse silently: recording is evidence, never authority, so a hostile or merely surprising
    recording directory disables recording - it must never crash startup or refuse to serve
    commands."""

    def __init__(self) -> None:
        self._fd: int | None = None

    @property
    def ready(self) -> bool:
        return self._fd is not None

    async def open(self, folder: str) -> bool:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.makedirs(folder, exist_ok=True)
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return False
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            self._fd = fd
            return True
        os.close(fd)
        return False

    async def read(self, name: str) -> bytes | None:
        """The bytes of a recorded file (an action's image, `actions.json`), or `None` if `name`
        is unsafe (not a plain basename - no `/`, not `.`/`..`) or the open/read hits anything
        but a plain regular file. `O_NOFOLLOW`: the daemon (uid 1001) trusts its OWN writes in
        this directory, never a name an agent (uid 1000) might have planted there before this
        ever ran - same law `create`'s own `O_EXCL|O_NOFOLLOW` open already lives by."""
        if self._fd is None or not _safe_basename(name):
            return None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)

    async def create(self, name: str, data: bytes) -> None:
        # O_EXCL|O_NOFOLLOW: this filename is new (seq only ever increases) and must never follow
        # a symlink planted at it - the approval covers the directory, not every future name in it.
        if self._fd is None:
            return
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self._fd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    async def replace(self, name: str, text: str) -> None:
        """Atomically (re)write one file in the recording folder: a fixed tmp name, written
        `O_NOFOLLOW` so a planted symlink at that name is refused rather than followed, then
        `os.replace` onto the real name - `rename(2)` replaces the destination DIRECTORY ENTRY
        atomically and never dereferences it, so a symlink already sitting at the name is swapped
        out, not written through. `O_EXCL` on top: a stale tmp file from an interrupted run is a
        planted-symlink lookalike either way, so it is unlinked (never followed - `unlink`
        removes the directory entry, not what it points at) and the open retried exactly once."""
        recording_fd = self._fd
        if recording_fd is None:
            return
        tmp = f"{name}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(tmp, flags, 0o644, dir_fd=recording_fd)
        except FileExistsError:
            os.unlink(tmp, dir_fd=recording_fd)
            fd = os.open(tmp, flags, 0o644, dir_fd=recording_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, name, src_dir_fd=recording_fd, dst_dir_fd=recording_fd)

    async def seal(self) -> None:
        """Opens the recording folder up for harvest: chmod the directory `0755` and every regular
        file in it `0644` (this daemon runs as uid 1001; harvest reads as a different uid). The
        directory the daemon checked, by fd - never whatever its name points at now - and only
        the regular files in it."""
        if self._fd is None:
            return
        os.fchmod(self._fd, 0o755)
        for name in os.listdir(self._fd):
            self._chmod_if_regular_file(name, 0o644)

    def _chmod_if_regular_file(self, name: str, mode: int) -> None:
        """`os.chmod(path, mode)` follows a symlink at `path` straight to whatever it points at -
        never safe on a name this uid did not necessarily create. Open `O_NOFOLLOW` first (a
        symlink there makes the open itself fail) and `fchmod` the resulting fd, so only a real
        regular file already sitting at that name is ever touched."""
        if self._fd is None:
            return
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
        except OSError:
            return
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.fchmod(fd, mode)
        finally:
            os.close(fd)


class Recorder:
    """A step's recording: `actions.json` (one row per act, and per screenshot the model asked for),
    each row's picture, and at `finalize` a self-contained `index.html`. The rows name an act's
    target from the snapshot the model acted on (`remember_targets`), never from the page after."""

    def __init__(self, sink: RecordingSink, folder: str) -> None:
        self.sink = sink
        self.folder = str(PurePosixPath(folder))
        self._actions: list[dict[str, Any]] = []
        self._seq = 0
        self._targets: dict[str, str] = {}  # ref -> 'role "name"' from the last snapshot

    @property
    def recording(self) -> bool:
        return self.sink.ready

    @property
    def targets(self) -> Mapping[str, str]:
        return dict(self._targets)

    async def open(self, folder: str | None = None) -> bool:
        """Record into `folder` (None: the one this recorder names) from here on: at startup, and
        whenever a warm Machine's next step (or a retry) names its own (`record`). The folder's own
        trail is picked up if it has one (a daemon restarted under the same step); the previous
        folder needs no flush, `row` writes every row as it lands. False when the sink refuses the
        folder: nothing is recorded until the next `open`."""
        if folder is not None:
            self.folder = str(PurePosixPath(folder))
        self._actions = []
        if await self.sink.open(self.folder):
            saved = await self.sink.read("actions.json")
            if saved is not None:
                try:
                    self._actions = json.loads(saved)
                except json.JSONDecodeError:
                    self._actions = []
            else:
                # Recording is evidence, never authority - same law as `row` below.
                with contextlib.suppress(Exception):
                    await self._write_actions()
        self._seq = max((int(row.get("seq", 0)) for row in self._actions), default=0)
        return self.sink.ready

    def forget_targets(self) -> None:
        """The page moved to another tab, or the browser restarted: no snapshot's names apply."""
        self._targets = {}

    def remember_targets(self, targets: Mapping[str, Sequence[str]]) -> None:
        """A snapshot, or an action that brought the page back: the next act's target is named
        from the `(role, name)` its look gave each reference, never parsed back out of its text."""
        self._targets = {ref: f'{role} "{name}"' if name else role for ref, (role, name) in targets.items()}

    async def file(
        self,
        verb: str,
        cmd: Mapping[str, Any],
        result: Mapping[str, Any],
        record: Mapping[str, Any] | None,
    ) -> None:
        """One verb's rows. A screenshot the model asked for is filed at full size (it is not an
        act - never in ACTING_VERBS - but the spec wants every one filed; `png_b64` stays in the
        result, the model needs it). An act files its row with its capture - and a failed capture
        (`{"capture": "failed"}`) still files one: the action happened and the journal counted
        it; only the picture is missing."""
        if verb == "screenshot" and result.get("ok"):
            png = base64.b64decode(result["png_b64"])
            await self.row(verb, "", png, "png", result.get("url", ""))
        if record is not None and verb in ACTING_VERBS:
            target = self._targets.get(str(cmd.get("ref", "")), str(record.get("target", "")))
            jpeg = record.get("jpeg")
            data = bytes(jpeg) if isinstance(jpeg, bytes | bytearray) and jpeg else None
            await self.row(verb, target, data, "jpg", result.get("url", ""))

    async def row(self, verb: str, target: str, data: bytes | None, ext: str, url: str) -> None:
        """File one action row (and its image). `data=None` is a failed capture: the row still
        lands - `file: ""`, `capture: "failed"` - so the count stays truthful; the verifier's
        image check skips it by name and says how many captures failed."""
        if not self.sink.ready:
            return
        self._seq += 1
        try:
            name = ""
            if data is not None:
                name = f"{self._seq:04d}-{verb}.{ext}"
                await self.sink.create(name, data)
            parts = urlsplit(url)
            self._actions.append(
                {
                    "seq": self._seq,
                    "verb": verb,
                    "target": target,
                    "host": parts.hostname or "",
                    "path": parts.path,
                    "at": datetime.now(UTC).isoformat(),
                    "file": name,
                    **({} if data is not None else {"capture": "failed"}),
                }
            )
            await self._write_actions()
        except Exception:
            # Recording is evidence, never authority: a frame or a journal write that fails must
            # never fail the action itself.
            pass

    async def finalize(self) -> dict[str, Any]:
        """Writes a self-contained `index.html` (each thumbnail inline as a `data:` URI - no
        cross-file references, so harvest reads one file and gets the whole story) and seals the
        folder for harvest. Every file this reads is read through the sink's no-follow rule -
        recording is evidence, never authority, and that includes trusting a name in this
        directory to be what `row` itself put there."""
        if not self.sink.ready:
            return {"ok": False, "error": "recording folder unavailable"}
        rows: list[str] = []
        for a in self._actions:
            data = await self.sink.read(str(a.get("file") or ""))
            src = ""
            if data is not None:
                mime = "image/png" if str(a.get("file", "")).endswith(".png") else "image/jpeg"
                src = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            rows.append(
                "<li><figure>"
                + (f'<img src="{src}" alt="">' if src else "")
                + ("<i>(capture failed)</i>" if a.get("capture") == "failed" else "")
                + f"<figcaption><b>{html.escape(str(a.get('verb', '')), quote=False)}</b> "
                + f"{html.escape(str(a.get('target', '')), quote=False)} · "
                + f"{html.escape(str(a.get('host', '')), quote=False)}"
                + f"{html.escape(str(a.get('path', '')), quote=False)} · "
                + f"{html.escape(str(a.get('at', '')), quote=False)}</figcaption></figure></li>"
            )
        body = "<ol>" + "".join(rows) + "</ol>" if rows else "<p>no actions were recorded</p>"
        doc = (
            "<!doctype html><meta charset=utf-8><title>Recording</title>"
            "<style>body{font:14px system-ui;margin:16px}img{max-width:640px;display:block}"
            "figcaption{margin:4px 0 16px}</style>"
            f"<h1>Recording · {len(rows)} action(s)</h1>{body}"
        )
        await self._write_actions()
        await self.sink.replace("index.html", doc)
        await self.sink.seal()
        # `acting`: how many rows are real ACTS (a screenshot is filed but acts on nothing) -
        # the engine delivers a recording only when at least one act happened.
        acting = sum(1 for a in self._actions if str(a.get("verb", "")) in ACTING_VERBS)
        return {"ok": True, "actions": len(rows), "acting": acting, "path": self.folder}

    async def _write_actions(self) -> None:
        await self.sink.replace("actions.json", json.dumps(self._actions))
