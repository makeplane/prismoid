# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The steering copy is built in one place (schemas/steering.py) and says exactly this."""

from __future__ import annotations

import uuid

import pytest

from prismoid.schemas.steering import (
    REJECT_CANCELLED,
    REJECT_FAILED,
    REJECT_TURN_ENDED,
    MissedSteer,
    SteerNote,
    approval_held_text,
    final_text,
    held_text,
    injection_text,
    missed_message,
    names,
    pause_resume_text,
    preview,
    replan_reason,
    replanning_label,
    rerun_text,
)


def _note(author: str, text: str, epoch: int) -> SteerNote:
    return SteerNote(command_id=uuid.uuid4(), epoch=epoch, author=author, text=text, by_creator=False)


def test_injection_lists_every_steer_oldest_first_then_the_instruction() -> None:
    text = injection_text((_note("Bob", "use euros", 2), _note("Ada", "add a chart", 3)))
    assert text == (
        "**Bob steered** while you worked: use euros\n"
        "**Ada steered** while you worked: add a chart\n\n"
        "Follow it from here. The work in /work stays; change it where the steer requires."
    )


def test_held_names_each_author_once_in_order() -> None:
    steers = (_note("Bob", "a", 2), _note("Ada", "b", 3), _note("Bob", "c", 4))
    assert names(steers) == "Bob and Ada"
    assert held_text(steers) == (
        "Held, not run: Bob and Ada steered after you chose this call. Re-issue it if it still fits."
    )


def test_final_rerun_and_approval_copy() -> None:
    bob = _note("Bob", "use euros", 2)
    assert final_text((bob,)) == (
        "**Bob steered** before you finished: use euros\n\n"
        "Revise your answer to follow it, or confirm it already does."
    )
    # The superseded output is mounted read-only where a follow-up finds its prior turn:
    # the words point there, because a fresh frame's /work is empty (Machines off, a seat).
    assert rerun_text((bob,), ("/task/inputs/memo.md",)) == (
        "**Bob steered** after your last attempt finished: use euros\n\n"
        "Your earlier output is superseded. It is at /task/inputs/memo.md (read-only): "
        "revise it in /work and publish again."
    )
    assert rerun_text((bob,), ("/task/inputs/a.md", "/task/inputs/b.md")).endswith(
        "It is at /task/inputs/a.md and /task/inputs/b.md (read-only): revise it in /work and publish again."
    )
    assert rerun_text((bob,), ()) == (
        "**Bob steered** after your last attempt finished: use euros\n\n"
        "Your earlier output is superseded: redo it in /work and publish again."
    )
    assert approval_held_text(bob) == (
        "Held, not run: Bob steered while this waited for approval: 'use euros'. "
        "Re-issue it if it still fits."
    )


def test_pause_resume_copy_with_and_without_a_resumer() -> None:
    assert pause_resume_text("Ada", "Bob") == (
        "Paused before this ran (Ada paused, Bob resumed). It did not run; re-issue it if it still fits."
    )
    assert pause_resume_text("Ada", None) == (
        "Paused before this ran (Ada paused). It did not run; re-issue it if it still fits."
    )


def test_replan_reason_one_and_many_authors() -> None:
    assert replan_reason((_note("Bob", "Only cover German providers", 2),)) == (
        "Re-planned for Bob's steer: 'Only cover German providers'"
    )
    assert replan_reason((_note("Bob", "a", 2), _note("Ada", "b", 3))) == (
        "Re-planned for Bob's and Ada's steers"
    )
    assert replan_reason((_note("Bob", "a", 2), _note("Bob", "b", 3))) == "Re-planned for Bob's steers"


def test_preview_caps_at_120_with_an_ellipsis_and_one_line() -> None:
    assert preview("x" * 200) == "x" * 119 + "…"
    assert preview("a\nb") == "a b"


def test_missed_message_names_the_first_thing_missed() -> None:
    steer = MissedSteer(kind="steer", author="Bob", text="use euros", epoch=3)
    turn = MissedSteer(kind="turn", author="Ada", epoch=4)
    assert missed_message([steer, turn]) == "Bob steered while you were typing: 'use euros'"
    assert missed_message([turn]) == "Ada started the next turn"


def test_an_author_named_with_and_is_not_split() -> None:
    notes = (_note("Ben and Jerry", "a", 2), _note("Ada", "b", 3))
    assert replan_reason(notes) == "Re-planned for Ben and Jerry's and Ada's steers"
    assert replan_reason((_note("Ben and Jerry", "a", 2),)) == "Re-planned for Ben and Jerry's steer: 'a'"


def test_missed_message_refuses_nothing_and_rejection_reasons_are_pinned() -> None:
    with pytest.raises(ValueError, match="nothing was missed"):
        missed_message([])
    assert REJECT_CANCELLED == "the task was cancelled before this applied"
    assert REJECT_FAILED == "the task failed before this applied"
    assert REJECT_TURN_ENDED == "the turn ended before this applied"


def test_the_live_label_says_what_the_replan_is_doing_now() -> None:
    assert replanning_label((_note("Bob", "Only cover German providers", 2),)) == (
        "Re-planning for Bob's steer: 'Only cover German providers'"
    )
    assert replanning_label((_note("Bob", "a", 2), _note("Bob", "b", 3))) == "Re-planning for Bob's steers"
    assert replanning_label((_note("Bob", "a", 2), _note("Ada", "b", 3), _note("Bob", "c", 4))) == (
        "Re-planning for Bob's and Ada's steers"
    )
