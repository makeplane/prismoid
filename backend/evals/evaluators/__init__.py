# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The evaluators: one turn's trace scored against its row.

`route_cost` scores the outcome, `budget_verdicts` the limits, `run_checks` the deterministic checks,
and `passes` is all three together, for a turn that ended: it delivered, or it stopped at a gate.

The golden-document suite's evaluators (`Scores`, `score`) live in `evals.evaluators.scores`.
"""

from __future__ import annotations

from evals.evaluators.budget import budget_verdicts
from evals.evaluators.checks import run_checks
from evals.evaluators.outcome import route_cost
from evals.schema import Outcome, OutcomeRow
from evals.trace import PrismoidTrace

# The outcomes whose whole answer is the delivered text: an empty one answered nothing.
ANSWERED_IN_TEXT: frozenset[Outcome] = frozenset({Outcome.REPLY, Outcome.LIGHT_WORK})


def ended(trace: PrismoidTrace) -> bool:
    """Whether the turn reached an ending a person sees: a delivery, or a gate waiting on them."""
    return trace.delivered or bool(trace.gates)


def passes(row: OutcomeRow, trace: PrismoidTrace) -> bool:
    """The turn ended, a text answer is not empty, its outcome is acceptable, every limit held and no
    decided check failed."""
    cost = route_cost(row, trace)
    if cost is None:
        raise ValueError(f"row {row.id} is not scored: it expects {row.expected_outcome.value!r}")
    return (
        ended(trace)
        and not (trace.outcome in ANSWERED_IN_TEXT and not trace.final_text.strip())
        and cost == 0
        and all(budget_verdicts(row, trace).values())
        and run_checks(row, trace).passed
    )


__all__ = ["ended", "passes"]
