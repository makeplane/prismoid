# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Questions - the deterministic half of asking a human something.

The Lead (or a step's `ask_user` call) proposes what to ask. This module decides whether that is a **question set** -
one numbered batch, never dripped - and, on the way back, whether what the human typed is an
**answer**.

Both directions matter, and the return leg matters more. Answers arrive from a CLI, from an HTTP
client, from an MCP host, and eventually from something nobody has written yet; they are typed by a
human at speed. `validate_answers` is the only thing standing between "`2: mabye`" and a step that
runs for four minutes on a value nothing ever checked.

No model here, and no database: the gate operations compose it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from prismoid.schemas.plan import (
    Clarification,
    Question,
    QuestionOption,
    QuestionSet,
    QuestionType,
    TimeoutPolicy,
)

MAX_QUESTIONS = 6  # a batch a human will not read is a batch a human will decline
DEFAULT_TTL = dt.timedelta(hours=24)

# The two ways an answer can be a boolean, and nobody types `True`.
TRUE = frozenset({"y", "yes", "true", "1", "on"})
FALSE = frozenset({"n", "no", "false", "0", "off"})


class BadQuestions(Exception):
    """The proposal is not a question set."""


class BadAnswers(Exception):
    """What came back does not answer what was asked. Carries `fields` - the API turns it into a
    422 that names the question, because "invalid input" is not a message anybody can act on."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


# ── asking only what is still open ──────────────────────────────────────────────────────────────


def _same_question(prompt: str) -> str:
    """A question as `known_asks` compares it: case, spacing and the closing mark aside."""
    return " ".join(prompt.casefold().split()).rstrip(" ?.!:")


def known_asks(prompts: Sequence[str], answers: Mapping[str, Any]) -> list[str | None]:
    """Per question, the answer `/task/answers.json` already holds for that exact question, else None.
    Only an exact re-ask matches: whether free text answers a question is the asking agent's judgement."""
    held: dict[str, str] = {}
    for raw in answers.values():
        given = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
        question, answer = str(given.get("question") or ""), str(given.get("answer") or "").strip()
        if question.strip() and answer:
            held[_same_question(question)] = f"{question.strip()!r} was answered {answer!r}"
    return [held.get(_same_question(prompt)) for prompt in prompts]


def already_answered(known: Iterable[str | None]) -> str:
    """What a step's dropped `ask_user` call is answered with when every question was asked before."""
    facts = "; ".join(k for k in known if k)
    return (
        f"Not asked again - /task/answers.json already holds it: {facts}. Go on with that answer. If "
        "what you found contradicts it, ask again and say what you found."
    )


def build(
    proposal: Clarification, *, now: dt.datetime, ttl: dt.timedelta = DEFAULT_TTL
) -> QuestionSet | None:
    """A proposal to the numbered batch, or `None` when nothing is worth asking (the good, cheap outcome)."""
    if not proposal.questions:
        return None
    if len(proposal.questions) > MAX_QUESTIONS:
        raise BadQuestions(f"{len(proposal.questions)} questions is more than the {MAX_QUESTIONS} allowed")

    questions: list[Question] = []
    for n, q in enumerate(proposal.questions, start=1):
        if why := q.misshapen():
            raise BadQuestions(f"question {n} {why}")

        # A yes/no is never assumed yes: declining it, or letting it time out, is "no".
        default = "false" if q.type is QuestionType.BOOLEAN else q.default
        questions.append(
            Question(
                id=f"q{n}",  # stable within the set, and it IS the number the user types
                prompt=q.prompt,
                type=q.type,
                options=[QuestionOption(value=o, label=o) for o in q.options],
                required=not default,  # a question with a default can always be declined
                default=default or None,
                allow_other=q.allow_other,
            )
        )

    return QuestionSet(
        title="A few things before I start",
        context=proposal.context,
        questions=questions,
        # The TTL and the policy are **ours**, not the model's. The contract requires every set to carry an
        # expiry and an `on_timeout`; a model asked to invent a timestamp will invent one, and it
        # will be wrong in a way that parks a task until somebody notices.
        expires_at=now + ttl,
        on_timeout=TimeoutPolicy.PROCEED_WITH_DEFAULTS,
    )


def mid_run(batch: Sequence[tuple[str, Sequence[str]]], *, now: dt.datetime) -> QuestionSet:
    """A step's `ask_user` batch, `(prompt, options)` pairs, as its gate's set: no default exists, so it waits."""
    n = len(batch)
    return QuestionSet(
        title="One question before I continue" if n == 1 else f"{n} questions before I continue",
        questions=[
            Question(
                id=f"q{i + 1}",
                prompt=prompt,
                type=QuestionType.SINGLE_SELECT if options else QuestionType.TEXT,
                options=[QuestionOption(value=o, label=o) for o in options],
                required=True,
            )
            for i, (prompt, options) in enumerate(batch)
        ],
        expires_at=now + DEFAULT_TTL,
        on_timeout=TimeoutPolicy.WAIT,  # a mid-run question has no safe default - only a human answers
    )


def validate_answers(questions: QuestionSet, answers: dict[str, Any]) -> dict[str, Any]:
    """What the human typed, coerced to what the plan may use (`"yes"`, `"41.6"` arrive as strings);
    anything else raises `BadAnswers`. The one boundary."""
    out: dict[str, Any] = {}
    bad: dict[str, str] = {}

    known = {q.id: q for q in questions.questions}
    if unknown := sorted(set(answers) - set(known)):
        raise BadAnswers(
            f"no such question: {', '.join(unknown)}",
            dict.fromkeys(unknown, "no such question in this set"),
        )

    for qid, question in known.items():
        raw = answers.get(qid)

        if raw is None or raw == "":
            if question.required:
                bad[qid] = f"required: {question.prompt}"
            elif question.default is not None:
                out[qid] = _coerce(question, question.default)
            continue

        try:
            out[qid] = _coerce(question, raw)
        except BadAnswers as why:
            bad[qid] = str(why)

    if bad:
        raise BadAnswers(f"{len(bad)} answer(s) did not fit the question", bad)
    return out


def given_answers(questions: QuestionSet, answers: dict[str, Any]) -> dict[str, Any]:
    """Only the answers the human gave, coerced like `validate_answers`; an empty one is no answer,
    and nothing is required."""
    known = {q.id: q for q in questions.questions}
    if unknown := sorted(set(answers) - set(known)):
        raise BadAnswers(
            f"no such question: {', '.join(unknown)}",
            dict.fromkeys(unknown, "no such question in this set"),
        )
    out: dict[str, Any] = {}
    bad: dict[str, str] = {}
    for qid, raw in answers.items():
        if raw is None or raw == "" or raw == []:
            continue
        try:
            out[qid] = _coerce(known[qid], raw)
        except BadAnswers as why:
            bad[qid] = str(why)
    if bad:
        raise BadAnswers(f"{len(bad)} answer(s) did not fit the question", bad)
    return out


def defaulted(questions: QuestionSet, given: dict[str, Any], stored: dict[str, Any]) -> list[str]:
    """The questions the person left unanswered whose stored answer is its default: a skip, never
    something they chose."""
    return [
        q.id
        for q in questions.questions
        if given.get(q.id) in (None, "", [])
        and q.default is not None
        and q.id in stored
        and stored[q.id] == _coerce(q, q.default)
    ]


def defaults(questions: QuestionSet) -> dict[str, Any]:
    """What declining the batch proceeds with: each stated default, typed as an answer would be."""
    return {q.id: _coerce(q, q.default) for q in questions.questions if q.default is not None}


def _coerce(question: Question, raw: Any) -> Any:
    match question.type:
        case QuestionType.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            token = str(raw).strip().casefold()
            if token in TRUE:
                return True
            if token in FALSE:
                return False
            raise BadAnswers(f"expected yes or no, got {raw!r}")

        case QuestionType.NUMBER:
            try:
                return float(Decimal(str(raw).strip().replace(",", "")))
            except (InvalidOperation, ValueError):
                raise BadAnswers(f"expected a number, got {raw!r}") from None

        case QuestionType.SINGLE_SELECT:
            return _one_of(question, str(raw).strip())

        case QuestionType.MULTI_SELECT:
            tokens = cast("list[Any]", raw) if isinstance(raw, list) else str(raw).split(",")
            return [_one_of(question, str(t).strip()) for t in tokens if str(t).strip()]

        case QuestionType.TEXT:
            return str(raw)


def _one_of(question: Question, token: str) -> str:
    """An option's value, its label, or the number the user typed (the batch is numbered on every surface)."""
    values = [o.value for o in question.options]

    if token.isdigit() and 1 <= int(token) <= len(values):
        return values[int(token) - 1]

    folded = token.casefold()
    for option in question.options:
        if folded in (option.value.casefold(), option.label.casefold()):
            return option.value

    # A single letter, `a`/`b`/`c` - the other way a numbered list gets referred to.
    if len(token) == 1 and token.isalpha():
        index = ord(folded) - ord("a")
        if 0 <= index < len(values):
            return values[index]

    if question.allow_other:
        return token

    raise BadAnswers(f"expected one of {', '.join(values)}, got {token!r}")
