# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead: one run per task turn that decides it and ends it (ADR-092).

The leading loops end a turn when the model writes text and add typed endings only for what a chat
loop lacks. So the Lead's output is plain text (the reply, which streams) beside five output tools:
`submit_plan`, `ask_person`, `offer_task`, `decline` (a refusal or a cannot-do, which ends as could-not-do), and, only when nobody is there to answer, `fail` and
`deliver_to_owner`, which replace the reply, the question and the card (the model never names a recipient:
code fixes it as the routine's owner, so delivering needs no approval). Each ends the run in the call that
makes it; the Lead never parks and never waits, and code commits the ending.

The output type is the same on every turn, so the cached prefix (instructions and tool definitions)
is one per deployment. What a turn allows is a fact intake computed (`LeadFacts`): the tail of the
turn's message says it (`context/turn.py`), and an ending it forbids is refused inside the run
(`capabilities/lead_check.py`, which also holds its plan to the rules), which is how opencode and
Claude Code keep a mode's rules off the cached prefix.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Final

from pydantic_ai import ToolOutput

from prismoid.ai.agents.base import AgentDef, register
from prismoid.ai.capabilities.lead_check import LeadEndings, PlanCheck
from prismoid.ai.context.turn import ENDING_TOOLS
from prismoid.schemas.ids import AgentName
from prismoid.schemas.lead import (
    AskUser,
    Decline,
    Deliver,
    Fail,
    KeepPlan,
    LeadEnding,
    LeadEndingKind,
    PlanRevision,
    ProposeTask,
    Reply,
    SubmitPlan,
)
from prismoid.schemas.model_class import ModelClass

PROMPT = (Path(__file__).parent / "prompts" / "lead.md").read_text()

# Each output tool's description is its ending's docstring, which pydantic-ai sends with its schema.
ENDINGS: Final = (
    str,
    ToolOutput(SubmitPlan, name=ENDING_TOOLS[LeadEndingKind.PLAN]),
    ToolOutput(AskUser, name=ENDING_TOOLS[LeadEndingKind.ASK]),
    ToolOutput(ProposeTask, name=ENDING_TOOLS[LeadEndingKind.PROPOSE]),
    ToolOutput(Fail, name=ENDING_TOOLS[LeadEndingKind.FAIL]),
    ToolOutput(Deliver, name=ENDING_TOOLS[LeadEndingKind.DELIVER]),
    ToolOutput(Decline, name=ENDING_TOOLS[LeadEndingKind.DECLINE]),
)


def ending_of(output: str | SubmitPlan | AskUser | ProposeTask | Fail | Deliver | Decline) -> LeadEnding:
    """A Lead run's output as its ending: text is the reply."""
    return Reply(text=output) if isinstance(output, str) else output


# The replan seat's endings: at a frontier boundary the Lead revises the steps not started, or keeps them.
REPLAN_ENDINGS: Final = (
    ToolOutput(PlanRevision, name="revise_plan", max_retries=2),
    ToolOutput(KeepPlan, name="keep_plan"),
)


def _with_replan(lead: AgentDef) -> AgentDef:
    """The Lead with its replan seat: the same role, thinking more, ending only in a revision or a keep."""
    seat = dataclasses.replace(
        lead, endings=REPLAN_ENDINGS, effort="medium", guarded=False, capabilities=(PlanCheck(),)
    )
    return dataclasses.replace(lead, replan=seat)


LEAD = register(
    _with_replan(
        AgentDef(
            name=AgentName("lead"),
            model_class=ModelClass.FRONTIER_EXECUTOR,  # it routes every turn and writes every reply the person reads
            description=(
                "Decides each task turn in one run: replies when a message can hold the result, plans "
                "when the outcome needs hands, asks when a guess would ruin the work, or answers and "
                "offers a task. Not for: a step's work - it has no computer, publishes nothing and is "
                "never planned."
            ),
            prompt=PROMPT,
            endings=ENDINGS,
            effort="low",
            guarded=True,  # it searches and reads, so the stall brakes and the request ceiling ride it
            plannable=False,  # it writes plans; a plan never seats it
            capabilities=(LeadEndings(), PlanCheck()),
        )
    )
)
