# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tail of a Lead run's message: the turn's facts (`turn_note`) or the steers a replan answers (`replan_note`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from prismoid.schemas.lead import LeadEndingKind, LeadFacts
from prismoid.schemas.steering import SteerNote

# Each ending's output tool; the reply is plain text and has none.
ENDING_TOOLS: Final[dict[LeadEndingKind, str]] = {
    LeadEndingKind.PLAN: "submit_plan",
    LeadEndingKind.ASK: "ask_person",
    LeadEndingKind.PROPOSE: "offer_task",
    LeadEndingKind.FAIL: "fail",
    LeadEndingKind.DELIVER: "deliver_to_owner",
    LeadEndingKind.DECLINE: "decline",
}


def said(kind: LeadEndingKind) -> str:
    """How the turn note and a refusal name an ending, in the words of the prompt."""
    tool = ENDING_TOOLS.get(kind)
    return "a text reply" if tool is None else f"`{tool}`"


def named(kinds: frozenset[LeadEndingKind]) -> str:
    """The endings in `kinds`, as the prompt names them, in a fixed order."""
    return ", ".join(said(kind) for kind in LeadEndingKind if kind in kinds)


def turn_note(facts: LeadFacts) -> str:
    """The turn's facts as the Lead reads them, last in its message: the endings it may take, then why."""
    allowed = facts.allowed_endings()
    lines = [f"- this turn may end with {named(allowed)}"]
    if facts.unattended:
        lines.append(f"- nobody is there to answer: this {facts.origin} turn runs on its own")
    elif allowed == frozenset({LeadEndingKind.ASK}):
        lines.append("- the person asked to be asked before any work starts")
    elif facts.asked:
        lines.append("- you already asked this turn; the answers are above")
    elif facts.clarify == "never":
        lines.append("- the person turned questions off")
    if facts.deliverable is not None:
        kind = (
            f"{facts.deliverable} ({facts.deliverable_format})"
            if facts.deliverable_format
            else facts.deliverable
        )
        lines.append(f"- the person set the deliverable: a {kind}")
    if facts.size is not None:
        lines.append(f"- the person set the size: {facts.size}")
    if facts.inputs_unread:
        lines.append(f"- attached files you cannot read here: {facts.inputs_unread}; a step reads them")
    if facts.direct_bot is not None:
        lines.append(
            f"- this is {facts.direct_bot}'s thread: the plan's work step runs as {facts.direct_bot}"
        )
    if facts.budget_left_usd is not None:
        lines.append(f"- budget left on this task: ${facts.budget_left_usd:.2f}")
    return "# This turn\n\n" + "\n".join(lines) + "\n"


def replan_note(steers: Sequence[SteerNote], facts: LeadFacts) -> str:
    """A replan run's tail, last in its message: the steers it answers, then the two endings it may take."""
    said_lines = "\n".join(f"- **{s.author} steered**: {s.text}" for s in steers)
    lines = [
        "- your plan is running: the done work stands, and only the steps not started can change",
        "- end with `revise_plan` (the steps that replace every one not started) or `keep_plan` (the "
        "plan already follows the steers, or they cannot change it)",
    ]
    if facts.budget_left_usd is not None:
        lines.append(f"- budget left on this task: ${facts.budget_left_usd:.2f}")
    return f"# Steered since you planned\n\n{said_lines}\n\n# This run\n\n" + "\n".join(lines) + "\n"
