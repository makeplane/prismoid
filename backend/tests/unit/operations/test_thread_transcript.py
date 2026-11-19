# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The thread transcript's selection, ordering and clipping (ADR-018) - pure, no DB.

The transcript is the conversation's ONLY carrier into the model, so these pins are the
context contract: what becomes dialogue, what stays machinery, that the render is
deterministic and append-only (the prompt-cache prefix property), and that clipping keeps the
newest turns whole with the opening ask always anchored and the elision announced.
"""

from __future__ import annotations

import uuid

from prismoid.ai.context.thread import render_thread_md
from prismoid.db.base import utcnow
from prismoid.operations.thread import transcript_from
from prismoid.schemas.events import (
    ActorRef,
    ArtifactProduced,
    ArtifactRevised,
    DurableEvent,
    GateOpened,
    GateResolved,
    PlanEdited,
    SteerApplied,
    SteerRejected,
    SteerRetracted,
    StepNarration,
    TaskContinued,
    TaskCreated,
    TaskDelivered,
    TaskStatusChanged,
)
from prismoid.schemas.ids import ActorId, ArtifactId, GateId, TaskId
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    GateAction,
    GateKind,
    PlanEditKind,
    Question,
    QuestionSet,
    QuestionType,
    TaskMode,
    TaskStatus,
)
from prismoid.schemas.plan import PlanEdit as PlanEditView
from prismoid.schemas.steering import REJECT_TURN_ENDED, SafePoint

NOW = utcnow()
T1 = TaskId(uuid.uuid4())
BIG = 100_000  # a budget nothing here approaches


def _created(seq: int, goal: str, task_id: TaskId = T1) -> TaskCreated:
    return TaskCreated(seq=seq, task_id=task_id, at=NOW, goal=goal, mode=TaskMode.STANDARD)


def _narration(seq: int, md: str, task_id: TaskId = T1) -> StepNarration:
    return StepNarration(seq=seq, task_id=task_id, at=NOW, markdown=md)


def _continued(seq: int, message: str, turn: int) -> TaskContinued:
    return TaskContinued(seq=seq, task_id=T1, at=NOW, message=message, turn=turn)


def _delivered(seq: int, summary: str, task_id: TaskId = T1) -> TaskDelivered:
    return TaskDelivered(seq=seq, task_id=task_id, at=NOW, summary=summary)


def _steer(seq: int, directive: str) -> PlanEdited:
    return PlanEdited(
        seq=seq,
        task_id=T1,
        at=NOW,
        edit=PlanEditView(kind=PlanEditKind.STEER, reason=directive, at=NOW),
    )


def _texts(events: list[DurableEvent], *, budget: int = BIG) -> list[tuple[str, str]]:
    t = transcript_from([(T1, events)], budget_tokens=budget)
    return [(e.kind, e.text) for turn in t.turns for e in turn.entries]


def test_the_selection_table() -> None:
    """Each conversation-shaped event becomes its entry; machinery never appears."""
    gate = GateId(uuid.uuid4())
    art = ArtifactId(uuid.uuid4())
    events: list[DurableEvent] = [
        _created(1, "Write the Q3 memo"),
        GateOpened(
            seq=2,
            task_id=T1,
            at=NOW,
            gate_id=gate,
            kind=GateKind.INPUT,
            title="Intake",
            questions=QuestionSet(
                title="Intake",
                questions=[Question(id="q1", prompt="Who reads it?", type=QuestionType.TEXT)],
                expires_at=NOW,
            ),
        ),
        GateResolved(
            seq=3, task_id=T1, at=NOW, gate_id=gate, action=GateAction.ACCEPT, content={"q1": "The board"}
        ),
        _steer(4, "use euros"),
        _narration(5, "Revenue rose 12%."),
        ArtifactProduced(
            seq=6,
            task_id=T1,
            at=NOW,
            artifact_id=art,
            artifact_type=ArtifactType.DOCUMENT,
            title="memo.md",
            sha256="0" * 64,
            bytes=1,
        ),
        _delivered(7, "A fresh conclusion the narrations never carried."),
        _continued(8, "now shorten it", 2),
        ArtifactRevised(
            seq=9,
            task_id=T1,
            at=NOW,
            artifact_id=ArtifactId(uuid.uuid4()),
            supersedes_id=art,
            lineage_id=art,
            revision=2,
            artifact_type=ArtifactType.DOCUMENT,
            title="memo.md",
            sha256="1" * 64,
            bytes=1,
        ),
        TaskStatusChanged(seq=10, task_id=T1, at=NOW, status=TaskStatus.FAILED, reason="provider down"),
    ]
    got = _texts(events)
    assert got == [
        ("user", "Write the Q3 memo"),
        ("qa", "**Asked:** Who reads it? **They said:** The board"),
        ("steering", "use euros"),
        ("assistant", "Revenue rose 12%."),
        ("event", "[published memo.md]"),
        ("assistant", "A fresh conclusion the narrations never carried."),
        ("event", "[delivered]"),
        ("user", "now shorten it"),
        ("event", "[revised memo.md - revision 2]"),
        ("event", "[the task failed: provider down]"),
    ]


def test_a_redundant_delivered_summary_is_just_the_marker() -> None:
    """`run_task` builds the summary by joining the narrations - repeating it would read the
    whole answer to the model twice."""
    events: list[DurableEvent] = [
        _created(1, "Write it"),
        _narration(2, "Part one."),
        _narration(3, "Part two."),
        _delivered(4, "Part one.\n\nPart two."),
    ]
    got = _texts(events)
    assert ("event", "[delivered]") in got
    assert not any(
        kind == "assistant" and "Part one." in text and "Part two." in text for kind, text in got[3:]
    ), "the joined summary never renders as a second assistant entry"


def test_current_ask_is_the_newest_user_turn() -> None:
    t = transcript_from(
        [(T1, [_created(1, "Write it"), _continued(2, "shorten it", 2), _continued(3, "translate it", 3)])],
        budget_tokens=BIG,
    )
    assert t.current_ask == "translate it"
    assert [turn.turn for turn in t.turns] == [1, 2, 3]


def test_a_fork_childs_copied_goal_is_suppressed_into_the_marker() -> None:
    child = TaskId(uuid.uuid4())
    t = transcript_from(
        [
            (T1, [_created(1, "Write the Q3 memo"), _narration(2, "Done.")]),
            (child, [_created(1, "Write the Q3 memo", task_id=child)]),
        ],
        budget_tokens=BIG,
    )
    entries = [(e.kind, e.text) for turn in t.turns for e in turn.entries]
    assert entries.count(("user", "Write the Q3 memo")) == 1
    assert ("event", "[the user forked the conversation here]") in entries


def test_is_empty_is_true_only_for_a_bare_opening() -> None:
    assert transcript_from([(T1, [_created(1, "Write it")])], budget_tokens=BIG).is_empty()
    assert not transcript_from(
        [(T1, [_created(1, "Write it"), _narration(2, "Done.")])], budget_tokens=BIG
    ).is_empty()


def _long_chain(turns: int) -> list[DurableEvent]:
    events: list[DurableEvent] = [_created(1, "The opening ask, always kept.")]
    seq = 2
    events.append(_narration(seq, "turn 1 " + "prose " * 120))
    seq += 1
    for n in range(2, turns + 1):
        events.append(_continued(seq, f"turn {n} ask", n))
        seq += 1
        events.append(_narration(seq, f"turn {n} " + "prose " * 120))
        seq += 1
    return events


def test_clipping_keeps_the_newest_turns_whole_and_announces_the_elision() -> None:
    t = transcript_from([(T1, _long_chain(6))], budget_tokens=500)
    assert t.truncated and t.elided_turns > 0
    # The anchor: the thread's opening user ask survives every squeeze.
    assert t.turns[0].entries[0].kind == "user"
    assert t.turns[0].entries[0].text == "The opening ask, always kept."
    # The newest turn is whole: its ask AND its prose.
    last = t.turns[-1]
    assert any(e.kind == "user" and e.text == "turn 6 ask" for e in last.entries)
    assert any(e.kind == "assistant" for e in last.entries)
    md = render_thread_md(t)
    assert "earlier turn(s) elided" in md


def test_the_build_is_deterministic() -> None:
    a = transcript_from([(T1, _long_chain(5))], budget_tokens=500)
    b = transcript_from([(T1, _long_chain(5))], budget_tokens=500)
    assert a == b
    assert render_thread_md(a) == render_thread_md(b)


def test_under_budget_renders_are_append_only_prefixes() -> None:
    """The prompt-cache property: turn K's render is a byte-prefix of turn K+1's, so an
    ongoing conversation keeps hitting the provider cache instead of re-reading itself."""
    short = render_thread_md(transcript_from([(T1, _long_chain(3))], budget_tokens=BIG))
    longer = render_thread_md(transcript_from([(T1, _long_chain(4))], budget_tokens=BIG))
    assert longer.startswith(short.rstrip("\n"))


def test_the_renderings_open_with_the_instruction_not_source_wall() -> None:
    t = transcript_from(
        [(T1, [_created(1, "Write it"), _narration(2, "Revenue rose 12%.")])], budget_tokens=BIG
    )
    md = render_thread_md(t)
    assert md.startswith("# The conversation so far")
    assert "not a source" in md and "never cite" in md.lower()
    assert "## Turn 1" in md and "Revenue rose 12%." in md


# ── a steer that never applied ─────────────────────────────────────────────────────────────────

BOB = ActorRef(type="user", id=ActorId(uuid.uuid4()), name="Bob")


def _steer_by(seq: int, directive: str, command_id: uuid.UUID) -> PlanEdited:
    return PlanEdited(
        seq=seq,
        task_id=T1,
        at=NOW,
        actor=BOB,
        edit=PlanEditView(kind=PlanEditKind.STEER, reason=directive, at=NOW, command_id=str(command_id)),
    )


def test_a_retracted_steer_is_never_read_as_instruction() -> None:
    """Bob withdrew it before anything took it: no planner, re-plan or mounted thread reads it."""
    withdrawn, kept = uuid.uuid4(), uuid.uuid4()
    events: list[DurableEvent] = [
        _created(1, "Write the Q3 memo"),
        _steer_by(2, "drop the Germany section", withdrawn),
        _steer_by(3, "use euros", kept),
        SteerRetracted(seq=4, task_id=T1, at=NOW, actor=BOB, command_id=withdrawn),
        _narration(5, "Done."),
    ]
    assert _texts(events) == [
        ("user", "Write the Q3 memo"),
        ("steering", "use euros"),
        ("assistant", "Done."),
    ]
    assert "Germany" not in render_thread_md(transcript_from([(T1, events)], budget_tokens=BIG))


def test_a_rejected_steer_reads_as_not_applied() -> None:
    """The turn ended before it applied: the next turn's planner reads that it never did."""
    late = uuid.uuid4()
    events: list[DurableEvent] = [
        _created(1, "Write the Q3 memo"),
        _narration(2, "Done."),
        _steer_by(3, "use euros", late),
        SteerRejected(seq=4, task_id=T1, at=NOW, command_id=late, reason=REJECT_TURN_ENDED),
        _delivered(5, "Done."),
        _continued(6, "now shorten it", 2),
    ]
    md = render_thread_md(transcript_from([(T1, events)], budget_tokens=BIG))
    assert "**Bob steered:**" not in md
    assert "*[not applied: the turn ended before this applied] Bob steered: use euros*" in md


