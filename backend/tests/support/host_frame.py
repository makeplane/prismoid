# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A verify frame on this host: each virtual root a folder, the suite run for real through the
system-Python door, so nothing between the record and the check is stubbed but the sandbox.
A plain module, so a test imports it and never another test module."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import FramePaths


class HostFrame:
    """The frame `operations/verification.py` gets from `new_frame`, on this host under `root`."""

    def __init__(self, root: Path) -> None:
        def at(name: str) -> str:
            return str(root / name)

        self.paths = FramePaths(
            task=at("task"),
            work=at("work"),
            artifacts=at("artifacts"),
            skills=at("skills"),
            verify=at("verify"),
            inputs=at("task/inputs"),
            browser=at("browser"),
            on_host=True,
        )
        self.job: dict[str, Any] = {}

    async def create(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: str = "agent") -> None:
        _put(Path(self.paths.render(path)), data)
        if path.endswith("/job.json"):
            self.job = json.loads(data)

    async def read_bytes(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    async def system_python(
        self,
        script: str,
        *args: str,
        stdin: str | None = None,
        timeout: float = 120.0,  # noqa: ASYNC109
    ) -> ExecResult:
        body = _get(Path(self.paths.render(stdin))) if stdin else b""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            self.paths.render(script),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(body), timeout)
        return ExecResult(proc.returncode or 0, out.decode(), err.decode())

    async def destroy(self) -> None:
        return None


def _put(real: Path, data: bytes) -> None:
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(data)


def _get(real: Path) -> bytes:
    return real.read_bytes()
