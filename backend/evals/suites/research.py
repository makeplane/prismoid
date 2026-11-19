# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The research suite: stable facts asked as a person would, scored by exact match, with search spend per row.

A row of `datasets/research.jsonl` is an `OutcomeRow` whose one `contains_any` check lists the accepted
forms of its answer, `category` says `quick_fact` or `multi_hop`, and `variant` says whether it is in
the `live` subset (run on every live pass) or only in the `full` set. Every answer is a fact that does not
drift with time, so a row never needs a date.

Exact match: the reply and each accepted form are folded the same way (accents dropped, case folded,
digit-group commas removed, punctuation to spaces) and a form must appear as a whole phrase. A row that
breaks this shape raises `RowError` when it is read, never scores quietly.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from evals.schema import ContainsAny, OutcomeRow, load_rows
from evals.suites import Receipts, RecordedTurn, Scorecard
from evals.trace import PrismoidTrace
from prismoid.schemas.events import DurableEvent, UsageRecorded
from prismoid.schemas.usage import Unit

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "research.jsonl"
SEARCH_FAMILY = "web"  # the usage family every search engine bills under


class Kind(StrEnum):
    QUICK_FACT = "quick_fact"  # one lookup
    MULTI_HOP = "multi_hop"  # the answer needs one fact to find the next


class Subset(StrEnum):
    LIVE = "live"  # run on every live pass
    FULL = "full"  # run only when the whole set runs


class RowError(ValueError):
    """A research row is missing what the scorer needs; it names the row and the field."""


@dataclass(frozen=True)
class ResearchCase:
    row: OutcomeRow
    answers: tuple[str, ...]
    kind: Kind
    subset: Subset


@dataclass(frozen=True)
class ResearchScore:
    """One row's result: was the answer exact, and what the turn and its searches cost."""

    row_id: str
    correct: bool
    matched: str | None
    search_queries: int
    search_usd: Decimal
    cost_usd: Decimal
    latency_s: float | None


def case_of(row: OutcomeRow) -> ResearchCase:
    """The case a row states; raises `RowError` naming the first field it lacks."""
    answers = [c for c in row.checks if isinstance(c, ContainsAny)]
    if len(answers) != 1 or not answers[0].value:
        raise RowError(f"research row {row.id}: needs exactly one non-empty contains_any check (the answers)")
    if row.category not in set(Kind):
        raise RowError(f"research row {row.id}: category must be one of {[k.value for k in Kind]}")
    if row.variant not in set(Subset):
        raise RowError(f"research row {row.id}: variant must be one of {[s.value for s in Subset]}")
    return ResearchCase(
        row=row, answers=answers[0].value, kind=Kind(row.category), subset=Subset(row.variant)
    )


def load_cases(path: Path = DATASET) -> list[ResearchCase]:
    return [case_of(row) for row in load_rows(path)]


def normalized(text: str) -> str:
    """Accents dropped, case folded, `299,792,458` read as `299792458`, punctuation turned to spaces."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    plain = re.sub(r"(?<=\d),(?=\d{3}\b)", "", plain.casefold())
    return " ".join(re.findall(r"\w+", plain))


def matched_answer(answers: Iterable[str], reply: str) -> str | None:
    """The first accepted form the reply holds as a whole phrase."""
    padded = f" {normalized(reply)} "
    return next((a for a in answers if f" {normalized(a)} " in padded), None)


def score(case: ResearchCase, events: Sequence[DurableEvent]) -> ResearchScore:
    """Score one turn from its durable record: the delivered reply against the answers, and the searches it paid for."""
    trace = PrismoidTrace.from_events(events)
    searches = [e for e in events if isinstance(e, UsageRecorded) and e.family == SEARCH_FAMILY]
    found = matched_answer(case.answers, trace.final_text) if trace.delivered else None
    return ResearchScore(
        row_id=case.row.id,
        correct=found is not None,
        matched=found,
        search_queries=sum(e.units.get(Unit.SEARCH, 0) for e in searches),
        search_usd=sum((e.cost_usd for e in searches), Decimal(0)),
        cost_usd=trace.cost_usd,
        latency_s=trace.latency_s,
    )


def table(scores: Sequence[ResearchScore]) -> str:
    """A markdown table, one line per row and a total line: exact or not, searches, search $, total $, seconds."""
    lines = ["| row | exact | searches | search $ | total $ | s |", "|---|---|---|---|---|---|"]
    for s in scores:
        seconds = "-" if s.latency_s is None else f"{s.latency_s:.1f}"
        lines.append(
            f"| {s.row_id} | {'yes' if s.correct else 'no'} | {s.search_queries} | {s.search_usd:.4f} "
            f"| {s.cost_usd:.4f} | {seconds} |"
        )
    right = sum(s.correct for s in scores)
    search = sum((s.search_usd for s in scores), Decimal(0))
    total = sum((s.cost_usd for s in scores), Decimal(0))
    lines.append(
        f"| total | {right}/{len(scores)} | {sum(s.search_queries for s in scores)} | {search:.4f} | {total:.4f} | |"
    )
    return "\n".join(lines)


class Suite:
    """The live run: only the `live` subset is submitted, and each turn is scored from its record."""

    def select(self, rows: Sequence[OutcomeRow]) -> list[OutcomeRow]:
        return [row for row in rows if case_of(row).subset is Subset.LIVE]

    async def score(self, turns: Sequence[RecordedTurn], receipts: Receipts) -> Scorecard:
        scores = [score(case_of(turn.row), turn.events) for turn in turns]
        return Scorecard(table=table(scores), passed=tuple(s.correct for s in scores))


SCORED = Suite()
