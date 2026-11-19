# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one place a run decides whether a call needs a person's yes (rules in `schemas/approvals.py`).

`ApprovalRules` evaluates a rule table in Claude Code's order: any matching deny wins, then any ask,
then allow; a call no rule matches runs. `Approvals` binds the table to one run (can its seat ask,
is anyone there to answer, has it read outside text, did a teammate steer it) and is the `approve`
callable `ai/agents/mounts.py` hands the connector and browser doors: it returns None to run, a
refusal the model reads, or raises pydantic-ai's `ApprovalRequired` with the door's card and the
rule that asked (`rule`, `why`: the card says why it asks), which parks the step on the approve gate. An ask on a seat that cannot park is refused, never parked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.exceptions import ApprovalRequired

from prismoid.schemas.approvals import RULES, Act, Call, Decision, Rule


@dataclass(frozen=True)
class ApprovalRules:
    """A validated rule table, decided deny over ask over allow."""

    rules: tuple[Rule, ...] = RULES

    def __post_init__(self) -> None:
        ids = [rule.id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError(f"approval rule ids repeat: {sorted(i for i in ids if ids.count(i) > 1)}")

    def decide(self, call: Call) -> Rule | None:
        """The rule that decides `call`: the strongest decision among the matches, None when none match."""
        matched = [rule for rule in self.rules if rule.when.matches(call)]
        for decision in Decision:
            if found := next((rule for rule in matched if rule.decision is decision), None):
                return found
        return None


@dataclass(frozen=True)
class Approvals:
    """The rules bound to one run: what its doors call before a call leaves the run; `tainted` is the
    run's read of whether it has seen outside text, passed in so a missing one never fails open."""

    tainted: Callable[[], bool]
    can_ask: bool = True
    attended: bool = True
    borrowed: Callable[[], bool] = lambda: False
    rules: ApprovalRules = field(default_factory=ApprovalRules)

    def __call__(self, act: Act, approved: bool, ask: Mapping[str, Any]) -> str | None:
        """None to run `act`; a refusal the model reads; or raise `ApprovalRequired` carrying `ask`."""
        call = Call(
            **act.model_dump(), tainted=self.tainted(), borrowed=self.borrowed(), attended=self.attended
        )
        rule = self.rules.decide(call)
        if rule is None or rule.decision is Decision.ALLOW:
            return None
        if rule.decision is Decision.DENY:
            return f"refused: {rule.why}"
        if approved:
            return None
        if not self.can_ask:
            return f"this call now asks its owner first ({rule.why}), and this step cannot ask - finish without it"
        raise ApprovalRequired(metadata={**ask, "rule": rule.id, "why": rule.why})
