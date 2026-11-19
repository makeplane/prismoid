# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The conversation a chat-born task came from rides the thread as turn 0 (ADR-059).

Pure half only: `transcript_from` with a prelude, the fit's anchor law (the task's own opening
ask, never the prelude), and the renderer's framing (a heading of its own, outside the turn
numbering, under the instruction-not-source wall). The row-world half (`conversation_prelude`
reading `messages`) is pinned by the chat lane's integration test.
"""

from __future__ import annotations

import datetime as dt
import uuid

from prismoid.ai.context.thread import render_thread_md
from prismoid.operations.thread import _entry, transcript_from  # pyright: ignore[reportPrivateUsage]
from prismoid.schemas.events import DurableEvent, StepNarration, TaskContinued, TaskCreated
from prismoid.schemas.ids import TaskId
from prismoid.schemas.plan import TaskMode
from prismoid.schemas.thread import ThreadEntry

Chain = list[tuple[TaskId, list[DurableEvent]]]

_AT = dt.datetime(2026, 9, 2, 10, 0, tzinfo=dt.UTC)


def _created(task_id: TaskId, goal: str, seq: int = 1) -> TaskCreated:
    return TaskCreated(seq=seq, task_id=task_id, at=_AT, goal=goal, mode=TaskMode.STANDARD)


def _prelude(task_id: TaskId) -> list[ThreadEntry]:
    return [
        _entry("user", "who was schwarzschild?", task_id, 0, 1),
        _entry(
            "assistant", "A German physicist; the first exact solution of the field equations.", task_id, 0, 2
        ),
        _entry("user", "/task write me a proper memo on his solution", task_id, 0, 3),
    ]


def test_the_prelude_opens_the_transcript_as_turn_zero() -> None:
    tid = TaskId(uuid.uuid4())
    chain: Chain = [(tid, [_created(tid, "write me a proper memo on his solution")])]
    t = transcript_from(chain, budget_tokens=4000, prelude=_prelude(tid))
    assert [turn.turn for turn in t.turns] == [0, 1]
    assert [e.kind for e in t.turns[0].entries] == ["user", "assistant", "user"]
    assert t.current_ask == "write me a proper memo on his solution"
    assert not t.is_empty()  # the conversation makes even a turn-1 task a thread with history


def test_the_anchor_is_the_tasks_own_ask_never_the_prelude() -> None:
    tid = TaskId(uuid.uuid4())
    events: list[DurableEvent] = [
        _created(tid, "write me a proper memo on his solution"),
        StepNarration(seq=2, task_id=tid, at=_AT, markdown="# Memo\n\n" + "words " * 600),
        TaskContinued(seq=3, task_id=tid, at=_AT, message="shorter please", turn=2),
        StepNarration(seq=4, task_id=tid, at=_AT, markdown="# Memo, shorter\n\n" + "words " * 300),
    ]
    prelude = [_entry("user", "x " * 400, tid, 0, 1), _entry("assistant", "y " * 400, tid, 0, 2)]
    t = transcript_from([(tid, events)], budget_tokens=700, prelude=prelude)
    assert t.truncated and t.elided_turns >= 1
    # the opening ask survives every clip; the prelude is context the budget may drop first
    assert t.turns[0].turn == 1
    assert [e.text for e in t.turns[0].entries] == ["write me a proper memo on his solution"]
    assert t.turns[-1].turn == 2


def test_the_prelude_renders_under_its_own_heading_outside_the_numbering() -> None:
    tid = TaskId(uuid.uuid4())
    chain: Chain = [(tid, [_created(tid, "write me a proper memo on his solution")])]
    t = transcript_from(chain, budget_tokens=4000, prelude=_prelude(tid))
    md = render_thread_md(t)
    assert "## Before this task - the conversation it came from" in md
    assert "## Turn 1\n\n**The user asked:**\n\nwrite me a proper memo" in md
    assert "## Turn 2" not in md  # the prelude never counts as a turn
    assert md.index("instruction and context, not a source") < md.index("## Before this task")


def test_no_prelude_changes_nothing() -> None:
    tid = TaskId(uuid.uuid4())
    chain: Chain = [(tid, [_created(tid, "hello")])]
    assert transcript_from(chain, budget_tokens=100) == transcript_from(chain, budget_tokens=100, prelude=[])
