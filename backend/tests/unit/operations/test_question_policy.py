# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The question/answer boundary.

`validate_answers` is the only thing standing between `"2: mabye"` and a step that runs for four
minutes on a value nothing ever checked. It is typed by a human, at speed, from a CLI or an HTTP
client or an MCP host - so it coerces as well as checks, and it fails by *naming the question*.
"""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.operations.questions import BadAnswers, BadQuestions, build, validate_answers
from prismoid.schemas.plan import Clarification, ProposedQuestion, QuestionType, TimeoutPolicy

NOW = dt.datetime(2026, 7, 14, tzinfo=dt.UTC)


def asked(*questions: ProposedQuestion):
    got = build(Clarification(context="why", questions=list(questions)), now=NOW)
    assert got is not None
    return got


CURRENCY = ProposedQuestion(
    prompt="Which currency?", type=QuestionType.SINGLE_SELECT, options=["euros", "dollars"]
)
LENGTH = ProposedQuestion(prompt="How long?", type=QuestionType.NUMBER, default="400")
PUBLIC = ProposedQuestion(prompt="Is this public?", type=QuestionType.BOOLEAN)


# ── silence is the good outcome, and it must stay cheap to reach ───────────────────────────────


def test_no_questions_means_no_gate() -> None:
    """The clarifier's most valuable answer. A system that asks two questions about every request is
    a system people route around - after which it is not there for the one that mattered."""
    assert build(Clarification(questions=[]), now=NOW) is None


# ── the batch, as every surface will render it ────────────────────────────────────────────────


def test_the_batch_is_numbered_and_carries_its_own_expiry_and_policy() -> None:
    got = asked(CURRENCY, LENGTH)
    assert [q.id for q in got.questions] == ["q1", "q2"]
    assert [n for n, _ in got.numbered()] == [1, 2]
    # The TTL and the policy are ours, not the model's - it would invent a timestamp, and be wrong
    # in a way that parks a task until somebody notices.
    assert got.expires_at > NOW
    assert got.on_timeout is TimeoutPolicy.PROCEED_WITH_DEFAULTS


def test_a_question_with_a_default_is_not_required() -> None:
    """Which is what makes `--decline` mean *proceed without me* rather than *give up*."""
    got = asked(LENGTH, PUBLIC)
    assert got.questions[0].required is False and got.questions[0].default == "400"
    assert got.questions[1].required is False and got.questions[1].default == "false"


def test_a_yes_no_is_never_assumed_yes() -> None:
    """A confirm the model defaulted to yes still proceeds as "no" when the person declines."""
    confirm = ProposedQuestion(prompt="Send the transfer?", type=QuestionType.BOOLEAN, default="true")
    got = asked(confirm)
    assert got.questions[0].default == "false"
    assert validate_answers(got, {}) == {"q1": False}


def test_a_select_with_one_option_is_not_a_question() -> None:
    with pytest.raises(BadQuestions, match="fewer than two options"):
        asked(ProposedQuestion(prompt="?", type=QuestionType.SINGLE_SELECT, options=["only"]))


def test_a_free_text_question_may_not_carry_options() -> None:
    with pytest.raises(BadQuestions, match="carries options"):
        asked(ProposedQuestion(prompt="?", type=QuestionType.TEXT, options=["a", "b"]))


# ── the return leg: what a human actually types ───────────────────────────────────────────────


def test_a_user_may_answer_a_numbered_list_by_its_number() -> None:
    """`1: b, 2: yes` is the shape the question gate asks for - the user is reading a numbered list,
    and making
    them retype the label is making them make a typo."""
    got = asked(CURRENCY)
    assert validate_answers(got, {"q1": "1"}) == {"q1": "euros"}
    assert validate_answers(got, {"q1": "2"}) == {"q1": "dollars"}
    assert validate_answers(got, {"q1": "b"}) == {"q1": "dollars"}  # and by letter
    assert validate_answers(got, {"q1": "DOLLARS"}) == {"q1": "dollars"}  # and by label, any case


def test_answers_are_coerced_not_merely_checked() -> None:
    """Every surface hands these over as strings. A step that receives `"true"` where it expected a
    boolean does something confident and wrong."""
    got = asked(LENGTH, PUBLIC)
    assert validate_answers(got, {"q1": "1,200", "q2": "yes"}) == {"q1": 1200.0, "q2": True}
    assert validate_answers(got, {"q1": 400, "q2": "n"}) == {"q1": 400.0, "q2": False}


def test_a_bad_answer_names_the_question() -> None:
    """ "invalid input" is not a message anybody can act on, and this one came from a human typing
    at speed."""
    got = asked(CURRENCY, PUBLIC)
    with pytest.raises(BadAnswers) as bad:
        validate_answers(got, {"q1": "yen", "q2": "mabye"})
    assert set(bad.value.fields) == {"q1", "q2"}
    assert "euros, dollars" in bad.value.fields["q1"]
    assert "yes or no" in bad.value.fields["q2"]


def test_a_missing_required_answer_is_a_failure_and_a_missing_optional_one_takes_its_default() -> None:
    got = asked(LENGTH, CURRENCY)  # q1 has a default, q2 does not
    with pytest.raises(BadAnswers) as bad:
        validate_answers(got, {})
    assert set(bad.value.fields) == {"q2"}, "q1 has a default; only q2 is required"

    assert validate_answers(got, {"q2": "euros"}) == {"q1": 400.0, "q2": "euros"}


def test_an_answer_to_a_question_nobody_asked_is_rejected() -> None:
    with pytest.raises(BadAnswers, match="no such question"):
        validate_answers(asked(CURRENCY), {"q9": "euros"})


def test_allow_other_lets_a_user_out_of_the_options() -> None:
    got = asked(
        ProposedQuestion(
            prompt="Which currency?",
            type=QuestionType.SINGLE_SELECT,
            options=["euros", "dollars"],
            allow_other=True,
        )
    )
    assert validate_answers(got, {"q1": "yen"}) == {"q1": "yen"}
