# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`propose_task` - a bot in conversation proposes work (ADR-057). It can propose; it can
never start.

The routines tool's twin: no hands of its own, only `deps.propose_task`, a closure OPERATIONS
wired (the emit_usage precedent - the engine stays persistence-free). The closure records the
proposal on the bot's message row; whether it STARTS is the door's business (a gesture, a
tap, or a standing grant inside a cap), never the model's. The call is journaled like every
tool's (`ToolRecorder`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final

from pydantic import Field
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog
from prismoid.schemas.conversation import MAX_PROPOSAL_GOAL_CHARS, DocumentFormat

ProposeTask = Callable[[dict[str, Any]], Awaitable[str]]
_PROPOSE_TASK: Final = catalog.tool("propose_task").name


def proposals_toolset(propose: ProposeTask) -> FunctionToolset[object]:
    """`propose_task` over the closure operations wired; the closure records the proposal and
    answers whether it started."""
    toolset: FunctionToolset[object] = FunctionToolset(id="prismoid-proposals")

    async def propose_task(
        goal: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_PROPOSAL_GOAL_CHARS,
                description=(
                    "the work, SELF-CONTAINED: the task starts fresh with this goal and the "
                    "conversation so far as context - say what to produce and what good looks like"
                ),
            ),
        ],
        deliverable: Annotated[
            str | None,
            Field(description="the kind expected: document | spreadsheet | deck | code | dataset | webapp"),
        ] = None,
        format: Annotated[
            DocumentFormat | None,
            Field(description="a document's file format, when the person named one or one fits best"),
        ] = None,
        why: Annotated[
            str, Field(max_length=300, description="one line: why this needs a run rather than an answer")
        ] = "",
        direct: Annotated[
            bool,
            Field(
                description=(
                    "true (default) = you do the work yourself; false = hand it to the seats a "
                    "plan chooses - only when the work is outside your role"
                )
            ),
        ] = True,
    ) -> str:
        """Propose a task. Use when the result must be a file, a run, a verified deliverable, or an
        action in a connected service. The result says whether it already started or waits for the
        person's consent - tell them exactly that."""
        payload = {"goal": goal, "deliverable": deliverable, "format": format, "why": why, "direct": direct}
        return await propose(payload)

    toolset.add_function(propose_task, name=_PROPOSE_TASK)
    return toolset
