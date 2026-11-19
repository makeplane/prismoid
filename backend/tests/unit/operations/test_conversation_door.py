# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation door (ADR-057) - pure, so every rung is a table row here.

Same facts, same verdict, whatever the words: what a line means is the model's to judge in the
reply (`ai/tools/chat.py`, `propose_task`). Money moves only with a gesture, a tap, a yes the bot
heard on its own waiting card, or a standing grant inside a cap; an outsider never starts spend.
"""

from __future__ import annotations

import uuid

import pytest

from prismoid.ai.agents.bot_compile import compile_bot
from prismoid.operations.chat_verdict import facts_of, settle_verdict, verdict_for
from prismoid.schemas.bots import BotManifest, ResolvedBot
from prismoid.schemas.conversation import (
    CHAT_LAW,
    Consent,
    ConversationFacts,
    Verdict,
    VerdictDecision,
    WorkMode,
)
from prismoid.schemas.ids import ActorId, BotName
from prismoid.schemas.steering import DIRECTIVE_MAX


def _facts(**over: object) -> ConversationFacts:
    return ConversationFacts(**over)  # pyright: ignore[reportArgumentType]


# ── facts_of: facts, never meaning ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "what is the difference between an MSA and an SOW?",
        "make me a spreadsheet of the renewals pipeline",
        "run the benchmark against last week's numbers",
        "send the summary to Dana in Slack",
        "book a ticket on makemytrip.com from Hyderabad to Mumbai",
        "just tell me quickly, is Q3 the renewal quarter?",
        "shorter, and drop the open questions",
        "yes, go ahead",
    ],
)
def test_the_door_reads_no_words(text: str) -> None:
    """Every line without a gesture or an explicit anchor is the bot's to judge: the same verdict."""
    d = verdict_for(facts_of(text, work_mode=WorkMode.AUTO.value))
    assert (d.verdict, d.reason, d.consent) == (Verdict.ANSWER, "talk", Consent.NONE)


def test_length_is_the_one_fact_read_off_the_text() -> None:
    assert not facts_of("x" * DIRECTIVE_MAX).too_long_to_steer
    assert facts_of("x" * (DIRECTIVE_MAX + 1)).too_long_to_steer
    assert facts_of("thoughts?", attachments=2).attachments == 2


def test_an_attachment_is_a_fact_that_moves_no_verdict() -> None:
    d = verdict_for(facts_of("thoughts?", attachments=2))
    assert (d.verdict, d.consent) == (Verdict.ANSWER, Consent.NONE)


# ── verdict_for: the ladder, first match wins ─────────────────────────────────────────────────


def test_rung_1_never_is_a_pure_talker() -> None:
    d = verdict_for(_facts(gesture=True, work_mode=WorkMode.NEVER.value))
    assert d.verdict is Verdict.ANSWER and "never" in d.reason


def test_rung_2_an_outsider_without_a_grant_only_talks_even_with_a_gesture() -> None:
    d = verdict_for(_facts(gesture=True, sender_is_owner=False, work_grant=False))
    assert d.verdict is Verdict.ANSWER and "the owner decides" in d.reason
    assert d.consent is Consent.NONE


def test_rung_3_the_gesture_starts() -> None:
    d = verdict_for(_facts(gesture=True))
    assert d.verdict is Verdict.START and d.consent is Consent.GESTURE


def test_rung_3_the_gesture_on_a_task_card_steers_or_follows_up() -> None:
    assert verdict_for(_facts(gesture=True, task_anchor="running")).verdict is Verdict.STEER
    assert verdict_for(_facts(gesture=True, task_anchor="settled")).verdict is Verdict.FOLLOW_UP


def test_rung_4_a_reply_on_a_card_without_a_gesture_still_routes_to_the_task() -> None:
    assert verdict_for(_facts(task_anchor="running")).verdict is Verdict.STEER
    assert verdict_for(_facts(task_anchor="settled")).verdict is Verdict.FOLLOW_UP


def test_a_running_anchor_past_the_steer_cap_is_refused() -> None:
    d = verdict_for(_facts(task_anchor="running", anchored_by="reply", too_long_to_steer=True))
    assert d.verdict is Verdict.REFUSED and "2,000 characters" in d.reason


def test_rung_6_talk() -> None:
    d = verdict_for(_facts())
    assert d.verdict is Verdict.ANSWER and d.reason == "talk"


def test_auto_mode_never_starts_on_the_pre_run_verdict() -> None:
    """A standing grant needs a PROPOSAL - the model's judgment enters once, after the reply."""
    d = verdict_for(_facts(work_mode=WorkMode.AUTO.value))
    assert d.verdict is Verdict.ANSWER


