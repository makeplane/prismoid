# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Intake: one turn's rows in, its facts and its bypass out, as pure decision rows."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from prismoid.operations.turn.intake import Bypass, TurnRows, dial_kind, intake_of
from prismoid.schemas.events import DurableEventAdapter, TaskContinued, TaskCreated
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.lead import LeadEndingKind, TurnOrigin
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import TaskMode

REPLY, PLAN, ASK, PROPOSE, FAIL, DELIVER, DECLINE = (
    LeadEndingKind.REPLY,
    LeadEndingKind.PLAN,
    LeadEndingKind.ASK,
    LeadEndingKind.PROPOSE,
    LeadEndingKind.FAIL,
    LeadEndingKind.DELIVER,
    LeadEndingKind.DECLINE,
)
NOTES = TaskInput(filename="notes.md", sha256="0" * 64, bytes=900, media_type="text/markdown")
LONG_TEXT = TaskInput(filename="log.txt", sha256="1" * 64, bytes=50_000, media_type="text/plain")
SCAN = TaskInput(filename="scan.pdf", sha256="2" * 64, bytes=900, media_type="application/pdf")
PHOTO = TaskInput(filename="photo.png", sha256="3" * 64, bytes=900, media_type="image/png")


def rows(**changes: Any) -> TurnRows:
    """A plain first turn in the app: no dial, no attachment, no ceiling; `changes` set the rest."""
    base = TurnRows(
        turn=1,
        mode=TaskMode.STANDARD,
        clarify="auto",
        from_routine=False,
        from_channel=False,
        from_api=False,
        direct_bot=None,
        bot_pins=False,
        connectors=False,
        dial=None,
        carried_dial=None,
        deliverable_format=None,
        scale="auto",
        target_pages=None,
        inputs=(),
        budget_usd=None,
        spent_usd=Decimal(0),
        asked=False,
    )
    return dataclasses.replace(base, **changes)


def test_a_plain_turn_is_the_leads_with_every_attended_ending() -> None:
    got = intake_of(rows())
    assert got.bypass is None
    assert got.facts.origin is TurnOrigin.WEB and not got.facts.unattended
    assert got.facts.allowed_endings() == {REPLY, PLAN, ASK, PROPOSE, DECLINE}


@pytest.mark.parametrize(
    ("changes", "origin", "unattended"),
    [
        ({"from_routine": True}, TurnOrigin.ROUTINE, True),
        ({"from_routine": True, "turn": 2}, TurnOrigin.WEB, False),  # a person continued the fire's task
        ({"from_channel": True}, TurnOrigin.CHANNEL, False),
        ({"from_channel": True, "turn": 3}, TurnOrigin.WEB, False),
        ({"from_api": True}, TurnOrigin.API, False),
        ({"from_api": True, "turn": 2}, TurnOrigin.API, False),
        ({"from_api": True, "from_routine": True}, TurnOrigin.ROUTINE, True),  # a fire through its API key
    ],
)
def test_the_origin_is_where_the_turn_came_from(
    changes: dict[str, Any], origin: TurnOrigin, unattended: bool
) -> None:
    facts = intake_of(rows(**changes)).facts
    assert facts.origin is origin and facts.unattended is unattended


def test_a_routine_fire_never_asks_proposes_or_writes_free_text() -> None:
    facts = intake_of(rows(from_routine=True, clarify="never")).facts
    assert facts.allowed_endings() == {DELIVER, PLAN, FAIL}


@pytest.mark.parametrize(
    ("changes", "bypass"),
    [
        ({"direct_bot": "scribe"}, Bypass.DIRECT),
        ({"direct_bot": "scribe", "turn": 2}, None),
        ({"mode": TaskMode.FLEET}, Bypass.FLEET),
        ({"mode": TaskMode.COUNCIL}, Bypass.FLEET),
        ({"mode": TaskMode.FLEET, "turn": 2}, None),
        ({"mode": TaskMode.FLEET, "turn": 2, "dial": "document"}, None),
        ({"direct_bot": "scribe", "turn": 2, "dial": "document"}, None),
        ({"scale": "extended"}, None),  # its midband is one run's words
        ({"scale": "extended", "target_pages": 25}, Bypass.LONGFORM),
        ({"scale": "long", "dial": "deck"}, Bypass.DIAL),  # a long deck is one run's words
        ({"scale": "book", "dial": "deck"}, Bypass.LONGFORM),
        ({"target_pages": 40}, Bypass.LONGFORM),
        ({"turn": 2, "scale": "book", "carried_dial": "spreadsheet"}, Bypass.LONGFORM),
        ({"turn": 2, "scale": "long", "carried_dial": "code"}, None),  # the dialed kind sizes the turn
        ({"scale": "standard"}, None),
        ({"scale": "long", "dial": "code"}, Bypass.DIAL),  # code is one step at every size
        ({"scale": "long", "dial": "document", "deliverable_format": "html"}, Bypass.DIAL),
        ({"dial": "document"}, Bypass.DIAL),
        ({"dial": "spreadsheet", "turn": 2}, Bypass.DIAL),
        ({"dial": "document", "connectors": True}, None),  # a service is not one step's material
        ({"dial": "document", "bot_pins": True}, None),
        ({"carried_dial": "document", "turn": 2}, None),  # an earlier turn's dial binds no reply
        ({"dial": "webapp"}, None),  # not a kind a dial offers
    ],
)
def test_the_bypass_is_a_code_made_plan_or_none(changes: dict[str, Any], bypass: Bypass | None) -> None:
    assert intake_of(rows(**changes)).bypass is bypass


