# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Ask only what is still open, and never answer a checkpoint for the person.

A step that re-asks a question the person already answered - the SAME question, in
`/task/answers.json` - gets that answer back in place instead of a second gate. Whether a request's free text answers a question
is the asking agent's judgement (its prompt says not to ask what the goal states), never a
pattern's: every vocabulary for it dropped a finding put in other words."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from prismoid.operations.execution.deferrals import Deferral
from prismoid.operations.execution.handlers import deferral
from prismoid.operations.questions import known_asks
from prismoid.schemas.park import ParkKind, park_of

_ANSWERS = {
    "q1": {"question": "What date should I search?", "answer": "next Friday"},
    "q2": {"question": "Any airline you prefer?", "answer": ""},  # declined: nothing held
}


@pytest.mark.parametrize(
    "prompt", ["What date should I search?", "  what date should I  search ", "WHAT DATE SHOULD I SEARCH."]
)
def test_the_same_question_again_is_known(prompt: str) -> None:
    (held,) = known_asks([prompt], _ANSWERS)
    assert held is not None and "next Friday" in held


@pytest.mark.parametrize(
    "prompt",
    [
        "What is the travel date?",  # the same fact in other words: the agent's judgement, asked
        "I found no flights next Friday. What other date works?",  # a finding
        "Any airline you prefer?",  # asked before, declined: nothing held
        "Which of these two flights should I book?",
    ],
)
def test_anything_but_an_exact_answered_re_ask_is_asked(prompt: str) -> None:
    assert known_asks([prompt], _ANSWERS) == [None]


def test_with_no_answers_nothing_is_known() -> None:
    assert known_asks(["What date should I search?"], {}) == [None]


# ── where a step's ask_user batch becomes a gate ─────────────────────────────────────────────────


def _ladder(*, turn: int = 1, answers: dict[str, Any] | None = None) -> Any:
    session = AsyncMock()
    task = SimpleNamespace(id="t", goal="book a flight", answers=answers or {}, turn=turn)
    return SimpleNamespace(session=session, task=task, step=SimpleNamespace(id="s"), spec=None, resume=None)


def _asked(*prompts: str) -> Deferral:
    return Deferral(tool_call_id="call_1", questions=[(p, []) for p in prompts])


def _gate(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    opened = AsyncMock(return_value="asking")
    monkeypatch.setattr(deferral.parks, "open_gate", opened)
    monkeypatch.setattr(deferral.parks, "park_on_question", lambda *a, **k: "parked")
    monkeypatch.setattr(deferral.Gate, "count_for_step", AsyncMock(return_value=0))
    return opened


async def test_a_batch_answered_before_continues_without_a_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _gate(monkeypatch)
    ladder = _ladder(answers=_ANSWERS)
    said = await deferral.question(ladder, _asked("What date should I search?"))
    assert said is deferral.AGAIN and not opened.called
    assert park_of(ladder.resume).kind is ParkKind.QUESTION, (
        "answered in place: the turn is read again on resume"
    )
    assert "next Friday" in ladder.resume["answer"] and "ask again" in ladder.resume["answer"]


async def test_a_mixed_batch_asks_only_what_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Deferral] = []

    def park(session: object, task: object, step: object, outcome: Deferral) -> str:
        seen.append(outcome)
        return "parked"

    monkeypatch.setattr(deferral.parks, "park_on_question", park)
    monkeypatch.setattr(deferral.parks, "open_gate", AsyncMock(return_value="asking"))
    monkeypatch.setattr(deferral.Gate, "count_for_step", AsyncMock(return_value=0))
    again, fresh = "What date should I search?", "Where are you flying from?"
    assert await deferral.question(_ladder(answers=_ANSWERS), _asked(again, fresh)) == "asking"
    assert [p for p, _ in seen[0].questions] == [fresh]


async def test_a_question_the_goal_seems_to_answer_is_still_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """No free-text reading: the step's own prompt keeps it from asking what the goal states."""
    opened = _gate(monkeypatch)
    assert (
        await deferral.question(_ladder(), _asked("Where are you flying from?")) == "asking" and opened.called
    )


async def test_on_a_follow_up_the_first_turns_answers_are_not_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """/task/answers.json is turn 1's: a follow-up ("make it Friday instead") may outdate it."""
    opened = _gate(monkeypatch)
    ladder = _ladder(turn=2, answers=_ANSWERS)
    assert await deferral.question(ladder, _asked("What date should I search?")) == "asking" and opened.called
