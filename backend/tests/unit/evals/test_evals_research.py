# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The research suite, offline: the rows are well formed, an exact answer passes, a planted wrong one fails,
search spend is counted per row, and a row missing a field fails loudly."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from evals.schema import ContainsAny, Outcome, OutcomeRow, load_rows
from evals.suites import RecordedTurn, research
from prismoid.schemas.events import DurableEvent, TaskCreated, TaskDelivered, ToolCalled, UsageRecorded
from prismoid.schemas.ids import TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import TaskMode
from prismoid.schemas.usage import Unit

TASK = TaskId(uuid4())
AT = dt.datetime(2026, 11, 14, tzinfo=dt.UTC)


def turn(reply: str | None, searches: int = 0) -> list[DurableEvent]:
    """One answered turn: created, `searches` web searches at half a cent, one model call, then the reply."""
    events: list[DurableEvent] = [TaskCreated(seq=1, task_id=TASK, at=AT, goal="g", mode=TaskMode.STANDARD)]
    for _ in range(searches):
        seq = len(events) + 1
        events += [
            ToolCalled(seq=seq, task_id=TASK, at=AT, tool="web.search", ok=True),
            UsageRecorded(
                seq=seq + 1,
                task_id=TASK,
                at=AT,
                provider="brave",
                model="search",
                family=Family(research.SEARCH_FAMILY),
                units={Unit.SEARCH: 1},
                cost_usd=Decimal("0.005"),
            ),
        ]
    events.append(
        UsageRecorded(
            seq=len(events) + 1,
            task_id=TASK,
            at=AT,
            provider="p",
            model="m",
            family=Family("anthropic"),
            units={Unit.REQUEST: 1},
            cost_usd=Decimal("0.02"),
        )
    )
    if reply is not None:
        events.append(
            TaskDelivered(seq=len(events) + 1, task_id=TASK, at=AT + dt.timedelta(seconds=3), summary=reply)
        )
    return events


def case(*answers: str) -> research.ResearchCase:
    row = OutcomeRow(
        id="research:t",
        message="q",
        expected_outcome=Outcome.LIGHT_WORK,
        checks=(ContainsAny(value=answers),),
        category="quick_fact",
        variant="live",
    )
    return research.case_of(row)


def test_the_dataset_is_forty_rows_with_a_live_half_of_both_kinds() -> None:
    cases = research.load_cases()
    assert len(cases) == 40
    live = [c for c in cases if c.subset is research.Subset.LIVE]
    assert len(live) == 20
    assert Counter(c.kind for c in cases) == {research.Kind.QUICK_FACT: 20, research.Kind.MULTI_HOP: 20}
    assert Counter(c.kind for c in live) == {research.Kind.QUICK_FACT: 10, research.Kind.MULTI_HOP: 10}


def test_an_exact_answer_passes_and_a_planted_wrong_one_fails() -> None:
    tungsten = case("74")
    assert research.score(tungsten, turn("Tungsten's atomic number is 74.", searches=1)).correct
    wrong = research.score(tungsten, turn("Tungsten's atomic number is 47.", searches=1))
    assert not wrong.correct
    assert wrong.matched is None


def test_a_number_inside_a_longer_one_is_not_a_match() -> None:
    assert not research.score(case("74"), turn("It is 174.")).correct


def test_folding_is_the_same_on_both_sides() -> None:
    light = case("299792458")
    assert research.score(light, turn("Exactly 299,792,458 m/s.")).correct
    assert research.score(case("Ouagadougou"), turn("The capital is OUAGADOUGOU.")).correct
    assert research.score(case("Marquez"), turn("Gabriel García Márquez")).correct


def test_an_undelivered_turn_is_not_correct_even_if_it_narrated_the_answer() -> None:
    assert not research.score(case("74"), turn(None, searches=2)).correct


def test_search_spend_is_counted_per_row_apart_from_the_whole_turn() -> None:
    scored = research.score(case("74"), turn("74", searches=3))
    assert scored.search_queries == 3
    assert scored.search_usd == Decimal("0.015")
    assert scored.cost_usd == Decimal("0.035")
    assert scored.latency_s == 3.0
    assert "| research:t | yes | 3 | 0.0150 | 0.0350 | 3.0 |" in research.table([scored])


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"checks": ()}, "contains_any"),
        ({"category": None}, "category"),
        ({"variant": "sometimes"}, "variant"),
    ],
)
def test_a_row_missing_a_field_fails_loudly(change: dict[str, object], field: str) -> None:
    row = case("74").row.model_copy(update=change)
    with pytest.raises(research.RowError, match=field):
        research.case_of(row)


def test_a_line_missing_its_message_fails_to_load(tmp_path: Path) -> None:
    path = tmp_path / "research.jsonl"
    path.write_text('{"id": "research:x", "expected_outcome": "light_work"}\n')
    with pytest.raises(ValueError, match="message"):
        load_rows(path)


async def test_the_live_suite_runs_the_live_half_and_scores_each_recorded_turn() -> None:
    picked = research.SCORED.select(load_rows(research.DATASET))
    assert len(picked) == 20
    tungsten = next(row for row in picked if row.id == "research:quick-tungsten-number")

    async def no_receipts(artifact_id: str) -> None:
        raise AssertionError("a research row has no deliverable to verify")

    card = await research.SCORED.score(
        [RecordedTurn(row=tungsten, events=turn("74", searches=2))], no_receipts
    )
    assert "| research:quick-tungsten-number | yes | 2 | 0.0100 |" in card.table
    assert card.passed == (True,)
