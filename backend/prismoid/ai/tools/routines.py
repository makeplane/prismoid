# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`routine_create` - an agent proposes standing work: the agent can propose, never arm.

The tool holds no hands of its own: it calls `deps.propose_routine`, a closure OPERATIONS
wired (the `emit_usage` precedent - the engine stays persistence-free), which creates the
routine BORN PAUSED under the task's human owner and knocks their inbox. Autonomy to create
standing spend is never inferred from a task; the owner's resume is the arming ceremony.

The call is journaled like every tool's (`ToolRecorder`); a refusal comes back to the agent as a
RESULT string (the web door's stance), so the tool says its own `ok=false` row with the reason -
the flight recorder never shows `ok=false` with no stated why.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final

from pydantic import Field
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog
from prismoid.ai.tools.call_rows import JournalTool, Said, say
from prismoid.schemas.routines import INSTRUCTION_MAX

_ROW: Final = catalog.tool("routine_create")
ROUTINE_CREATE: Final = _ROW.name

# The proposer's shape lives HERE - the module that calls it (the JournalTool precedent);
# `deps` aliases it, because `deps` sits above `tools` in the engine contract.
ProposeRoutine = Callable[[dict[str, Any]], Awaitable[str]]


def routines_toolset(journal: JournalTool | None, propose: ProposeRoutine) -> FunctionToolset[object]:
    """`routine_create` over the closure operations wired; a refusal is journaled and answered."""
    toolset: FunctionToolset[object] = FunctionToolset(id="prismoid-routines")

    async def routine_create(
        name: Annotated[str, Field(min_length=1, max_length=120, description="a short display name")],
        instruction: Annotated[
            str,
            Field(
                min_length=1,
                max_length=INSTRUCTION_MAX,
                description=(
                    "the standing instruction - SELF-CONTAINED: each run starts fresh with no "
                    "memory of this conversation"
                ),
            ),
        ],
        cron: Annotated[
            str | None, Field(description='5-field cron, e.g. "0 9 * * 1-5" - OR every OR on_events')
        ] = None,
        every: Annotated[str | None, Field(description='an interval like "4h" or "30m"')] = None,
        on_events: Annotated[
            list[str] | None,
            Field(
                description=(
                    "listen instead of scheduling: task.delivered · task.failed · "
                    "escalation.raised · verification.signed · artifact.produced"
                ),
            ),
        ] = None,
        budget_usd: Annotated[
            float | None, Field(gt=0, le=100, description="per-run budget; the instance default if omitted")
        ] = None,
    ) -> str:
        """Save the user's ask as a STANDING routine - when they ask for something recurring
        ("every Monday…", "whenever a task fails…"), not for one-off work.

        A recurring ask IS this call: make it first, rather than asking the user questions
        before you start. The routine is created PAUSED, in the user's name, and they are
        notified to review and resume it, so its review is where open details (where to post,
        what to include) get settled - name them in your answer as what to check before
        resuming. You cannot arm it, and you must SAY in your answer that it is a routine
        waiting for their go-ahead. Give exactly one of cron / every / on_events.
        """
        payload = {
            "name": name,
            "instruction": instruction,
            "cron": cron,
            "every": every,
            "on_events": on_events,
            "budget_usd": budget_usd,
        }
        try:
            return await propose(payload)
        except Exception as exc:  # fail-soft: a refusal is a RESULT the agent can act on
            await say(journal, Said.failed(_ROW.journal, exc))
            return f"The routine was not saved: {exc}. Fix the ask and try once more, or tell the user."

    toolset.add_function(routine_create, name=ROUTINE_CREATE)
    return toolset
