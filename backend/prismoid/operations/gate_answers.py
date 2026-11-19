# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a resolved gate stores as its answers - the pure half of `gates.resolve`.

`resolve` takes the task's row lock, checks who may answer, and records the resolution; the
answers themselves are decided here from the gate's spec and what the person sent, without a
database: the scope an approval takes, the defaults a decline proceeds with, the validated
answers an accept carries.
"""

from __future__ import annotations

from typing import Any

from prismoid.errors import ValidationFailed
from prismoid.operations import questions
from prismoid.schemas.plan import GateAction, GateKind, GateSpec


def cancel_stops_task(action: GateAction, kind: str, *, terminal: bool) -> bool:
    """**Cancel stops the task**, whichever door (the gate card's "Cancel task", the header): a
    step woken on a cancel only leaves, so a cancel that closed the gate alone would wedge the
    task. A budget gate's own cancel already ends it, wherever it parked."""
    return action is GateAction.CANCEL and kind != GateKind.BUDGET.value and not terminal


def browser_scope(scopes: list[str], content: dict[str, Any] | None) -> str:
    """The scope a browser approval takes: the one the person chose, else the default - decided
    here, once, for every client (a Project editor's CLI cannot look it up: the gate list is the
    creator's own queue). Empty when the gate offers no scopes; a scope it does not offer is a 422."""
    if not scopes:
        return ""
    scope = str((content or {}).get("scope", "")) or ("task" if "task" in scopes else scopes[0])
    if scope not in scopes:
        raise ValidationFailed("choose a scope", fields={"scope": f"one of {', '.join(scopes)}"})
    return scope


def accept_answers(
    spec: GateSpec, content: dict[str, Any] | None, prior: dict[str, Any] | None
) -> dict[str, Any]:
    """An accept's answers for every kind but connect: an `approve`-shaped gate (no questions)
    stores what an earlier branch built (`prior`, the browser scope or the control hand-back),
    else `{}` - accepting it *is* the content. A question batch is validated, and a bad answer
    is a **422 naming the question**: "invalid input" is not something a human typing at speed
    can act on."""
    if spec.questions is None:
        return prior if prior is not None else {}
    try:
        return questions.validate_answers(spec.questions, content or {})
    except questions.BadAnswers as bad:
        raise ValidationFailed(str(bad), fields=bad.fields) from None


def decline_answers(spec: GateSpec, content: dict[str, Any] | None = None) -> dict[str, Any]:
    """**Proceed without me.** Not a cancellation - the defaults are what the clarifier said it
    would do if nobody answered, and it said so *in the question*. An answer the person did give
    stands over its default: declining the rest never overwrites what they chose. A bad one is a 422."""
    if spec.questions is None:
        return {}
    try:
        given = questions.given_answers(spec.questions, content or {})
    except questions.BadAnswers as bad:
        raise ValidationFailed(str(bad), fields=bad.fields) from None
    return {**questions.defaults(spec.questions), **given}


def defaulted(spec: GateSpec, content: dict[str, Any] | None, answers: dict[str, Any] | None) -> list[str]:
    """The question ids an accept or a decline filled from defaults, so no surface calls them answers."""
    if spec.questions is None or not answers:
        return []
    return questions.defaulted(spec.questions, content or {}, answers)
