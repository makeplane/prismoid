# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker's recording sink: a step's recording written
into the frame through the driver's own file door, for a browser the worker drives over CDP
(`remote_browser.RemoteBrowser`). The daemon's sink (`sidecar.recorder.DirSink`) holds a folder by
fd inside the frame; this one holds only a path, and every write is the driver's `write_bytes`.

Every file is the SYSTEM's (`owner="system"`, 0644): the agent reads its recording - the engine
copies it into `/work` as the agent before publishing it - and never rewrites it. The folder is a
plain absolute path with no empty, `.` or `..` part, and each name a plain file name, so nothing
the recorder writes can land anywhere but straight in the folder.

Each call here is a network round trip that yields to the event loop. The `Recorder`'s
guard-then-write steps (`row`: check `ready`, take the next `seq`, write) are atomic only because
its one owner serialises every call to it (`RemoteBrowser`'s lock); a sink is never shared."""

from __future__ import annotations

from typing import Protocol

from prismoid.ai.frames.driver import Owner


class FrameFiles(Protocol):
    async def read_bytes(self, path: str) -> bytes: ...
    async def write_bytes(
        self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent"
    ) -> None: ...


def _plain(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _canonical(folder: str) -> bool:
    parts = folder.split("/")
    return folder.startswith("/") and len(parts) > 1 and all(p not in ("", ".", "..") for p in parts[1:])


class FrameSink:
    def __init__(self, frame: FrameFiles) -> None:
        self._frame = frame
        self._folder: str | None = None

    @property
    def ready(self) -> bool:
        return self._folder is not None

    async def open(self, folder: str) -> bool:
        self._folder = folder if _canonical(folder) else None
        return self._folder is not None

    async def read(self, name: str) -> bytes | None:
        if self._folder is None or not _plain(name):
            return None
        try:
            return await self._frame.read_bytes(f"{self._folder}/{name}")
        except FileNotFoundError:
            return None

    async def create(self, name: str, data: bytes) -> None:
        """A new row's picture: its name carries the next `seq`, so it is never one written before."""
        if self._folder is not None and _plain(name):
            await self._frame.write_bytes(f"{self._folder}/{name}", data, mode=0o644, owner="system")

    async def replace(self, name: str, text: str) -> None:
        """One whole file, rewritten (the driver's write replaces a file whole)."""
        if self._folder is not None and _plain(name):
            await self._frame.write_bytes(f"{self._folder}/{name}", text.encode(), mode=0o644, owner="system")

    async def seal(self) -> None:
        """Nothing to open up: every file is already 0644, in a folder the system made."""
