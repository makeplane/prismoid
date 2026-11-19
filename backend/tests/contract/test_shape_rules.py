# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The shape mechanism itself, with no database and no model: what enters a shape, what stays out,
and that a comparison fails when the record's structure changes. This is the proof that the golden
files in `shapes/` can fail.

The perturbation tests run over every recorded file, so a scenario added later is covered by them
the day its file lands.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from decimal import Decimal

import pytest

from prismoid.schemas.events import (
    DurableEvent,
    StepNarration,
    StepStateChanged,
    TaskCreated,
    TaskMoved,
    TaskPlanned,
    TaskRenamed,
    UsageRecorded,
)
from prismoid.schemas.ids import AgentName, StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import PlanStepView, PlanView, StepKind, StepState, TaskMode
from prismoid.schemas.usage import Unit
from tests.contract.harness import shape
from tests.contract.scenarios.all import SCENARIOS

TASK = TaskId(uuid.uuid4())
NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def narration(seq: int, step: StepId, words: str, at: dt.datetime = NOW) -> StepNarration:
    return StepNarration(seq=seq, task_id=TASK, step_id=step, at=at, markdown=words)


def shaped(*events: DurableEvent) -> shape.Shape:
    return shape.shape_of("t", "delivered", events)


# ── what a shape leaves out ───────────────────────────────────────────────────────────────────


def test_words_ids_times_and_sequence_numbers_are_not_part_of_a_shape() -> None:
    one = shaped(narration(3, StepId(uuid.uuid4()), "It is noon."))
    other = shaped(narration(90, StepId(uuid.uuid4()), "Midday, as it happens.", NOW + dt.timedelta(hours=5)))
    assert one == other


def test_a_step_is_named_by_when_it_first_appeared() -> None:
    first, second = StepId(uuid.uuid4()), StepId(uuid.uuid4())
    seen = shaped(narration(1, first, "a"), narration(2, second, "b"), narration(3, first, "c"))
    assert [row["step"] for row in seen["record"]] == ["s1", "s2", "s1"]


def test_an_event_that_lands_when_a_background_call_finishes_has_no_place_in_the_order() -> None:
    renamed = TaskRenamed(seq=1, task_id=TASK, at=NOW, title="A title")
    before = shaped(renamed, narration(2, StepId(uuid.uuid4()), "x"))
    after = shaped(narration(1, StepId(uuid.uuid4()), "x"), renamed)
    assert before == after
    assert before["loose"] == {"task.renamed": 1}


def test_an_event_that_exists_only_when_a_run_was_slow_is_left_out() -> None:
    billed = UsageRecorded(
        seq=2,
        task_id=TASK,
        at=NOW,
        provider="docker",
        model="machine.base",
        family=Family("machine"),
        units={Unit.SECOND: 1},
        cost_usd=Decimal("0.0001"),
    )
    step = StepId(uuid.uuid4())
    assert shaped(narration(1, step, "x"), billed) == shaped(narration(1, step, "x"))


def test_an_event_type_no_rule_knows_is_refused_rather_than_guessed() -> None:
    moved = TaskMoved(seq=1, task_id=TASK, at=NOW)
    with pytest.raises(KeyError, match="add a row to KEYS"):
        shaped(moved)


def test_a_plan_is_its_route_its_steps_and_the_edges_from_its_rows() -> None:
    a, b = StepId(uuid.uuid4()), StepId(uuid.uuid4())

    def view(step: StepId, agent: str) -> PlanStepView:
        return PlanStepView(
            id=step, title="t", kind=StepKind.STEP, state=StepState.READY, agent=AgentName(agent)
        )

    planned = TaskPlanned(
        seq=1,
        task_id=TASK,
        at=NOW,
        plan=PlanView(steps=[view(a, "researcher"), view(b, "writer")]),
        route="standard",
        route_why="any words at all",
    )
    seen = shape.shape_of("t", "delivered", [planned], edges=[(b, a, "depends_on")])
    (row,) = seen["record"]
    assert row["steps"] == [
        {"id": "s1", "agent": "researcher", "kind": "step"},
        {"id": "s2", "agent": "writer", "kind": "step"},
    ]
    assert "route_why" not in row
    assert seen["edges"] == [["s2", "s1", "depends_on"]]
    assert shape.differences(seen, shape.shape_of("t", "delivered", [planned]))


