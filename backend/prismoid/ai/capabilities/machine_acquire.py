# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The task's Machine starts on the first tool that needs the computer, never before (ADR-063, amended).

A serial step starts on paper (`frames/paper.py`): reads, the web, connectors, a file the model
writes and the publish door all run without a computer. The first call that changes the Machine
itself (`SideEffect.LOCAL`: a shell, an image rendered into `/work`) acquires it through the run's
port (`TaskDeps.machine`), which carries the paper's files across; from then on every frame call
lands on the Machine. This is the lazy sandbox of Anthropic's Managed Agents and pydantic-ai-harness's
sandbox backends (created on first use), with paper serving the calls that need no computer.

A re-entered or continued run replays its call log onto a fresh paper: the writes paper holds run
again in their logged order (`replays_onto_paper`), so the attempt sees the `/work` it left.

The toolsets hold the step's frame from the build, so the frame they hold is an `OnDemandFrame`
(`frames/on_demand.py`): the paper until the swap, the Machine after it. Nothing here starts a
computer; operations does, behind the port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.on_demand import OnDemandFrame
from prismoid.ai.tools import catalog
from prismoid.ai.tools.catalog import side_effect_of
from prismoid.schemas.side_effects import SideEffect

# A call that changes the Machine but that paper holds as well: a file the model writes or edits.
PAPER_HOLDS: Final = frozenset({"frame.write_file", "frame.edit"})


def needs_machine(journal: str) -> bool:
    """Does a call journaled as `journal` need the computer under it?"""
    return side_effect_of(journal) is SideEffect.LOCAL and journal not in PAPER_HOLDS


# What a run's logged calls left on paper: applied again as the run replays, since paper ends with its attempt.
PAPER_REPLAYS: Final = PAPER_HOLDS | {"frame.publish"}


def replays_onto_paper(deps: object, tool: str) -> bool:
    """Must a logged `tool` call run again as its run replays? A write paper holds, on paper: the call
    log is paper's durable home, so a re-entered or continued run sees the `/work` it left."""
    frame = deps.frame if isinstance(deps, TaskDeps) else None
    return isinstance(frame, OnDemandFrame) and frame.on_paper and catalog.journal_name(tool) in PAPER_REPLAYS


@dataclass
class MachineAcquire(AbstractCapability[TaskDeps]):
    """Before a tool that needs the computer runs, the step's Machine is acquired (`TaskDeps.machine`)."""

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # stateless; mounted on every build, inert without a port

    async def before_tool_execute(
        self,
        ctx: RunContext[TaskDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        port = ctx.deps.machine
        if port is not None and needs_machine(catalog.journal_name(call.tool_name)):
            ctx.deps.frame = await port.acquire()
        return args
