# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A decision-only run: a built agent runs until its first ending or its first acting tool call.

What a call can change is the tool catalog's `SideEffect`: a pure tool runs as it is, a read is
answered by the caller, and any other call is the decision, stopped before it acts. With no answer
for reads, a read is the decision too, the way a trigger eval stops at the first other tool. The
offline decision runner and a bot's publish gate both decide this way.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.models import Model
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools import catalog
from prismoid.schemas.side_effects import SideEffect

# A read's answer, by tool name; it may raise to stop the run.
ReadAnswer = Callable[[str], object]


class Acted(Exception):
    """The run's first acting call, stopped before it ran; `tool` is its record name."""

    def __init__(self, tool: str) -> None:
        super().__init__(tool)
        self.tool = tool


@dataclass
class DecisionOnly(WrapperToolset[TaskDeps]):
    """A built toolset whose pure tools run, whose reads `answer` serves, and whose acts end the run unrun."""

    answer: ReadAnswer | None  # None: a read ends the run as an act does
    reads: list[str]  # the record names of the reads answered, in call order

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[TaskDeps], tool: ToolsetTool[TaskDeps]
    ) -> Any:
        row = catalog.tool(name)
        if row.side_effect is SideEffect.PURE:
            return await super().call_tool(name, tool_args, ctx, tool)
        if row.side_effect is not SideEffect.READ or self.answer is None:
            raise Acted(row.journal)
        answered = self.answer(name)
        self.reads.append(row.journal)
        return answered


@contextmanager
def decision_only(
    runner: AgentRunner, answer: ReadAnswer | None, reads: list[str], model: Model | None = None
) -> Generator[None]:
    """Run `runner` decision-only inside the block; `model` replaces the seat's model when given."""
    toolsets = [DecisionOnly(toolset, answer, reads) for toolset in runner.agent.toolsets]
    override = (
        runner.agent.override(toolsets=toolsets)
        if model is None
        else runner.agent.override(toolsets=toolsets, model=model)
    )
    with override:
        yield
