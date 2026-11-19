# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's parity book, offline: every problem-book row's facts allow the ending its label expects, the
decision runner reaches it with the plan unrefused, and every rule the rows lean on names one block of the
Lead's prompt. A planted forbidden ending or a broken plan turns the judge red."""

from __future__ import annotations

import pytest

from evals.cases import lead_parity
from evals.cases.lead_parity import JUDGED, RULES, Fate, ParityRow
from evals.runners.decide import Seat
from prismoid.ai.agents.base import get
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.model_class import ModelClass
from tests.support.first_request import fake_row

SEAT = Seat([fake_row(ModelClass.FRONTIER_EXECUTOR)])
ROWS = lead_parity.population()
LEAD_ROWS = [r for r in ROWS if r.fate is not Fate.NOT_LEAD]
CONSISTENT = [r for r in ROWS if r.fate is Fate.CONSISTENT and r.expected in JUDGED]


def _by_id(row_id: str) -> ParityRow:
    return next(r for r in ROWS if r.row.id == row_id)


def test_every_book_row_is_classified_once_and_every_lead_row_knows_its_ending() -> None:
    ids = [r.row.id for r in ROWS]
    assert len(ids) == len(set(ids))
    assert {r.row.id.split(":")[0] for r in ROWS} >= {
        "range",
        "computer",
        "computer_routing",
        "navigate",
        "machine",
        "planner",
        "chat",
    }
    for row in ROWS:
        assert (row.fate is Fate.CONSISTENT) == (not row.why), row.row.id
    for row in LEAD_ROWS:
        assert row.expected in row.endings, row.row.id
        assert (row.expected is LeadEndingKind.PLAN) == bool(row.plan), row.row.id


def test_every_lead_rows_facts_allow_the_ending_its_label_expects() -> None:
    forbidden = [r.row.id for r in LEAD_ROWS if not lead_parity.allowed(r)]
    assert not forbidden, forbidden


async def test_the_decision_runner_reaches_every_consistent_rows_ending_with_its_plan_unrefused() -> None:
    missed = {r.row.id: why for r in CONSISTENT if (why := await lead_parity.judge(r, SEAT))}
    assert not missed, missed
    assert len(CONSISTENT) > 300


@pytest.mark.parametrize(
    ("row_id", "planted"),
    [
        ("range:dial-clarify-always-blocks", LeadEndingKind.REPLY),  # the dial lets the first run only ask
        ("planner:sourced-sheet-dialed-search", LeadEndingKind.REPLY),  # a dialed deliverable needs a plan
    ],
)
async def test_a_planted_ending_the_facts_forbid_turns_the_judge_red(
    row_id: str, planted: LeadEndingKind
) -> None:
    found = _by_id(row_id)
    facts = lead_parity.facts_of(found.row)  # a dial row its recipe answers still has the facts a Lead would
    row = ParityRow(found.row, Fate.CONSISTENT, facts=facts, expected=LeadEndingKind.PLAN, plan=("writer",))
    assert planted not in facts.allowed_endings()
    assert (await lead_parity.judge(row, SEAT, planted)).startswith("refused")


async def test_a_plan_that_breaks_the_rules_lands_on_the_floor_and_turns_the_judge_red() -> None:
    row = _by_id("range:deliverable-plain-sheet")
    broken = ParityRow(
        row.row, row.fate, facts=row.facts, endings=row.endings, expected=row.expected, plan=("nobody",)
    )
    assert (await lead_parity.judge(broken, SEAT)).startswith("plan replaced by the floor")


def test_every_rule_names_one_block_of_the_leads_prompt() -> None:
    ids = [block.id for block in get("lead").blocks]
    for key, rule in RULES.items():
        assert ids.count(rule.block) == 1, f"{key}: {rule.block} is not one block of the Lead's prompt"


def test_every_rules_rows_are_lead_rows_whose_ending_the_rule_can_give() -> None:
    lead_ids = {r.row.id for r in LEAD_ROWS}
    for key, rule in RULES.items():
        assert set(rule.rows) <= lead_ids, f"{key}: {sorted(set(rule.rows) - lead_ids)}"
    assert sum(bool(rule.rows) for rule in RULES.values()) >= 30


def test_every_row_routes_where_its_classification_says() -> None:
    astray = [r.row.id for r in ROWS if not lead_parity.routed_as_classified(r)]
    assert not astray, astray
    assert all(lead_parity.route(r.row) is None for r in LEAD_ROWS), "every Lead row reaches the Lead"


@pytest.mark.parametrize(
    "row_id",
    ["range:deliverable-plain-fleet", "range:dial-direct-blocks", "range:closedqa-plain-author"],
)
def test_a_row_classified_against_its_route_turns_the_check_red(row_id: str) -> None:
    row = _by_id(row_id)
    flipped = (
        ParityRow(row.row, Fate.NOT_LEAD, "the fleet dial's turn 1 is the fleet recipe")
        if row.fate is not Fate.NOT_LEAD
        else ParityRow(row.row, Fate.CONSISTENT, facts=lead_parity.facts_of(row.row))
    )
    assert lead_parity.routed_as_classified(row) and not lead_parity.routed_as_classified(flipped)