def test_a_fork_carries_only_the_steers_its_parent_had_applied() -> None:
    """A fork carries only applied instructions: a steer still open where the fork was made belongs
    to the parent's run, so the fork reads that it was not carried, never the instruction. One the
    parent had applied by then is the fork's too. The tip's own open steer is still instruction."""
    open_one, applied_one, own = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    child = TaskId(uuid.uuid4())
    parent: list[DurableEvent] = [
        _created(1, "Write the Q3 memo"),
        _steer_by(2, "cover Germany only", applied_one),
        SteerApplied(
            seq=3, task_id=T1, at=NOW, command_id=applied_one, author="Bob", safe_point=SafePoint.BEFORE_MODEL
        ),
        _narration(4, "Drafting the German section."),
        _steer_by(5, "use euros", open_one),  # the fork is made before the parent's run takes it
    ]
    fork: list[DurableEvent] = [
        _created(1, "Write the Q3 memo", task_id=child),
        PlanEdited(
            seq=2,
            task_id=child,
            at=NOW,
            actor=BOB,
            edit=PlanEditView(kind=PlanEditKind.STEER, reason="add a table", at=NOW, command_id=str(own)),
        ),
    ]
    md = render_thread_md(transcript_from([(T1, parent), (child, fork)], budget_tokens=BIG))
    assert "**Bob steered:** cover Germany only" in md
    assert "**Bob steered:** use euros" not in md
    assert "*[not carried: the fork was made before this applied] Bob steered: use euros*" in md
    assert "**Bob steered:** add a table" in md


