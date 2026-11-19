# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation door (ADR-057): facts in, verdict out, as a pure function.

The door reads no words for meaning. What a line asks for is the model's to judge in the reply,
through typed tools (`propose_task`, `continue_card`, `accept_pending`, `decline_pending`), and code
disposes of each call by consent, cap, sender and state. Money never moves on a verdict alone:
START needs a gesture, a tap, a yes the bot heard on its own waiting card, or a standing grant
inside a cap.

The ladder, first match wins:

  1. work_mode=never                          → ANSWER (a pure talker)
  2. outsider without a work grant            → ANSWER (the bot may still PROPOSE; the owner
                                                 decides - an outsider never starts spend)
  3. a running anchor past the steer cap      → REFUSED, wearing why
  4. the explicit gesture                     → START / STEER / FOLLOW_UP by the task anchor
  5. the card's explicit Reply                → STEER (running) / FOLLOW_UP (settled)
  6. otherwise                                → ANSWER

`settle_verdict` finishes the record after the model ran: a proposal becomes PROPOSE, or
START under a standing grant (work_mode=auto, the owner's own message, inside the cap).
"""

from __future__ import annotations

from prismoid.schemas.conversation import (
    Consent,
    ConversationFacts,
    Verdict,
    VerdictDecision,
    WorkMode,
)
from prismoid.schemas.steering import DIRECTIVE_MAX, TOO_LONG_TO_STEER


def facts_of(
    message: str,
    *,
    attachments: int = 0,
    gesture: bool = False,
    sender_is_owner: bool = True,
    work_grant: bool = False,
    work_mode: str = WorkMode.ASK.value,
    pending_proposal: bool = False,
    task_anchor: str | None = None,
    anchored_by: str = "",
) -> ConversationFacts:
    """The facts of one line: who sent it, the dials, the anchor, and its length - never its meaning."""
    return ConversationFacts(
        gesture=gesture,
        attachments=attachments,
        sender_is_owner=sender_is_owner,
        work_grant=work_grant,
        work_mode=work_mode,
        pending_proposal=pending_proposal,
        task_anchor=task_anchor,
        anchored_by=anchored_by,
        too_long_to_steer=len(message.strip()) > DIRECTIVE_MAX,
    )


def verdict_for(facts: ConversationFacts) -> VerdictDecision:
    """The ladder. Returns the PRE-RUN decision; the model may add a proposal after."""
    if facts.work_mode == WorkMode.NEVER.value:
        return VerdictDecision(verdict=Verdict.ANSWER, reason="work mode never: a pure talker")

    if not facts.sender_is_owner and not facts.work_grant:
        return VerdictDecision(
            verdict=Verdict.ANSWER,
            reason="external sender without a work grant: talk only, the owner decides work",
        )

    if facts.task_anchor == "running" and facts.too_long_to_steer:
        # A line for a running task steers it, and a steer's words are capped at every door:
        # a longer one is refused, wearing why - never cut short (half an instruction),
        # never answered past the card.
        return VerdictDecision(verdict=Verdict.REFUSED, reason=TOO_LONG_TO_STEER)

    if facts.gesture:
        if facts.task_anchor == "running":
            return VerdictDecision(
                verdict=Verdict.STEER, reason="gesture on a running task", consent=Consent.GESTURE
            )
        if facts.task_anchor == "settled":
            return VerdictDecision(
                verdict=Verdict.FOLLOW_UP, reason="gesture on a settled task", consent=Consent.GESTURE
            )
        return VerdictDecision(verdict=Verdict.START, reason="the explicit gesture", consent=Consent.GESTURE)

    how = "under" if facts.anchored_by == "card" else "on"
    if facts.task_anchor == "running":
        return VerdictDecision(verdict=Verdict.STEER, reason=f"a reply {how} a running task's card")
    if facts.task_anchor == "settled":
        return VerdictDecision(verdict=Verdict.FOLLOW_UP, reason=f"a reply {how} a delivered task's card")

    return VerdictDecision(verdict=Verdict.ANSWER, reason="talk")


def settle_verdict(
    decision: VerdictDecision, facts: ConversationFacts, *, proposed: bool, within_cap: bool
) -> VerdictDecision:
    """After the model spoke: a proposal becomes PROPOSE, or START under a standing grant (work_mode=auto, the
    owner's own message, inside the cap); no proposal keeps the pre-run verdict."""
    if not proposed or decision.verdict is not Verdict.ANSWER:
        return decision
    if facts.work_mode == WorkMode.AUTO.value and facts.sender_is_owner and within_cap:
        return VerdictDecision(
            verdict=Verdict.START,
            reason=f"proposed; standing grant (work_mode=auto) - {decision.reason}",
            consent=Consent.GRANT,
        )
    return VerdictDecision(
        verdict=Verdict.PROPOSE,
        reason=f"proposed; awaiting consent - {decision.reason}",
        consent=Consent.NONE,
    )
