# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""In-memory doubles for a frame's file door (`MemoryFiles`) and for the tool record's journal
(`Journal`): what was written or recorded, for a test to read back."""

from __future__ import annotations

import asyncio


class MemoryFiles:
    """A frame's file door in memory: what was written, as whom, at which mode."""

    def __init__(self, yields: int = 0) -> None:
        self.yields = yields  # event-loop turns each call gives up first: a network door yields
        self.files: dict[str, bytes] = {}
        self.writes: list[tuple[str, int, str]] = []

    async def read_bytes(self, path: str) -> bytes:
        for _ in range(self.yields):
            await asyncio.sleep(0)
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: str = "agent") -> None:
        for _ in range(self.yields):
            await asyncio.sleep(0)
        self.files[path] = data
        self.writes.append((path, mode, owner))


class Journal:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.entries.append(
            {
                "tool": tool,
                "ok": ok,
                "summary": summary,
                "denied_reason": denied_reason,
                "tool_call_id": tool_call_id,
            }
        )
