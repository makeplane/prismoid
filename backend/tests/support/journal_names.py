# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The names the durable journal writes, read off the real tools as each one journals a call.

An eval that classifies a turn by its tool calls must key on these, never on a name typed by hand:
each helper runs one real toolset under the real `ToolRecorder` and returns the one name it journaled.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.toolsets import AbstractToolset

from prismoid.ai.tools import catalog
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.ai.tools.proposals import proposals_toolset
from prismoid.ai.tools.routines import routines_toolset
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.runners import run_recorded


class _Names:
    """A journal that keeps only the tool names it was handed."""

    def __init__(self) -> None:
        self.names: list[str] = []

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
        self.names.append(tool)


async def _accepted(_: dict[str, Any]) -> str:
    return "ok"


async def _the_one_name(toolset: AbstractToolset[Any], tool: str, args: dict[str, Any]) -> str:
    """Run one call of `tool` through the recorder and return the name it journaled."""
    journal = _Names()
    await run_recorded(toolset, [(tool, args)], journal)
    (name,) = journal.names
    return name


async def routine_proposal() -> str:
    """What `routine_create` journals when an agent proposes standing work."""
    args = {"name": "digest", "instruction": "post the digest", "cron": "0 9 * * 1-5"}
    return await _the_one_name(routines_toolset(None, _accepted), "routine_create", args)


async def task_proposal() -> str:
    """What `propose_task` journals when a bot proposes a task."""
    return await _the_one_name(proposals_toolset(_accepted), "propose_task", {"goal": "build the Q3 deck"})


def connector_call(slug: str, tool: str) -> str:
    """What a connector call of `tool` on the connector `slug` journals: the name it mounts under."""
    return catalog.journal_name(catalog.connector_tool(slug, tool))


async def shell() -> str:
    """What the frame's `shell` tool journals for one command."""
    frame = FakeFrame("journal-names")
    await frame.create("journal-names", [])
    return await _the_one_name(frame_toolset(("frame.shell",), frame), "shell", {"command": "ls"})
