# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's seams: every ending round-trips, the wire additions read old rows, and the facts narrow the endings."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TypedDict
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from prismoid.schemas import events as ev
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.lead import (
    AskUser,
    Decline,
    Deliver,
    Fail,
    FailKind,
    LeadEnding,
    LeadEndingKind,
    LeadFacts,
    ProposeTask,
    Reply,
    SubmitPlan,
    TurnOrigin,
)
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    GateKind,
    PlanEdit,
    PlanEditKind,
    PlanProposal,
    PlanView,
    ProposedQuestion,
    ProposedStep,
    QuestionType,
    StepState,
)

ENDINGS: TypeAdapter[LeadEnding] = TypeAdapter(LeadEnding)
NOW = dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.UTC)


class Env(TypedDict):
    """The Lane-1 envelope as typed kwargs."""

    task_id: TaskId
    step_id: StepId
    at: dt.datetime
    seq: int


ENV = Env(task_id=TaskId(uuid4()), step_id=StepId(uuid4()), at=NOW, seq=4)


def _parse(wire: dict[str, object]) -> ev.TaskEvent:
    return ev.TaskEventAdapter.validate_python(wire)


STEP = ProposedStep(label="s1", agent="writer", title="Draft", goal="Write the memo")

EVERY_ENDING = [
    Reply(text="Paris."),
    SubmitPlan(say="I'll draft the memo now.", plan=PlanProposal(steps=[STEP])),
    AskUser(
        context="The tone decides the whole memo.",
        questions=[
            ProposedQuestion(
                prompt="Formal or casual?", type=QuestionType.SINGLE_SELECT, options=["formal", "casual"]
            )
        ],
    ),
    AskUser(kind=GateKind.CONNECT, context="Your inbox holds the thread.", service="gmail"),
    ProposeTask(
        answer="Here are the three points.", goal="A one-page memo on Q3", deliverable=ArtifactType.DOCUMENT
    ),
    Fail(kind=FailKind.NEEDS_OWNER, reason="The site needs a sign-in nobody can give at 3 a.m."),
    Deliver(result="No new alerts since 09:00.", findings=False),
    Decline(why="refused", answer="I won't help find a private person's home address."),
]


@pytest.mark.parametrize("ending", EVERY_ENDING, ids=lambda e: f"{e.ending}")
def test_every_ending_round_trips_through_the_union(ending: LeadEnding) -> None:
    wire = ENDINGS.dump_json(ending)
    back = ENDINGS.validate_json(wire)
    assert back == ending
    assert type(back) is type(ending)


def test_the_discriminator_names_each_ending_once() -> None:
    kinds = {e.ending for e in EVERY_ENDING}
    assert kinds == set(LeadEndingKind)


def test_the_model_never_sees_the_discriminator() -> None:
    for model in (Reply, SubmitPlan, AskUser, ProposeTask, Fail, Deliver):
        assert "ending" not in model.model_json_schema()["properties"]


def test_an_ending_cannot_claim_another_kind() -> None:
    with pytest.raises(ValidationError):
        ENDINGS.validate_python({"ending": "plan", "text": "Paris."})


