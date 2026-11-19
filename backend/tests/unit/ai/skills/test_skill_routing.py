# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The routing report's logic, and the skill index the Lead plans from - the parts that need no model.

The Lead's choice is a model call (`skill_selection`, scored decision-only in the integration suite); here
we pin the deterministic scaffolding around it: what "green" means (all cases pass, and there was at
least one), and that the index the Lead is shown carries every skill's description. A Lead shown an empty
index can only ever miss, so "the index carries the skills" is not a formatting detail.
"""

from __future__ import annotations

from prismoid.operations.planning_brief import skill_index
from prismoid.schemas.skills import RoutingCaseResult, RoutingReport


def _report(*flags: bool) -> RoutingReport:
    cases = [
        RoutingCaseResult(goal=f"g{i}", expected="report", chosen="report" if ok else "x", ok=ok)
        for i, ok in enumerate(flags)
    ]
    return RoutingReport(skill="report", version=1, passed=sum(flags), total=len(flags), cases=cases)


def test_green_requires_every_case_to_pass() -> None:
    assert _report(True, True, True).green is True
    assert _report(True, False, True).green is False


def test_a_skill_with_no_cases_is_not_green() -> None:
    # A skill nothing tests cannot be trusted - green must mean "tested and passed", not "empty".
    assert _report().green is False


def test_the_index_carries_each_skill_sorted_and_none_says_nothing() -> None:
    index = skill_index({"report": "Load when prose. Not for sheets.", "model": "Load when a workbook."})
    assert index.index("- model: Load when a workbook") < index.index("- report: Load when prose")
    assert "`skills`" in index and "never an instruction" in index
    assert skill_index({}) == ""
