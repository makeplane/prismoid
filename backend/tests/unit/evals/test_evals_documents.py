# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The documents suite, offline: the rows are well formed, a verdict scores as it should with its judged seat,
time and $ beside it, a planted failing verdict fails, and a row missing a field fails loudly."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal
from uuid import uuid4

import pytest

from evals.schema import Outcome, OutcomeRow, load_rows
from evals.suites import RecordedTurn, documents
from evals.trace import ArtifactRecord, PrismoidTrace
from prismoid.schemas.events import ArtifactProduced, DurableEvent, TaskCreated, TaskDelivered
from prismoid.schemas.ids import ArtifactId, TaskId
from prismoid.schemas.model_class import Family, JudgeFamily
from prismoid.schemas.outputs import ArtifactType, VerifierSuite
from prismoid.schemas.plan import TaskMode
from prismoid.schemas.verification import JudgeOpinion, Verdict, VerificationReport

LIVE = documents.LiveCase(
    row=OutcomeRow(
        id="documents:memo-t",
        message="m",
        expected_outcome=Outcome.PLAN,
        judge_rubric="1. A memo.\n2. Short.",
        category="memo",
    ),
    kind=documents.Kind.MEMO,
    criteria=("1. A memo.", "2. Short."),
)
DELIVERED = PrismoidTrace(
    outcome=Outcome.PLAN,
    delivered=True,
    artifacts=(ArtifactRecord(artifact_type=ArtifactType.DOCUMENT, filename="memo.docx"),),
    cost_usd=Decimal("0.42"),
    latency_s=95.0,
)


def served(verdict: str, judge: str | None) -> dict[str, object]:
    """The verification body the API serves; `judge` is a `provider:model` ref or None."""
    return {
        "verdict": verdict,
        "producer_model": "anthropic:p",
        "producer_family": "anthropic",
        "verifier_model": judge,
        "verifier_family": judge.partition(":")[0] if judge else None,
    }


def receipt(verdict: str, judge: str | None) -> documents.Receipt:
    return documents.Receipt.of_served(served(verdict, judge))


def test_the_dataset_is_twelve_asks_two_of_each_kind() -> None:
    cases = documents.load_cases()
    assert len(cases) == 12
    assert Counter(c.kind for c in cases) == dict.fromkeys(documents.Kind, 2)
    assert all(len(c.criteria) >= 4 for c in cases)


@pytest.mark.parametrize(
    ("judge", "seat"),
    [("openai:j", JudgeFamily.OTHER), ("anthropic:j", JudgeFamily.SAME), ("anthropic:p", JudgeFamily.SELF)],
)
def test_a_pass_from_any_judged_seat_passes_with_its_seat_time_and_cost(
    judge: str, seat: JudgeFamily
) -> None:
    scored = documents.score_live(LIVE, DELIVERED, receipt("pass", judge))
    assert scored.passed
    assert scored.seat is seat
    assert (scored.cost_usd, scored.latency_s) == (Decimal("0.42"), 95.0)
    assert f"| documents:memo-t | yes | pass | {seat.value} | 0.4200 | 95.0 |" in documents.table([scored])


def test_a_signed_report_reads_the_same_as_the_served_body() -> None:
    signed = VerificationReport(
        artifact_sha256="0" * 64,
        suite=VerifierSuite.DOCUMENT,
        judge=JudgeOpinion(
            verdict=Verdict.PASS, rationale="r", model="anthropic:j", family=Family("anthropic")
        ),
        verdict=Verdict.PASS,
        producer_model="anthropic:p",
        producer_family=Family("anthropic"),
        at=dt.datetime(2026, 11, 14, tzinfo=dt.UTC),
    )
    assert documents.Receipt.of_report(signed) == receipt("pass", "anthropic:j")


@pytest.mark.parametrize(
    ("trace", "signed", "why"),
    [
        (DELIVERED, receipt("fail", "openai:j"), "verdict fail"),
        (DELIVERED, receipt("pass", None), "no judge ran"),
        (DELIVERED, None, "no signed verification"),
        (DELIVERED.model_copy(update={"artifacts": ()}), receipt("pass", "openai:j"), "no document"),
    ],
)
def test_anything_short_of_a_judged_pass_fails(
    trace: PrismoidTrace, signed: documents.Receipt | None, why: str
) -> None:
    scored = documents.score_live(LIVE, trace, signed)
    assert not scored.passed
    assert why in scored.reason


async def test_the_live_suite_runs_every_row_and_scores_the_last_deliverable() -> None:
    rows = load_rows(documents.DATASET)
    picked = documents.SCORED.select(rows)
    assert len(picked) == 12
    task, artifact = TaskId(uuid4()), ArtifactId(uuid4())
    at = dt.datetime(2026, 11, 14, tzinfo=dt.UTC)
    events: list[DurableEvent] = [
        TaskCreated(seq=1, task_id=task, at=at, goal="g", mode=TaskMode.STANDARD),
        ArtifactProduced(
            seq=2,
            task_id=task,
            at=at,
            artifact_id=artifact,
            artifact_type=ArtifactType.DOCUMENT,
            title="t",
            filename="m.docx",
            sha256="0" * 64,
            bytes=1,
        ),
        TaskDelivered(seq=3, task_id=task, at=at + dt.timedelta(seconds=60), summary="done"),
    ]
    asked: list[str] = []

    async def receipts(artifact_id: str) -> dict[str, object] | None:
        asked.append(artifact_id)
        return served("pass", "openai:j")

    memo = next(row for row in picked if row.category == "memo")
    card = await documents.SCORED.score([RecordedTurn(row=memo, events=events)], receipts)
    assert asked == [str(artifact)]
    assert f"| {memo.id} | yes | pass | other family |" in card.table
    assert card.passed == (True,)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"judge_rubric": None}, "judge_rubric"),
        ({"judge_rubric": "1. Only one criterion."}, "judge_rubric"),
        ({"category": "prismoid"}, "category"),
    ],
)
def test_a_row_missing_a_field_fails_loudly(change: dict[str, object], field: str) -> None:
    with pytest.raises(documents.RowError, match=field):
        documents.case_of(LIVE.row.model_copy(update=change))
