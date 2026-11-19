# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The four evaluators, and IPT.

The evaluators run over a judge-gated suite, so a unit test is where their logic is proven: that they
are **independent axes** which can disagree (a fabrication that is cheap and hands-off is Verified-false
but WithinBudget and NoInterventions), that a judge of the producer's own family does not count as a
cross-family judge, and that IPT excludes the designed gates it is defined to exclude.
"""

from __future__ import annotations

from decimal import Decimal

from evals.evaluators.scores import (
    DESIGNED_GATE_TYPES,
    INTERVENTION_TYPES,
    ipt,
    no_interventions,
    rubric_judged,
    score,
    verified,
    within_budget,
)


def test_verified_is_pass_and_only_pass() -> None:
    assert verified("pass")
    assert not verified("unverified")
    assert not verified("fail")
    assert not verified("error")


def test_rubric_judge_requires_a_different_family() -> None:
    assert rubric_judged("qwen", "anthropic")  # a cross-family judge ran
    assert not rubric_judged("anthropic", "anthropic")  # self-grading is not a judgement
    assert not rubric_judged("", "anthropic")  # no judge ran at all


def test_within_budget_treats_no_budget_as_uncapped() -> None:
    assert within_budget(Decimal("0.50"), Decimal("1.00"))
    assert within_budget(Decimal("1.00"), Decimal("1.00"))  # exactly at the cap is within
    assert not within_budget(Decimal("1.01"), Decimal("1.00"))
    assert within_budget(Decimal("999"), None)  # an uncapped task is trivially within


def test_no_interventions_excludes_designed_gates() -> None:
    # The designed question flow is not an intervention - asking a good question is autonomy working.
    assert no_interventions(["task.created", "gate.opened", "task.clarifying", "task.delivered"])
    # An escalation is the system reaching for a human it did not plan to need.
    assert not no_interventions(["task.created", "escalation.raised", "task.delivered"])
    assert INTERVENTION_TYPES.isdisjoint(DESIGNED_GATE_TYPES), "a gate cannot be both designed and unplanned"


def test_the_axes_can_disagree() -> None:
    # A cheap, hands-off fabrication the verifier caught: three green, one red - the case for four axes.
    s = score(
        verdict="fail",
        verifier_family="qwen",
        producer_family="anthropic",
        cost_usd=Decimal("0.02"),
        budget_usd=Decimal("1.00"),
        event_types=["task.created", "task.delivered"],
    )
    assert not s.verified
    assert s.rubric_judge and s.within_budget and s.no_interventions
    assert not s.all_pass


def test_ipt_is_touches_over_tasks() -> None:
    assert ipt(38, 239) == 38 / 239
    assert ipt(38, 239) < 0.5
    assert ipt(0, 0) == 0.0  # no tasks ⇒ nothing to intervene in, not a divide-by-zero