def test_a_turn_with_a_withdrawn_steer_is_still_a_prefix_of_the_next() -> None:
    """A retract or a rejection lands in its own steer's turn (a turn settles every steer before it
    ends), so the turn renders the same once it is over and once the next one starts: the
    prompt-cache prefix holds across turns."""
    withdrawn, late = uuid.uuid4(), uuid.uuid4()
    turn_1: list[DurableEvent] = [
        _created(1, "Write the Q3 memo"),
        _steer_by(2, "drop the Germany section", withdrawn),
        SteerRetracted(seq=3, task_id=T1, at=NOW, actor=BOB, command_id=withdrawn),
        _narration(4, "Done."),
        _steer_by(5, "use euros", late),
        SteerRejected(seq=6, task_id=T1, at=NOW, command_id=late, reason=REJECT_TURN_ENDED),
        _delivered(7, "Done."),
    ]
    turn_2: list[DurableEvent] = [_continued(8, "now shorten it", 2), _narration(9, "Shorter.")]
    once = render_thread_md(transcript_from([(T1, turn_1)], budget_tokens=BIG))
    then = render_thread_md(transcript_from([(T1, turn_1 + turn_2)], budget_tokens=BIG))
    assert then.startswith(once.rstrip("\n"))


def test_an_oversized_newest_turn_drops_its_prose_oldest_first_and_keeps_the_ask() -> None:
    # The newest turn carries three narrations, together over the budget: its oldest prose goes
    # first, the ask stays, and the short turn before it still fits beside what is left.
    events: list[DurableEvent] = [_created(1, "The opening ask, always kept."), _narration(2, "short")]
    events.append(_continued(3, "turn 2 ask", 2))
    events += [_narration(seq, f"part {seq} " + "prose " * 120) for seq in (4, 5, 6)]
    t = transcript_from([(T1, events)], budget_tokens=300)
    first, last = t.turns
    assert [e.seq for e in first.entries] == [1, 2]
    assert [(e.kind, e.seq) for e in last.entries] == [("user", 3), ("assistant", 6)]
    # The clipped turn is a copy, so the original newest turn counts as elided.
    assert t.truncated and t.elided_turns == 1
    assert t.total_tokens == sum(e.tokens for turn in t.turns for e in turn.entries)