def test_a_waves_siblings_are_recorded_by_step_whichever_ran_first() -> None:
    """Two seats wired from one map run at once: their rows interleave by timing, and the shape groups
    them by step in the places the wave took. A step that waits on the wave keeps its place."""
    root, left, right, join = (StepId(uuid.uuid4()) for _ in range(4))
    edges = [(root, left, "produces_for"), (root, right, "produces_for")]
    edges += [(left, join, "produces_for"), (right, join, "produces_for")]

    def run(*order: tuple[StepId, str]) -> shape.Shape:
        events = [narration(1, root, "map"), narration(2, left, "x"), narration(3, right, "x")]
        events += [narration(4 + i, step, words) for i, (step, words) in enumerate(order)]
        return shape.shape_of("t", "delivered", [*events, narration(9, join, "end")], edges=edges)

    one = run((left, "a"), (right, "a"), (left, "b"), (right, "b"))
    other = run((right, "a"), (left, "a"), (right, "b"), (left, "b"))
    assert one == other
    assert [row["step"] for row in one["record"]] == ["s1", "s2", "s2", "s2", "s3", "s3", "s3", "s4"]


def test_a_first_waves_siblings_are_recorded_by_step_whichever_ran_first() -> None:
    """A fleet's seats wait on nothing and run at once; the table that waits on them keeps its place."""
    left, right, join = (StepId(uuid.uuid4()) for _ in range(3))
    edges = [(left, join, "produces_for"), (right, join, "produces_for")]

    def run(*order: tuple[StepId, str]) -> shape.Shape:
        events = [narration(1, left, "x"), narration(2, right, "x")]
        events += [narration(3 + i, step, words) for i, (step, words) in enumerate(order)]
        return shape.shape_of("t", "delivered", [*events, narration(9, join, "end")], edges=edges)

    one = run((left, "a"), (right, "a"))
    other = run((right, "a"), (left, "a"))
    assert one == other
    assert [row["step"] for row in one["record"]] == ["s1", "s1", "s2", "s2", "s3"]


def test_steps_in_a_chain_keep_the_order_they_ran_in() -> None:
    first, second = StepId(uuid.uuid4()), StepId(uuid.uuid4())
    events = [narration(1, first, "a"), narration(2, second, "b"), narration(3, first, "c")]
    seen = shape.shape_of("t", "delivered", events, edges=[(first, second, "depends_on")])
    assert [row["step"] for row in seen["record"]] == ["s1", "s2", "s1"]


S1 = StepId(uuid.uuid4())


# ── what the scenarios hold beside the record ────────────────────────────────────────────────


def test_facts_are_part_of_a_shape_only_when_there_are_any() -> None:
    assert "facts" not in shaped(narration(1, S1, "x"))
    held = shape.shape_of("t", "delivered", [narration(1, S1, "x")], {"jobs": ["run_task task-{task}"]})
    assert held["facts"] == {"jobs": ["run_task task-{task}"]}


def test_a_changed_fact_is_a_difference_and_an_absent_one_too() -> None:
    events = [narration(1, S1, "x")]
    was = shape.shape_of("t", "delivered", events, {"jobs": ["run_task task-{task}"]})
    assert not shape.differences(was, was)
    assert shape.differences(was, shape.shape_of("t", "delivered", events, {"jobs": []}))
    assert shape.differences(was, shape.shape_of("t", "delivered", events))


