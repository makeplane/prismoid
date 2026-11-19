# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The four evaluators - one delivered task, scored four independent ways.

A single pass/fail hides the disagreement that matters. A fabricated board memo can be **Verified**-false
(the suite caught the invented figure) yet **WithinBudget** (it was cheap to fabricate) and needed
**NoInterventions** (nobody had to step in) - three greens and one red, and collapsing them to one tick
loses the one that counts. So each delivered task is scored on four axes that can disagree:

* **Verified** - the signed verdict is `pass`. The floor: does the delivered artifact survive the
  deterministic suite and the judge? Nothing else the case asked for counts if this is false.
* **RubricJudge** - a judge of a **different family** from the producer assessed it (no model grades
  its own family's homework). This scores the *structural* property - that a cross-family judge ran and
  did not fail it - separately from Verified, so "the judge never ran" cannot masquerade as "the judge
  passed it".
* **WithinBudget** - the task's spend did not exceed its budget. A correct answer bought at 10× the
  ceiling is a different failure from a wrong one, and the ledger is where it shows.
* **NoInterventions** - the task reached delivery with **zero unplanned human touches**. Designed gates
  (a planned clarify round, an `ask_user` step) are *excluded*: choosing to ask a sharp question is
  autonomy working, not failing. What counts is the unplanned touch - an escalation, a human forced to
  cancel or redirect a task that was going wrong. This is the axis IPT aggregates across the suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Unplanned human touches ("Interventions Per Task - unplanned human touches; designed
# gates excluded"). `gate.opened` / `gate.resolved` / `task.clarifying` are the *designed* question flow
# and are deliberately NOT here - asking a good question is the system working. An escalation is the
# system reaching for a human it did not plan to need.
INTERVENTION_TYPES: frozenset[str] = frozenset({"escalation.raised"})
DESIGNED_GATE_TYPES: frozenset[str] = frozenset({"gate.opened", "gate.resolved", "task.clarifying"})


@dataclass(frozen=True)
class Scores:
    verified: bool
    rubric_judge: bool
    within_budget: bool
    no_interventions: bool

    @property
    def all_pass(self) -> bool:
        return self.verified and self.rubric_judge and self.within_budget and self.no_interventions


def verified(verdict: str) -> bool:
    """The signed verdict is a pass. `unverified`, `fail`, and every error string are not."""
    return verdict == "pass"


def rubric_judged(verifier_family: str, producer_family: str) -> bool:
    """A judge ran, and it was **not** the producer's family (no self-grading)."""
    return bool(verifier_family) and verifier_family != producer_family


def within_budget(cost_usd: Decimal, budget_usd: Decimal | None) -> bool:
    """Spend did not exceed budget. No budget set ⇒ trivially within (the task was uncapped)."""
    return budget_usd is None or cost_usd <= budget_usd


def no_interventions(event_types: list[str] | frozenset[str]) -> bool:
    """No unplanned human touch. A designed gate is not an intervention (see INTERVENTION_TYPES)."""
    return not any(t in INTERVENTION_TYPES for t in event_types)


def score(
    *,
    verdict: str,
    verifier_family: str,
    producer_family: str,
    cost_usd: Decimal,
    budget_usd: Decimal | None,
    event_types: list[str] | frozenset[str],
) -> Scores:
    return Scores(
        verified=verified(verdict),
        rubric_judge=rubric_judged(verifier_family, producer_family),
        within_budget=within_budget(cost_usd, budget_usd),
        no_interventions=no_interventions(event_types),
    )


def ipt(unplanned_touches: int, tasks: int) -> float:
    """Interventions Per Task - the suite-level aggregate of NoInterventions. Below 0.5 is the M7 gate:
    fewer than one unplanned human touch for every two tasks. Zero tasks ⇒ 0.0 (nothing to intervene in)."""
    return unplanned_touches / tasks if tasks else 0.0
