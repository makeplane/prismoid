# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The join law (ADR-028), asserted exhaustively - `execution/joins.decide` is pure and total,
so this file walks its whole truth table. If someone bends a join to make a bug go away, a row
here fails and asks them to change the law first.
"""

from __future__ import annotations

import uuid
from itertools import product

import pytest

from prismoid.operations.execution.joins import JoinDecision, decide
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import StepSpec, StepState

D, R, S, E = StepState.DONE, StepState.RUNNING, StepState.SKIPPED, StepState.ESCALATED


def spec(
    join: str = "all", *, tolerant: bool = False, primary: bool = False, quorum: float | None = None
) -> StepSpec:
    return StepSpec(
        join=join,  # pyright: ignore[reportArgumentType]
        join_quorum=quorum,
        tolerates_missing_inputs=tolerant,
        primary_input=StepId(uuid.uuid4()) if primary else None,
    )


# ── join="all" (the pre-028 contract, byte-identical) ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("states", "verdict"),
    [
        ([D, D, D], JoinDecision.PROMOTE),  # every producer done → run
        ([D, D, R], JoinDecision.WAIT),  # one still in flight → wait
        ([D, D, S], JoinDecision.WAIT),  # strict: a skipped producer never settles an "all" join
        ([R, R, R], JoinDecision.WAIT),
        ([D], JoinDecision.PROMOTE),
    ],
)
def test_all_join(states: list[StepState], verdict: JoinDecision) -> None:
    assert decide(spec("all"), states) is verdict


@pytest.mark.parametrize(
    ("states", "verdict"),
    [
        ([D, S, E], JoinDecision.PROMOTE),  # the fleet chair: compares the answers that exist
        ([S, S, E], JoinDecision.WAIT),  # …but never tabulates an empty room
        ([D, R, S], JoinDecision.WAIT),  # a seat still running → not settled
        ([D, D, D], JoinDecision.PROMOTE),
    ],
)
def test_tolerant_all_join(states: list[StepState], verdict: JoinDecision) -> None:
    assert decide(spec("all", tolerant=True), states) is verdict


# ── join="quorum" / "consensus": settled + ≥1 done promotes; the FLOOR is the executor's ─────


@pytest.mark.parametrize("join", ["quorum", "consensus"])
@pytest.mark.parametrize(
    ("states", "verdict"),
    [
        ([D, D, D, D], JoinDecision.PROMOTE),
        ([D, S, S, S], JoinDecision.PROMOTE),  # promotion never strands a settled graph -
        # the quorum floor is assembly_ready_guard's refusal, with a diagnosis
        ([S, S, S, S], JoinDecision.PROMOTE),  # all settled, zero done: PROMOTE so the executor
        # refuses legibly (assembly_ready_guard / an empty merge) - never a silent WAIT-forever
        # on a graph with nothing left to complete (the deadlock fix; the floor is the executor's)
        ([D, D, R, S], JoinDecision.WAIT),  # still in flight
    ],
)
def test_quorum_and_consensus_joins(join: str, states: list[StepState], verdict: JoinDecision) -> None:
    assert decide(spec(join, quorum=0.8 if join == "quorum" else None), states) is verdict


# ── primary_input outranks everything ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("primary_state", "states", "verdict"),
    [
        (S, [D, D, R], JoinDecision.SKIP),  # its section died → the harmonize pass dies with it,
        (E, [D, D, D], JoinDecision.SKIP),  # immediately - dependents see it settled sooner
        (R, [D, D, D], JoinDecision.WAIT),  # its section still writing → wait for IT, not the room
        (D, [D, D, D], JoinDecision.PROMOTE),
        (D, [D, R, D], JoinDecision.WAIT),  # primary done but a neighbour still in flight
        (None, [D, D, D], JoinDecision.WAIT),  # unresolved primary = not-yet-done; the CALLER
        # maps a primary that vanished from the graph to SKIPPED before asking the law
    ],
)
def test_primary_input(
    primary_state: StepState | None, states: list[StepState], verdict: JoinDecision
) -> None:
    tolerant = spec("all", tolerant=True, primary=True)
    assert decide(tolerant, states, primary_state) is verdict


# ── totality: every (join, tolerant, 3-state combination) yields a verdict, never raises ─────


def test_the_law_is_total() -> None:
    every_state = list(StepState)
    for join, tolerant, combo in product(
        ["all", "quorum", "consensus"], [False, True], product(every_state, repeat=3)
    ):
        verdict = decide(spec(join, tolerant=tolerant), list(combo))
        assert verdict in JoinDecision
