# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a resolved gate stores as its answers - decided from the spec and what the person sent.
No I/O: `gates.resolve` holds the lock and records what these return."""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.errors import ValidationFailed
from prismoid.operations import gate_answers
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, Question, QuestionSet, QuestionType

QUESTIONS = QuestionSet(
    title="Two things before I start",
    questions=[
        Question(id="currency", prompt="Which currency?", type=QuestionType.TEXT, default="USD"),
        Question(id="ship", prompt="Ship on Friday?", type=QuestionType.BOOLEAN),
    ],
    expires_at=dt.datetime(2026, 11, 5, tzinfo=dt.UTC),
)
ASK = GateSpec(kind=GateKind.INPUT, title="Two things", questions=QUESTIONS)
APPROVE = GateSpec(kind=GateKind.APPROVE, title="Allow Gmail: gmail_send?")


def test_cancel_stops_the_task_except_at_a_budget_gate_or_once_settled() -> None:
    assert gate_answers.cancel_stops_task(GateAction.CANCEL, GateKind.INPUT.value, terminal=False)
    assert not gate_answers.cancel_stops_task(GateAction.CANCEL, GateKind.BUDGET.value, terminal=False)
    assert not gate_answers.cancel_stops_task(GateAction.CANCEL, GateKind.INPUT.value, terminal=True)
    assert not gate_answers.cancel_stops_task(GateAction.DECLINE, GateKind.INPUT.value, terminal=False)


def test_a_gate_without_scopes_takes_none() -> None:
    assert gate_answers.browser_scope([], {"scope": "always"}) == ""


def test_the_chosen_scope_stands_and_the_default_prefers_task() -> None:
    assert gate_answers.browser_scope(["once", "task", "always"], {"scope": "always"}) == "always"
    assert gate_answers.browser_scope(["once", "task", "always"], None) == "task"
    assert gate_answers.browser_scope(["once", "always"], {}) == "once", "no task scope: the first offered"


def test_a_scope_the_gate_does_not_offer_is_a_422_naming_it() -> None:
    with pytest.raises(ValidationFailed) as caught:
        gate_answers.browser_scope(["once", "task"], {"scope": "always"})
    assert caught.value.fields == {"scope": "one of once, task"}


def test_accepting_an_approval_is_the_content() -> None:
    assert gate_answers.accept_answers(APPROVE, None, None) == {}
    assert gate_answers.accept_answers(APPROVE, {"x": 1}, {"scope": "task"}) == {"scope": "task"}


def test_an_accepted_question_batch_is_validated() -> None:
    assert gate_answers.accept_answers(ASK, {"currency": "EUR", "ship": True}, None) == {
        "currency": "EUR",
        "ship": True,
    }


def test_a_bad_answer_is_a_422() -> None:
    with pytest.raises(ValidationFailed):
        gate_answers.accept_answers(ASK, {"currency": "EUR", "ship": "perhaps"}, None)


def test_a_decline_proceeds_with_the_stated_defaults() -> None:
    assert gate_answers.decline_answers(ASK) == {"currency": "USD"}
    assert gate_answers.decline_answers(APPROVE) == {}


def test_a_declined_default_is_typed_like_an_answer() -> None:
    confirm = Question(
        id="send", prompt="Send it?", type=QuestionType.BOOLEAN, required=False, default="false"
    )
    spec = ASK.model_copy(update={"questions": QUESTIONS.model_copy(update={"questions": [confirm]})})
    assert gate_answers.decline_answers(spec) == {"send": False}


def test_a_decline_keeps_what_the_person_answered_over_its_default() -> None:
    """Answering one question and declining the rest never overwrites the answer given."""
    assert gate_answers.decline_answers(ASK, {"currency": "EUR"}) == {"currency": "EUR"}
    assert gate_answers.decline_answers(ASK, {"currency": "", "ship": "yes"}) == {
        "currency": "USD",
        "ship": True,
    }


def test_a_bad_answer_on_a_decline_is_a_422() -> None:
    with pytest.raises(ValidationFailed):
        gate_answers.decline_answers(ASK, {"ship": "perhaps"})
    with pytest.raises(ValidationFailed):
        gate_answers.decline_answers(ASK, {"nope": "x"})


def test_the_resolution_names_the_questions_filled_from_defaults() -> None:
    """A skipped optional question carries its default, and says it was not the person's answer."""
    optional = QUESTIONS.questions[0].model_copy(update={"required": False})
    spec = ASK.model_copy(
        update={"questions": QUESTIONS.model_copy(update={"questions": [optional, QUESTIONS.questions[1]]})}
    )
    accepted = gate_answers.accept_answers(spec, {"currency": "", "ship": True}, None)
    assert accepted == {"currency": "USD", "ship": True}
    assert gate_answers.defaulted(spec, {"currency": "", "ship": True}, accepted) == ["currency"]
    chose = gate_answers.accept_answers(spec, {"currency": "USD", "ship": True}, None)
    assert gate_answers.defaulted(spec, {"currency": "USD", "ship": True}, chose) == [], "chosen, not skipped"
    declined = gate_answers.decline_answers(ASK, {"ship": "yes"})
    assert gate_answers.defaulted(ASK, {"ship": "yes"}, declined) == ["currency"]
    assert (
        gate_answers.defaulted(
            ASK, {"currency": "EUR"}, gate_answers.decline_answers(ASK, {"currency": "EUR"})
        )
        == []
    )
    assert gate_answers.defaulted(APPROVE, None, {}) == []
