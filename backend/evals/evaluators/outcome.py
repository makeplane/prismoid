# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The routing verdict: did the turn land on an outcome the row accepts, and what a miss costs.

Both read the trace's outcome and the row's label and nothing else: `route` and `route_source` are
diagnostic, so a row passes through any door that delivers the right thing.
"""

from __future__ import annotations

from evals.evaluators.checks import verdict
from evals.metrics import RoutedTask
from evals.schema import Asks, Outcome, OutcomeRow, Surface, Unscored
from evals.trace import PrismoidTrace


def outcome_of(row: OutcomeRow, trace: PrismoidTrace) -> Outcome | Unscored | None:
    """The outcome a trial is scored on: a conversation reply that passes every `asks` check asked."""
    asks = [spec for spec in row.checks if isinstance(spec, Asks)]
    if (
        row.context.surface is Surface.CONVERSATION
        and trace.outcome is Outcome.REPLY
        and asks
        and all(verdict(spec, row, trace) is True for spec in asks)
    ):
        return Outcome.ASK
    return trace.outcome


def route_cost(row: OutcomeRow, trace: PrismoidTrace) -> float | None:
    """The misroute cost of this trial (0 when acceptable); None for a row with no routing outcome."""
    if not isinstance(row.expected_outcome, Outcome):
        return None
    return RoutedTask.of(row, [outcome_of(row, trace)]).cost()