# ── settle_verdict: consent, after the model spoke ────────────────────────────────────────────


def _pre(**over: object) -> VerdictDecision:
    return VerdictDecision(verdict=Verdict.ANSWER, reason="talk", **over)  # pyright: ignore[reportArgumentType]


def test_no_proposal_keeps_the_verdict() -> None:
    assert settle_verdict(_pre(), _facts(), proposed=False, within_cap=True) == _pre()


def test_a_proposal_waits_under_ask() -> None:
    d = settle_verdict(_pre(), _facts(work_mode="ask"), proposed=True, within_cap=True)
    assert d.verdict is Verdict.PROPOSE and d.consent is Consent.NONE


def test_a_proposal_starts_under_a_standing_grant() -> None:
    d = settle_verdict(_pre(), _facts(work_mode="auto"), proposed=True, within_cap=True)
    assert d.verdict is Verdict.START and d.consent is Consent.GRANT


def test_a_grant_needs_the_owner_and_the_cap() -> None:
    outsider = settle_verdict(
        _pre(),
        _facts(work_mode="auto", sender_is_owner=False, work_grant=True),
        proposed=True,
        within_cap=True,
    )
    assert outsider.verdict is Verdict.PROPOSE
    over_cap = settle_verdict(_pre(), _facts(work_mode="auto"), proposed=True, within_cap=False)
    assert over_cap.verdict is Verdict.PROPOSE


def test_settle_never_touches_a_non_answer_verdict() -> None:
    steer = VerdictDecision(verdict=Verdict.STEER, reason="x", consent=Consent.GESTURE)
    assert settle_verdict(steer, _facts(work_mode="auto"), proposed=True, within_cap=True) is steer


# ── the chat compile: the same bot, built to talk ─────────────────────────────────────────────


def _bot(**over: object) -> ResolvedBot:
    manifest = BotManifest.model_validate(
        {
            "name": "contract-reviewer",
            "description": "Reviews contracts. Load when: an MSA lands. Not for: drafting.",
            "capability": "frontier-reasoner",
            "tools": ["frame.read_file", "frame.publish", "web.fetch"],
            "can_ask_user": True,
            **over,
        }
    )
    return ResolvedBot(
        name=BotName("contract-reviewer"),
        version=1,
        manifest=manifest,
        instructions="Review carefully.",
        owner_actor_id=ActorId(uuid.uuid4()),
    )


def test_conversation_compile_drops_the_frame_and_grants_propose() -> None:
    chat = compile_bot(_bot(), conversation=True)
    assert "propose_task" in chat.tools
    assert {"chat.continue_card", "chat.accept_pending", "chat.decline_pending"} <= set(chat.tools)
    assert not any(t.startswith("frame.") for t in chat.tools)
    assert "web.fetch" in chat.tools  # a granted read-only reach survives
    # A reply holds reach and the proposer, nothing frame- or task-shaped - the
    # hatch has no closure in a conversation, and a routine minted from a chat has no task.
    assert "escalate_to_plan" not in chat.tools
    assert (
        "routine.create"
        not in compile_bot(_bot(tools=["routine.create", "web.search"]), conversation=True).tools
    )
    assert "web.search" in compile_bot(_bot(tools=["routine.create", "web.search"]), conversation=True).tools
    assert chat.can_ask_user is False and chat.publishes_output is False and chat.plannable is False
    assert chat.prompt.endswith(CHAT_LAW) and chat.prompt.startswith("Review carefully.")


def test_task_compile_is_unchanged_by_the_conversation_arm() -> None:
    task = compile_bot(_bot())
    assert "propose_task" not in task.tools and "frame.publish" in task.tools
    assert not any(t.startswith("chat.") for t in task.tools)
    assert task.can_ask_user is True and task.plannable is True


@pytest.mark.parametrize("mode", ["ask", "auto", "never"])
def test_work_modes_are_the_three_named_dials(mode: str) -> None:
    assert WorkMode(mode).value == mode


def test_the_card_anchor_wears_its_source_on_the_verdict() -> None:
    d = verdict_for(_facts(task_anchor="settled", anchored_by="card"))
    assert d.verdict is Verdict.FOLLOW_UP and "under" in d.reason
    d = verdict_for(_facts(task_anchor="running", anchored_by="reply"))
    assert d.verdict is Verdict.STEER and "a reply on a running" in d.reason


@pytest.mark.parametrize("mode", ["one", "many"])
def test_threads_is_a_two_way_dial(mode: str) -> None:
    from prismoid.schemas.conversation import ThreadsMode

    assert ThreadsMode(mode).value == mode
    assert ThreadsMode.MANY.value == "many"  # the default: a chat per topic