def test_a_branch_records_that_it_has_a_parent_and_never_which() -> None:
    def created(parent: uuid.UUID | None) -> TaskCreated:
        return TaskCreated(
            seq=1,
            task_id=TASK,
            at=NOW,
            goal="g",
            mode=TaskMode.STANDARD,
            parent_task_id=TaskId(parent) if parent else None,
            parent_seq=7 if parent else None,
        )

    (branch,) = shaped(created(uuid.uuid4()))["record"]
    assert branch == {"type": "task.created", "mode": "standard", "parent_task_id": True, "parent_seq": True}
    assert shaped(created(uuid.uuid4())) == shaped(created(uuid.uuid4()))
    assert shaped(created(None))["record"] == [{"type": "task.created", "mode": "standard"}]


# ── a comparison fails when structure changes ─────────────────────────────────────────────────


@pytest.fixture(params=[s.name for s in SCENARIOS])
def recorded(request: pytest.FixtureRequest) -> shape.Shape:
    return shape.read(request.param)


def test_a_shape_matches_itself(recorded: shape.Shape) -> None:
    assert shape.differences(recorded, copy.deepcopy(recorded)) == []


def test_a_lost_event_is_a_difference_wherever_it_was(recorded: shape.Shape) -> None:
    for i in range(len(recorded["record"])):
        seen = copy.deepcopy(recorded)
        del seen["record"][i]
        assert shape.differences(recorded, seen), f"losing event {i + 1} went unnoticed"


def test_an_extra_event_is_a_difference(recorded: shape.Shape) -> None:
    seen = copy.deepcopy(recorded)
    seen["record"].insert(1, {"type": "step.narration", "step": "s1"})
    assert shape.differences(recorded, seen)


def test_two_events_in_the_other_order_are_a_difference(recorded: shape.Shape) -> None:
    rows = recorded["record"]
    for i in range(len(rows) - 1):
        if rows[i] == rows[i + 1]:
            continue
        seen = copy.deepcopy(recorded)
        seen["record"][i], seen["record"][i + 1] = seen["record"][i + 1], seen["record"][i]
        assert shape.differences(recorded, seen), f"swapping events {i + 1} and {i + 2} went unnoticed"


def test_a_changed_field_is_a_difference(recorded: shape.Shape) -> None:
    for i, row in enumerate(recorded["record"]):
        for field, value in row.items():
            if field == "type":
                continue
            seen = copy.deepcopy(recorded)
            seen["record"][i][field] = "changed" if isinstance(value, str) else 99
            assert shape.differences(recorded, seen), f"event {i + 1}'s {field} went unnoticed"


def test_a_different_ending_is_a_difference(recorded: shape.Shape) -> None:
    assert shape.differences(recorded, recorded | {"outcome": "failed"})


def test_a_named_variance_passes_and_only_that_one() -> None:
    now = {"outcome": "delivered", "loose": {}, "record": [{"type": "task.planned", "route_source": "lead"}]}
    was = {
        "outcome": "delivered",
        "loose": {},
        "record": [{"type": "task.planned", "route_source": "anchor"}],
    }
    assert shape.differences(was, now)
    assert not shape.differences(was, now, {"task.planned.route_source": frozenset({"lead"})})
    assert shape.differences(was, now, {"task.planned.route": frozenset({"lead"})})
    assert shape.differences(was, now, {"task.planned.route_source": frozenset({"hatch"})})


def test_a_step_that_changes_state_is_named_in_the_difference() -> None:
    step = StepId(uuid.uuid4())

    def run(state: StepState) -> shape.Shape:
        return shaped(
            StepStateChanged(seq=1, task_id=TASK, step_id=step, at=NOW, state=state, agent="writer")
        )

    (message,) = shape.differences(run(StepState.DONE), run(StepState.ESCALATED))
    assert "step.state" in message and "'done'" in message and "'escalated'" in message


# ── the files and the scenarios ───────────────────────────────────────────────────────────────


def test_every_recorded_shape_has_its_scenario_and_back() -> None:
    assert shape.names() == sorted(s.name for s in SCENARIOS)
