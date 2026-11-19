# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The pure half of steering: what a directive does to a contract, and when a verb is legal.

The park/resume wiring and the plan_edits row are validated live; this is the
deterministic core - `merge_directives` - which is a pure function and therefore the cheapest thing
to be sure of.
"""

from __future__ import annotations

from prismoid.schemas.steering import merge_directives


def test_a_directive_becomes_an_acceptance_criterion_not_a_goal_rewrite() -> None:
    """ "use euros" is a thing the output must satisfy - a constraint the verifier checks - not a
    change to what the step is *for*. The goal is the planner's; the how is where steering lives."""
    goal, acceptance = merge_directives("write a memo", ["it answers the question"], ["use euros"])

    assert goal == "write a memo", "steering must not rewrite the goal"
    assert "it answers the question" in acceptance, "the original criteria survive"
    assert any("euros" in a for a in acceptance), "the directive is now a checkable criterion"


def test_multiple_directives_all_apply() -> None:
    """Directives accumulate - `plan_edits` is append-only and every one applies to a step that has
    not finished, so a second thought does not erase the first."""
    _, acceptance = merge_directives("g", ["a"], ["use euros", "keep it under 200 words"])
    assert sum("steered" in a for a in acceptance) == 2


def test_no_directives_is_the_contract_untouched() -> None:
    """The common case - most tasks are never steered - must cost nothing and change nothing."""
    goal, acceptance = merge_directives("g", ["a", "b"], [])
    assert goal == "g" and acceptance == ["a", "b"]
