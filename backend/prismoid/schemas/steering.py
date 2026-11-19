# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Steering values (ADR-064): the command's states, the safe points, what crosses the engine seam,
and every sentence the model or a person reads about a steer. Pure: no clock, no I/O."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

PREVIEW_MAX: Final = 120
DIRECTIVE_MAX: Final = 2000  # a steer's words, at every door (the `steers.text` column's width)
TOO_LONG_TO_STEER: Final = (
    f"too long to steer a running task ({DIRECTIVE_MAX:,} characters at most): shorten it, or send it "
    "once the task is done"
)
REJECT_CANCELLED: Final = "the task was cancelled before this applied"
REJECT_FAILED: Final = "the task failed before this applied"
REJECT_TURN_ENDED: Final = "the turn ended before this applied"
NOT_CARRIED: Final = "the fork was made before this applied"  # an open steer stays its run's
PAUSE_METADATA: Final[dict[str, str]] = {"steering": "pause"}  # a CallDeferred's mark for a pause
BUDGET_METADATA: Final[dict[str, str]] = {"guard": "budget"}  # the run guard's budget park


class SteerState(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    RETRACTED = "retracted"


class SafePoint(StrEnum):
    BEFORE_MODEL = "before_model"
    BEFORE_TOOL = "before_tool"
    FINAL_ANSWER = "final_answer"
    APPROVAL = "approval"
    REPLAN = "replan"
    RERUN = "rerun"
    INTERRUPT = "interrupt"  # early cancel only
    BOUNDARY = "boundary"  # pause.applied only


class SteerNote(BaseModel):
    """One steer as the engine sees it. `author` is already a safe display name. `by_creator`:
    the task's creator wrote it - a step's connectors are hers, so anyone else's words borrow
    them (`Steering.borrows`)."""

    model_config = ConfigDict(frozen=True)

    command_id: uuid.UUID
    epoch: int
    author: str
    text: str
    by_creator: bool


class SteerPending(BaseModel):
    """What a safe point reads: is the task paused, has it ended (failed, cancelled - the run stops
    there), and which steers has this run not been told."""

    model_config = ConfigDict(frozen=True)

    paused: bool = False
    steers: tuple[SteerNote, ...] = ()
    ended: bool = False


class MissedSteer(BaseModel):
    """One thing a stale steer's author had not seen: a later steer, or a turn's start."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["steer", "turn"]
    author: str
    text: str = ""
    epoch: int
    state: str | None = None


def preview(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= PREVIEW_MAX else one_line[: PREVIEW_MAX - 1] + "…"


def _authors(steers: Sequence[SteerNote]) -> list[str]:
    seen: list[str] = []
    for s in steers:
        if s.author not in seen:
            seen.append(s.author)
    return seen


def names(steers: Sequence[SteerNote]) -> str:
    return " and ".join(_authors(steers))


def injection_text(steers: Sequence[SteerNote]) -> str:
    lines = "\n".join(f"**{s.author} steered** while you worked: {s.text}" for s in steers)
    return f"{lines}\n\nFollow it from here. The work in /work stays; change it where the steer requires."


HELD: Final = "held: "  # a journaled call a steer held - chosen, never run (R53 reads past it)


def held_reason(steers: Sequence[SteerNote]) -> str:
    """A held call's journal reason: the timeline's "Held · Bob steered before this ran"."""
    return f"{HELD}{names(steers)} steered"


def held_text(steers: Sequence[SteerNote]) -> str:
    return f"Held, not run: {names(steers)} steered after you chose this call. Re-issue it if it still fits."


def final_text(steers: Sequence[SteerNote]) -> str:
    lines = "\n".join(f"**{s.author} steered** before you finished: {s.text}" for s in steers)
    return f"{lines}\n\nRevise your answer to follow it, or confirm it already does."


def rerun_text(steers: Sequence[SteerNote], at: Sequence[str]) -> str:
    """A re-run's brief. `at` is where the superseded output is mounted, read-only: a re-run on a
    fresh frame (Machines off, a seat) starts with an empty /work, so the words point there."""
    lines = "\n".join(f"**{s.author} steered** after your last attempt finished: {s.text}" for s in steers)
    if not at:
        return f"{lines}\n\nYour earlier output is superseded: redo it in /work and publish again."
    where = " and ".join(at)
    return (
        f"{lines}\n\nYour earlier output is superseded. It is at {where} (read-only): "
        "revise it in /work and publish again."
    )


def approval_held_text(steer: SteerNote) -> str:
    return (
        f"Held, not run: {steer.author} steered while this waited for approval: '{preview(steer.text)}'. "
        "Re-issue it if it still fits."
    )


def pause_resume_text(paused_by: str, resumed_by: str | None) -> str:
    who = f"{paused_by} paused, {resumed_by} resumed" if resumed_by else f"{paused_by} paused"
    return f"Paused before this ran ({who}). It did not run; re-issue it if it still fits."


def _for_steers(lead: str, steers: Sequence[SteerNote]) -> str:
    authors = _authors(steers)
    if len(authors) == 1 and len(steers) == 1:
        return f"{lead} for {authors[0]}'s steer: '{preview(steers[0].text)}'"
    if len(authors) == 1:
        return f"{lead} for {authors[0]}'s steers"
    return f"{lead} for " + " and ".join(f"{a}'s" for a in authors) + " steers"


def replan_reason(steers: Sequence[SteerNote]) -> str:
    """The record's `plan.edited{replan}` reason, once the re-plan is done."""
    return _for_steers("Re-planned", steers)


def replanning_label(steers: Sequence[SteerNote]) -> str:
    """The live phase while the boundary re-plan's planner works."""
    return _for_steers("Re-planning", steers)


def missed_message(missed: Sequence[MissedSteer]) -> str:
    """Callers build a 409 only when something was missed."""
    if not missed:
        raise ValueError("nothing was missed")
    first = missed[0]
    if first.kind == "turn":
        return f"{first.author} started the next turn"
    return f"{first.author} steered while you were typing: '{preview(first.text)}'"


def merge_directives(goal: str, acceptance: list[str], directives: list[str]) -> tuple[str, list[str]]:
    """Fold steering directives into a step's contract. Pure - the deterministic half.

    A directive becomes an **acceptance criterion**, not a rewrite of the goal: "use euros" is a
    thing the output must satisfy and the verifier will check, which is the honest place to put a
    human's instruction. The goal - what the step is *for* - is the planner's; the constraints on how
    it is done are where steering belongs.
    """
    if not directives:
        return goal, acceptance
    steered = [*acceptance, *(f"the user steered mid-task: {d}" for d in directives)]
    return goal, steered
