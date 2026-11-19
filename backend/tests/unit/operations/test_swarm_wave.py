# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scatter tally's graph reads: which map a seat hangs from, which steps make up that map's
wave, and when the wave has settled. Pure over transient rows - no database."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.operations.swarm import seat_map_step, wave_members, wave_settled
from prismoid.schemas.plan import StepSpec, StepState


def _step(role: str | None, state: StepState = StepState.DONE, ordinal: int = 0) -> PlanStep:
    return PlanStep(
        id=uuid.uuid4(),
        spec=StepSpec.model_validate({"lane_role": role}).model_dump(mode="json"),
        state=state.value,
        ordinal=ordinal,
    )


def _edge(a: PlanStep, b: PlanStep) -> PlanEdge:
    return PlanEdge(from_step_id=a.id, to_step_id=b.id, kind="produces_for")


def _specs(*steps: PlanStep) -> dict[uuid.UUID, StepSpec]:
    return {s.id: StepSpec.model_validate(s.spec) for s in steps}


def test_a_seat_hangs_from_its_scatter_map_only() -> None:
    other, map_step, seat = _step("section"), _step("scatter_map"), _step("gather")
    edges = [_edge(other, seat), _edge(map_step, seat)]
    assert seat_map_step(edges, _specs(other, map_step, seat), seat.id) == map_step.id


def test_a_seat_with_no_map_edge_has_no_map() -> None:
    other, seat = _step("section"), _step("gather")
    assert seat_map_step([_edge(other, seat)], _specs(other, seat), seat.id) is None
    assert seat_map_step([], _specs(seat), seat.id) is None


def test_the_wave_is_edge_scoped_to_its_own_map() -> None:
    map1, map2 = _step("scatter_map"), _step("scatter_map")
    g1, g2, syn1 = _step("gather"), _step("gather"), _step("synthesis")
    turn2_seat, turn2_syn = _step("gather"), _step("synthesis")
    steps = [map1, map2, g1, g2, syn1, turn2_seat, turn2_syn]
    edges = [
        _edge(map1, g1),
        _edge(map1, g2),
        _edge(map1, syn1),
        _edge(map2, turn2_seat),
        _edge(map2, turn2_syn),
    ]
    gathers, synthesis = wave_members(steps, edges, _specs(*steps), map1.id)
    assert {r.id for r, _ in gathers} == {g1.id, g2.id}
    assert all(spec.lane_role == "gather" for _, spec in gathers)
    assert synthesis is syn1


def test_a_wave_without_synthesis_or_with_unknown_targets() -> None:
    map_step, seat = _step("scatter_map"), _step("gather")
    ghost = _step("gather")  # an edge target that is not among the task's steps
    steps = [map_step, seat]
    gathers, synthesis = wave_members(
        steps, [_edge(map_step, seat), _edge(map_step, ghost)], _specs(*steps), map_step.id
    )
    assert [r.id for r, _ in gathers] == [seat.id]
    assert synthesis is None


@pytest.mark.parametrize(
    ("states", "settled"),
    [
        ([], False),
        ([StepState.DONE], True),
        ([StepState.DONE, StepState.SKIPPED, StepState.ESCALATED], True),
        ([StepState.DONE, StepState.RUNNING], False),
        ([StepState.READY], False),
        ([StepState.RETRYING, StepState.DONE], False),
    ],
)
def test_a_wave_settles_only_when_every_seat_has(states: list[StepState], settled: bool) -> None:
    gathers = [(s, StepSpec.model_validate(s.spec)) for s in (_step("gather", st) for st in states)]
    assert wave_settled(gathers) is settled