@pytest.mark.parametrize(
    "bad",
    [
        {"context": "why", "questions": []},
        {"context": "why", "questions": [{"prompt": "q?", "type": "text"}], "service": "gmail"},
        {"kind": "connect", "context": "why", "service": " "},
        {
            "kind": "connect",
            "context": "why",
            "service": "gmail",
            "questions": [{"prompt": "q?", "type": "text"}],
        },
        {"kind": "approve", "context": "why", "questions": [{"prompt": "q?", "type": "text"}]},
    ],
)
def test_an_ask_has_exactly_one_shape(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AskUser.model_validate(bad)


def test_a_plan_has_steps() -> None:
    with pytest.raises(ValidationError, match="at least one step"):
        SubmitPlan(say="On it.", plan=PlanProposal())


def test_a_proposal_offers_only_a_kind_the_dial_accepts() -> None:
    with pytest.raises(ValidationError):
        ProposeTask.model_validate({"answer": "a", "goal": "g", "deliverable": "dataset"})


def test_a_proposal_names_a_format_only_for_a_document() -> None:
    ProposeTask(answer="a", goal="g", deliverable=ArtifactType.DOCUMENT, format="docx")
    with pytest.raises(ValidationError, match="deliverable=document"):
        ProposeTask(answer="a", goal="g", deliverable=ArtifactType.DECK, format="docx")


def test_facts_round_trip() -> None:
    facts = LeadFacts(
        turn=2,
        origin=TurnOrigin.ROUTINE,
        unattended=True,
        clarify="never",
        deliverable=ArtifactType.DECK,
        deliverable_format=None,
        inputs_unread=1,
        direct_bot="reviewer",
        budget_left_usd=Decimal("1.25"),
    )
    assert LeadFacts.model_validate_json(facts.model_dump_json()) == facts


R, P, A, PR, F, D, DC = (
    LeadEndingKind.REPLY,
    LeadEndingKind.PLAN,
    LeadEndingKind.ASK,
    LeadEndingKind.PROPOSE,
    LeadEndingKind.FAIL,
    LeadEndingKind.DELIVER,
    LeadEndingKind.DECLINE,
)


@pytest.mark.parametrize(
    ("facts", "allowed"),
    [
        ({}, {R, P, A, PR, DC}),
        ({"clarify": "never"}, {R, P, PR, DC}),
        ({"asked": True}, {R, P, PR, DC}),
        ({"clarify": "always"}, {A}),
        ({"clarify": "always", "turn": 2}, {R, P, A, PR, DC}),
        ({"clarify": "always", "asked": True}, {R, P, PR, DC}),
        ({"deliverable": "deck"}, {P, A, DC}),
        ({"inputs_unread": 2}, {P, A, DC}),
        ({"origin": "routine", "unattended": True, "clarify": "never"}, {D, P, F}),
        ({"origin": "routine", "unattended": True, "clarify": "always"}, {D, P, F}),
        ({"origin": "routine", "unattended": True, "deliverable": "spreadsheet"}, {P, F}),
    ],
)
def test_the_facts_narrow_the_endings(facts: dict[str, object], allowed: set[LeadEndingKind]) -> None:
    turn = LeadFacts.model_validate({"turn": 1, "origin": "web", **facts})
    assert turn.allowed_endings() == allowed


def test_an_unattended_delivery_wears_its_findings_and_old_rows_read_none() -> None:
    frame = ev.StepStateChanged(
        **ENV, state=StepState.DONE, agent="lead", ending=LeadEndingKind.DELIVER, findings=True
    )
    wire = frame.model_dump(mode="json")
    assert wire["ending"] == "deliver" and wire["findings"] is True
    del wire["findings"]
    old = _parse(wire)
    assert isinstance(old, ev.StepStateChanged) and old.findings is None


def test_the_step_frame_carries_the_ending_and_old_rows_read_none() -> None:
    frame = ev.StepStateChanged(**ENV, state=StepState.DONE, agent="lead", ending=LeadEndingKind.ASK)
    wire = frame.model_dump(mode="json")
    assert wire["ending"] == "ask"
    assert _parse(wire) == frame
    del wire["ending"]
    old = _parse(wire)
    assert isinstance(old, ev.StepStateChanged)
    assert old.ending is None


def test_the_lead_wire_values_round_trip() -> None:
    planned = ev.TaskPlanned(
        **ENV, plan=PlanView(), route="standard", route_source="lead", route_why="On it."
    )
    assert _parse(planned.model_dump(mode="json")) == planned
    edit = ev.PlanEdited(**ENV, edit=PlanEdit(kind=PlanEditKind.LEAD, reason="the Lead planned", at=NOW))
    wire = edit.model_dump(mode="json")
    assert wire["edit"]["kind"] == "lead"
    assert _parse(wire) == edit


def test_route_source_still_reads_a_retired_value() -> None:
    old = ev.TaskPlanned(**ENV, plan=PlanView(), route_source="classifier").model_dump(mode="json")
    parsed = _parse(old)
    assert isinstance(parsed, ev.TaskPlanned)
    assert parsed.route_source == "classifier"


def test_an_old_sectioned_route_reads_as_the_longform_lane() -> None:
    old: dict[str, object] = {
        **ev.TaskPlanned(**ENV, plan=PlanView()).model_dump(mode="json"),
        "route": "sectioned",
    }
    parsed = _parse(old)
    assert isinstance(parsed, ev.TaskPlanned)
    assert parsed.route == "longform"
