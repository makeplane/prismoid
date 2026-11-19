# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The limits a row sets on what the turn may do and cost: calls, tools, Machines, artifacts, money.

Shape is part of correctness: an answer that is right but took three calls and a Machine failed the
user. A limit a row leaves unset is not checked, and its verdict is absent rather than true.

Two kinds of limit: caps (`max_*`, `usd_max`) hold whatever the turn delivered; the shape limits
(`expected_machines`, `min_tool_calls`, `expected_tools`) describe the row's primary outcome, so they
apply only when the turn delivered it. An allowed `ask` on a computer row never fails for opening
no Machine, and a turn parked at a gate is not held to the tools its work has yet to call.
"""

from __future__ import annotations

from decimal import Decimal

from evals.evaluators.outcome import outcome_of
from evals.schema import OptionalMachine, OutcomeRow
from evals.trace import PrismoidTrace


def _within(count: int, *, least: int | None = None, most: int | None = None) -> bool:
    return (least is None or count >= least) and (most is None or count <= most)


def _machine_bounds(row: OutcomeRow, *, shaped: bool) -> tuple[int, int | None]:
    """The fewest and most Machines the row accepts: `expected_machines` pinned when `shaped`,
    `max_machines` capped always."""
    expected = row.expected_machines if shaped else None
    least, most = (expected, expected) if isinstance(expected, int) else (0, None)
    if expected is OptionalMachine.ZERO_OR_ONE:
        most = 1
    if row.max_machines is not None:
        most = row.max_machines if most is None else min(most, row.max_machines)
    return least, most


def budget_verdicts(row: OutcomeRow, trace: PrismoidTrace) -> dict[str, bool]:
    """One verdict per limit that applies to this trial, true when the turn kept inside it."""
    shaped = outcome_of(row, trace) == row.expected_outcome
    working = shaped and (trace.delivered or not trace.gates)  # a parked turn's work has not run yet
    least_tools = row.min_tool_calls if working else None
    verdicts: dict[str, bool] = {}
    if row.max_calls_before_delivery is not None:
        verdicts["calls_before_delivery"] = trace.calls_before_delivery <= row.max_calls_before_delivery
    if row.max_tool_calls is not None or least_tools is not None:
        verdicts["tool_calls"] = _within(len(trace.tool_calls), least=least_tools, most=row.max_tool_calls)
    if working and row.expected_tools:
        verdicts["expected_tools"] = all(
            any(call.is_named(name) for call in trace.tool_calls) for name in row.expected_tools
        )
    if (shaped and row.expected_machines is not None) or row.max_machines is not None:
        least, most = _machine_bounds(row, shaped=shaped)
        verdicts["machines"] = _within(trace.machines, least=least, most=most)
    if row.max_artifacts is not None:
        verdicts["artifacts"] = len(trace.artifacts) <= row.max_artifacts
    if row.budget is not None and row.budget.usd_max is not None:
        verdicts["usd"] = trace.cost_usd <= Decimal(str(row.budget.usd_max))
    return verdicts
