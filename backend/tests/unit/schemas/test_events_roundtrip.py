# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every event type parses - from the wire, back to the wire, unchanged.

This is the frontend's contract tested against itself. If a field name drifts here, a
client that was rendering it stops rendering it, silently. The test is deliberately exhaustive
over the union rather than over a sample: a new event type that nobody round-trips is a new
event type nobody can render.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel

from prismoid.schemas import events as ev
from tests.support.samples import QUESTIONS, SAMPLES, STEP, TASK


def test_every_event_type_has_a_sample() -> None:
    """The union and the samples agree. A new event type with no sample fails here."""
    assert {type(s).__name__ for s in SAMPLES} == {
        c.__name__ for c in (*ev.DURABLE_TYPES, *ev.LIVE_TYPES, *ev.PRESENCE_TYPES, *ev.SCREEN_TYPES)
    }


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: str(type(s).__name__))
def test_roundtrips_through_the_wire(sample: BaseModel) -> None:
    """Serialize as the socket does, parse as a client does, and get the same value back."""
    wire = sample.model_dump_json()
    parsed = ev.TaskEventAdapter.validate_json(wire)
    assert parsed == sample
    assert parsed.model_dump_json() == wire


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: str(type(s).__name__))
def test_type_is_the_discriminator_and_the_sse_event_name(sample: BaseModel) -> None:
    """One string, one meaning: the discriminator IS the SSE `event:` name."""
    name = sample.model_dump()["type"]
    assert name in ev.event_type_names()
    assert ev.TaskEventAdapter.validate_python(sample.model_dump()) == sample


def test_only_durable_frames_carry_a_seq() -> None:
    """The id rule, asserted on the schema: a Lane-2 delta cannot advance Last-Event-ID
    because it has no seq to advance it with."""
    for cls in ev.DURABLE_TYPES:
        assert "seq" in cls.model_fields
        assert "after_seq" not in cls.model_fields
    for cls in ev.LIVE_TYPES:
        assert "seq" not in cls.model_fields
        assert "after_seq" in cls.model_fields
    for cls in ev.PRESENCE_TYPES:  # state about the room: not even an anchor on the record
        assert "seq" not in cls.model_fields
        assert "after_seq" not in cls.model_fields
    for cls in ev.SCREEN_TYPES:  # state about the browser: the same
        assert "seq" not in cls.model_fields
        assert "after_seq" not in cls.model_fields


def test_is_durable_partitions_the_union() -> None:
    durable = [s for s in SAMPLES if ev.is_durable(s)]
    live = [s for s in SAMPLES if not ev.is_durable(s)]
    assert len(durable) == len(ev.DURABLE_TYPES)
    assert len(live) == len(ev.LIVE_TYPES) + len(ev.PRESENCE_TYPES) + len(ev.SCREEN_TYPES)
    assert all(s.model_dump()["lane"] == "record" for s in durable)
    assert all(s.model_dump()["lane"] == "live" for s in live)


def test_every_task_scoped_event_carries_task_and_step(sample: None = None) -> None:
    """Rule 10, asserted rather than remembered. Presence and `screen.state` name the task and no
    step; `screen.action` names the step whose verb acted."""
    for s in SAMPLES:
        dumped = s.model_dump()
        assert dumped["task_id"] == TASK
        stepless = isinstance(s, (*ev.PRESENCE_TYPES, ev.ScreenState))
        assert dumped["step_id"] == (None if stepless else STEP)


def test_questions_are_numbered_one_to_n() -> None:
    """The canonical rendering. `1: b, 2: yes` is a grammar, so the numbering is a contract."""
    assert [n for n, _ in QUESTIONS.numbered()] == [1, 2]
    assert QUESTIONS.numbered()[0][1].id == "currency"


def test_money_survives_the_wire_as_a_decimal() -> None:
    """The ledger reconciles to the cent - so a cost must not become a float in transit."""
    usage = next(s for s in SAMPLES if isinstance(s, ev.UsageRecorded))
    parsed = ev.TaskEventAdapter.validate_json(usage.model_dump_json())
    assert isinstance(parsed, ev.UsageRecorded)
    assert parsed.cost_usd == Decimal("0.0123")
    assert parsed.cost_usd.as_tuple().exponent == -4