def test_a_dial_the_lead_meets_needs_hands_or_a_question() -> None:
    facts = intake_of(rows(dial="document", deliverable_format="pdf", connectors=True)).facts
    assert facts.deliverable is ArtifactType.DOCUMENT and facts.deliverable_format == "pdf"
    assert facts.allowed_endings() == {PLAN, ASK, DECLINE}


def test_a_carried_dial_leaves_a_later_turn_free_to_reply() -> None:
    facts = intake_of(rows(turn=2, carried_dial="deck")).facts
    assert facts.deliverable is None and REPLY in facts.allowed_endings()


def test_a_format_rides_only_a_document_dial() -> None:
    assert intake_of(rows(dial="deck", deliverable_format="pdf")).facts.deliverable_format is None


def test_a_size_the_lead_meets_is_a_fact_it_reads_and_no_size_is_none() -> None:
    assert intake_of(rows(scale="standard")).facts.size == "standard · ~5 pages"
    assert intake_of(rows(target_pages=2)).facts.size == "brief · ~2 pages"
    assert intake_of(rows()).facts.size is None


def test_unread_inputs_are_counted_on_the_turn_that_attached_them() -> None:
    first = intake_of(rows(inputs=(NOTES, PHOTO))).facts
    assert first.inputs_unread == 1 and first.allowed_endings() == {PLAN, ASK, DECLINE}
    assert intake_of(rows(inputs=(NOTES,))).facts.inputs_unread == 0
    assert intake_of(rows(turn=2, inputs=(NOTES, PHOTO, PHOTO))).facts.inputs_unread == 2


def test_a_long_text_or_a_document_is_the_leads_to_read_not_a_plan() -> None:
    """Text past 4 KB and a PDF are read for the Lead (bounded by tokens once read), so the reply stays open."""
    facts = intake_of(rows(inputs=(NOTES, LONG_TEXT, SCAN))).facts
    assert facts.inputs_unread == 0 and REPLY in facts.allowed_endings()


@pytest.mark.parametrize("kind", ["task.created", "task.continued"])
def test_a_frame_from_before_the_turn_fields_decodes_as_a_web_turn_that_attached_nothing(kind: str) -> None:
    old = {"type": kind, "seq": 1, "task_id": str(uuid.uuid4()), "at": "2026-01-01T00:00:00Z", "actor": None}
    old |= {"goal": "g", "mode": "standard"} if kind == "task.created" else {"message": "m", "turn": 2}
    frame = DurableEventAdapter.validate_python(old)
    assert isinstance(frame, TaskCreated | TaskContinued) and frame.credential is None
    assert getattr(frame, "attached", None) is None


@pytest.mark.parametrize(
    ("budget", "spent", "left"),
    [
        (Decimal("2.00"), Decimal("0.50"), Decimal("1.50")),
        (Decimal("2.00"), Decimal("3.10"), Decimal(0)),
        (None, Decimal("9.00"), None),
    ],
)
def test_the_budget_left_is_the_ceiling_less_the_spend(
    budget: Decimal | None, spent: Decimal, left: Decimal | None
) -> None:
    assert intake_of(rows(budget_usd=budget, spent_usd=spent)).facts.budget_left_usd == left


def test_a_turn_that_asked_does_not_ask_again() -> None:
    assert ASK not in intake_of(rows(asked=True)).facts.allowed_endings()


def test_clarify_always_asks_first_and_never_asks_nothing() -> None:
    assert intake_of(rows(clarify="always")).facts.allowed_endings() == {ASK}
    assert ASK not in intake_of(rows(clarify="never")).facts.allowed_endings()


def test_a_stored_value_outside_the_vocabulary_is_refused() -> None:
    with pytest.raises(ValidationError):
        intake_of(rows(clarify="sometimes"))


def test_dial_kind_reads_only_dialable_kinds() -> None:
    assert dial_kind("collection") is ArtifactType.COLLECTION
    assert dial_kind(None) is None and dial_kind("fleet_report") is None
