# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The documents suite: a deliverable is scored by its signed verdict and its judge, with time and $ per row.

A row of `datasets/documents.jsonl` is an ask a person makes (`category` is
memo, report, deck, sheet, long_doc or code) with its acceptance criteria as numbered lines in
`judge_rubric`; it passes when the turn delivered the right kind of artifact and its signed verdict is a
pass from a judged seat: another family, the same family or the maker itself, whichever keys the host has
(only `no judge` is unverified). The seat is reported per row. A judge FAIL always sinks the verdict, so a
pass with a seat is a pass the judge did not fail. Planted defects live in
`tests/integration/verification/test_planted_defects.py`. A row that breaks this shape raises `RowError` when it is read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from evals.schema import OutcomeRow, load_rows
from evals.suites import Receipts, RecordedTurn, Scorecard
from evals.trace import PrismoidTrace
from prismoid.schemas.events import ArtifactProduced
from prismoid.schemas.model_class import Family, JudgeFamily
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.verification import Verdict, VerificationReport

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "documents.jsonl"
CRITERION = re.compile(r"^\d+\. \S", re.MULTILINE)


class Kind(StrEnum):
    MEMO = "memo"
    REPORT = "report"
    DECK = "deck"
    SHEET = "sheet"
    LONG_DOC = "long_doc"  # about fifteen pages
    CODE = "code"


DELIVERS: dict[Kind, ArtifactType] = {
    Kind.MEMO: ArtifactType.DOCUMENT,
    Kind.REPORT: ArtifactType.DOCUMENT,
    Kind.DECK: ArtifactType.DECK,
    Kind.SHEET: ArtifactType.SPREADSHEET,
    Kind.LONG_DOC: ArtifactType.DOCUMENT,
    Kind.CODE: ArtifactType.CODE,
}


class RowError(ValueError):
    """A documents row is missing what the scorer needs; it names the row and the field."""


@dataclass(frozen=True)
class LiveCase:
    row: OutcomeRow
    kind: Kind
    criteria: tuple[str, ...]


@dataclass(frozen=True)
class Receipt:
    """What a deliverable's signed verification says: its verdict, and which seat judged it."""

    verdict: Verdict
    seat: JudgeFamily

    @classmethod
    def of_report(cls, report: VerificationReport) -> Receipt:
        return cls(verdict=report.verdict, seat=report.judged)

    @classmethod
    def of_served(cls, body: Mapping[str, Any]) -> Receipt:
        """From `GET /v1/artifacts/{id}/verification`, which names the judge model but not its own verdict."""
        judge, family = body.get("verifier_model"), body.get("verifier_family")
        seat = (
            JudgeFamily.between(
                maker=body["producer_model"],
                maker_family=Family(body["producer_family"]),
                judge=judge,
                judge_family=Family(family),
            )
            if judge and family
            else JudgeFamily.NONE
        )
        return cls(verdict=Verdict(body["verdict"]), seat=seat)


@dataclass(frozen=True)
class DocumentScore:
    """One row's result, with the turn's time and $."""

    row_id: str
    passed: bool
    verdict: Verdict
    seat: JudgeFamily | None  # who judged; None when no signed verification came
    reason: str
    cost_usd: Decimal
    latency_s: float | None


def case_of(row: OutcomeRow) -> LiveCase:
    """The case a row states; raises `RowError` naming the first field it lacks."""
    if row.category not in set(Kind):
        raise RowError(f"documents row {row.id}: category must be one of {[k.value for k in Kind]}")
    criteria = tuple(line for line in (row.judge_rubric or "").splitlines() if CRITERION.match(line))
    if len(criteria) < 2:
        raise RowError(
            f"documents row {row.id}: judge_rubric needs at least two numbered acceptance criteria"
        )
    return LiveCase(row=row, kind=Kind(row.category), criteria=criteria)


def load_cases(path: Path = DATASET) -> list[LiveCase]:
    return [case_of(row) for row in load_rows(path)]


def score_live(case: LiveCase, trace: PrismoidTrace, receipt: Receipt | None) -> DocumentScore:
    """A live row: the right artifact delivered, and a pass verdict from any judged seat."""
    if not trace.delivered or not any(a.artifact_type is DELIVERS[case.kind] for a in trace.artifacts):
        reason = f"no {DELIVERS[case.kind].value} delivered"
    elif receipt is None:
        reason = "no signed verification"
    elif receipt.seat is JudgeFamily.NONE:
        reason = "no judge ran: unverified"
    elif receipt.verdict is not Verdict.PASS:
        reason = f"verdict {receipt.verdict.value}"
    else:
        reason = ""
    return DocumentScore(
        row_id=case.row.id,
        passed=not reason,
        verdict=receipt.verdict if receipt else Verdict.UNVERIFIED,
        seat=receipt.seat if receipt else None,
        reason=reason,
        cost_usd=trace.cost_usd,
        latency_s=trace.latency_s,
    )


def table(scores: Sequence[DocumentScore]) -> str:
    """A markdown table, one line per row and a pass count: verdict, judge, $, seconds and why it failed."""
    lines = ["| row | pass | verdict | judged by | $ | s | why |", "|---|---|---|---|---|---|---|"]
    for s in scores:
        seconds = "-" if s.latency_s is None else f"{s.latency_s:.1f}"
        judge = "-" if s.seat is None else s.seat.value
        lines.append(
            f"| {s.row_id} | {'yes' if s.passed else 'no'} | {s.verdict.value} | {judge} | {s.cost_usd:.4f} | {seconds} | {s.reason} |"
        )
    lines.append(f"| total | {sum(s.passed for s in scores)}/{len(scores)} | | | | | |")
    return "\n".join(lines)


class Suite:
    """The live run: every row is submitted, each scored on its last deliverable's signed receipt."""

    def select(self, rows: Sequence[OutcomeRow]) -> list[OutcomeRow]:
        return [case_of(row).row for row in rows]

    async def score(self, turns: Sequence[RecordedTurn], receipts: Receipts) -> Scorecard:
        scores: list[DocumentScore] = []
        for turn in turns:
            case = case_of(turn.row)
            made = [e for e in turn.events if isinstance(e, ArtifactProduced) and e.role == "deliverable"]
            body = await receipts(str(made[-1].artifact_id)) if made else None
            receipt = Receipt.of_served(body) if body else None
            scores.append(score_live(case, PrismoidTrace.from_events(turn.events), receipt))
        return Scorecard(table=table(scores), passed=tuple(s.passed for s in scores))


SCORED = Suite()