def test_a_lone_turn_over_budget_keeps_only_the_ask_behind_the_anchor() -> None:
    # One turn only: its prose is dropped whole, and the opening ask is anchored in front of the
    # clipped copy of the same turn.
    long = "start " + "prose " * 400 + "the conclusion"
    t = transcript_from([(T1, [_created(1, "Ask"), _narration(2, long)])], budget_tokens=200)
    assert [[(e.kind, e.text) for e in turn.entries] for turn in t.turns] == [
        [("user", "Ask")],
        [("user", "Ask")],
    ]
    assert t.truncated and t.elided_turns == 1


def test_a_skipped_question_is_told_as_its_default_never_as_an_answer() -> None:
    """The dock's Skip on an optional question stores its default: the thread says so, on an accept
    and on a decline alike, and only what the person typed is theirs."""
    gate = GateId(uuid.uuid4())
    asked = GateOpened(
        seq=2,
        task_id=T1,
        at=NOW,
        gate_id=gate,
        kind=GateKind.INPUT,
        title="Intake",
        questions=QuestionSet(
            title="Intake",
            questions=[
                Question(id="q1", prompt="Who reads it?", type=QuestionType.TEXT),
                Question(id="q2", prompt="Which currency?", type=QuestionType.TEXT, default="USD"),
            ],
            expires_at=NOW,
        ),
    )
    ada = ActorRef(type="user", id=ActorId(uuid.uuid4()), name="Ada")
    content: dict[str, str | int | float | bool | list[str]] = {"q1": "The board", "q2": "USD"}
    for action in (GateAction.ACCEPT, GateAction.DECLINE):
        resolved = GateResolved(
            seq=3,
            task_id=T1,
            at=NOW,
            gate_id=gate,
            actor=ada,
            action=action,
            content=content,
            defaulted=["q2"],
        )
        qa = [text for kind, text in _texts([_created(1, "memo"), asked, resolved]) if kind == "qa"]
        assert qa == [
            "**Asked:** Who reads it? **Ada answered:** The board",
            "**Asked:** Which currency? **Default used:** USD",
        ], action


def test_a_decline_recorded_before_defaulted_was_all_defaults() -> None:
    gate = GateId(uuid.uuid4())
    asked = GateOpened(
        seq=2,
        task_id=T1,
        at=NOW,
        gate_id=gate,
        kind=GateKind.INPUT,
        title="Intake",
        questions=QuestionSet(
            title="Intake",
            questions=[Question(id="q2", prompt="Which currency?", type=QuestionType.TEXT, default="USD")],
            expires_at=NOW,
        ),
    )
    resolved = GateResolved(
        seq=3, task_id=T1, at=NOW, gate_id=gate, action=GateAction.DECLINE, content={"q2": "USD"}
    )
    qa = [text for kind, text in _texts([_created(1, "memo"), asked, resolved]) if kind == "qa"]
    assert qa == ["**Asked:** Which currency? **Default used:** USD"]
