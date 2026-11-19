# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Claims grounding, offline: planted defects in a sourced memo are caught, the clean memo passes.

Each case is the same memo with one defect planted (a wrong number, an unsupported claim, a wrong
citation, a miscomputed figure), its claims as the claim reader would list them, checked against the
stored source text. The receipt path runs the real `claim_extract` agent on a scripted model, with
the kept pages labelled by their source ids. A reader's slip (a quote not in the source, a source cut
for the reader, a claim not in the document) never fails the author. No database, no live model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.verification import grounding
from prismoid.db.models.artifact import Artifact
from prismoid.operations import grounding as grounding_ops
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import sources as sources_ops
from prismoid.operations import usage as usage_ops
from prismoid.schemas.claims import Claim, ClaimStatus
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.usage import Unit
from prismoid.schemas.verification import CheckStatus

BRIEF = (
    "# Northwind Q3 2026 operating brief\n\n"
    "- Revenue: **$41.6M** in Q3 2026, up from $33.9M in Q3 2025.\n"
    "- Gross margin: 46.5%.\n- Headcount: 312 at quarter end.\n"
)
MARKET = "Analysts expect the mid-market segment to grow 18% in 2027.\n"
SOURCES = [
    grounding.Source("S1", BRIEF, "https://northwind.example/q3"),
    grounding.Source("S2", MARKET, "https://analysts.example/mid"),
]
REVENUE = Claim(
    id="c1",
    statement="Revenue was $41.6M in Q3 2026 [S1].",
    figure="$41.6M",
    cites=["S1"],
    quote="Revenue: $41.6M in Q3 2026",
    quoted_figure="$41.6M",
)
PRIOR = Claim(
    id="c2",
    statement="up from $33.9M a year earlier [S1]",
    figure="$33.9M",
    cites=["S1"],
    quote="up from $33.9M in Q3 2025",
    quoted_figure="$33.9M",
)
GROWTH = Claim(id="c3", statement="Revenue grew 22.7%.", figure="22.7%", formula="(c1 - c2) / c2 * 100")
MARGIN = Claim(
    id="c4",
    statement="Gross margin held at 46.5% [S1].",
    figure="46.5%",
    cites=["S1"],
    quote="Gross margin: 46.5%",
    quoted_figure="46.5%",
)
SEGMENT = Claim(
    id="c5",
    statement="The mid-market segment should grow 18% in 2027 [S2].",
    figure="18%",
    cites=["S2"],
    quote="mid-market segment to grow 18% in 2027",
    quoted_figure="18%",
)
CLEAN = [REVENUE, PRIOR, GROWTH, MARGIN, SEGMENT]
MEMO = (
    "# Q3 memo\n\nRevenue was $41.6M in Q3 2026 [S1], up from $33.9M a year earlier [S1]. Revenue grew 22.7%.\n"
    "Gross margin held at 46.5% [S1]. The mid-market segment should grow 18% in 2027 [S2].\n"
)


@dataclass(frozen=True)
class Planted:
    """One defect: the memo with it, the claims a reader lists, and the outcome it must earn."""

    memo: str
    claims: list[Claim]
    status: ClaimStatus


PLANTED: dict[str, Planted] = {
    "wrong_number": Planted(
        MEMO.replace("$41.6M", "$41.9M"),
        [
            REVENUE.model_copy(
                update={"statement": "Revenue was $41.9M in Q3 2026 [S1].", "figure": "$41.9M"}
            ),
            *CLEAN[1:],
        ],
        ClaimStatus.WRONG_NUMBER,
    ),
    "unsupported_claim": Planted(
        MEMO + "Churn fell to 4.25% this quarter [S1].\n",
        [
            *CLEAN,
            Claim(id="c6", statement="Churn fell to 4.25% this quarter [S1].", figure="4.25%", cites=["S1"]),
        ],
        ClaimStatus.UNSUPPORTED,
    ),
    "wrong_citation": Planted(
        MEMO.replace("46.5% [S1]", "46.5% [S2]"),
        [*CLEAN[:3], MARGIN.model_copy(update={"cites": ["S2"]}), SEGMENT],
        ClaimStatus.MISCITED,
    ),
    "miscomputed": Planted(
        MEMO.replace("22.7%", "25.4%"),
        [
            *CLEAN[:2],
            GROWTH.model_copy(update={"statement": "Revenue grew 25.4%.", "figure": "25.4%"}),
            *CLEAN[3:],
        ],
        ClaimStatus.MISCOMPUTED,
    ),
}


def _statuses(memo: str, claims: list[Claim], **kw: Any) -> list[ClaimStatus]:
    return [f.status for f in grounding.ground(claims, SOURCES, document=memo, **kw)]


def test_the_clean_memo_passes_with_every_claim_held() -> None:
    findings = grounding.ground(CLEAN, SOURCES, document=MEMO)
    assert [f.status for f in findings] == [ClaimStatus.SUPPORTED] * 2 + [ClaimStatus.COMPUTED] + [
        ClaimStatus.SUPPORTED
    ] * 2
    check = grounding.check(findings)
    assert check.status is CheckStatus.PASS
    assert "22.7% = (c1 - c2) / c2 * 100" in check.detail, "the judge reads every computed figure"


@pytest.mark.parametrize("defect", sorted(PLANTED))
def test_each_planted_defect_fails_the_check(defect: str) -> None:
    case = PLANTED[defect]
    findings = grounding.ground(case.claims, SOURCES, document=case.memo)
    assert case.status in [f.status for f in findings]
    assert not any(f.status is ClaimStatus.UNCHECKED for f in findings), (
        "a defect is decided, never left over"
    )
    check = grounding.check(findings)
    assert check.status is CheckStatus.FAIL
    assert check.actual


def test_a_wrong_citation_names_the_source_that_states_it() -> None:
    case = PLANTED["wrong_citation"]
    miscited = [f for f in grounding.ground(case.claims, SOURCES, document=case.memo) if f.status.failed]
    assert miscited[0].detail == "stated in S1, not in S2 it cites"


def test_a_reader_slip_never_fails_the_author() -> None:
    misquoted = MARGIN.model_copy(update={"quote": "gross margin was a healthy 46.5%"})
    assert _statuses(MEMO, [misquoted]) == [ClaimStatus.UNCHECKED], "the figure is in S1: the judge reads it"
    invented = Claim(id="c9", statement="Bookings doubled to 88 deals [S1].", figure="88", cites=["S1"])
    assert _statuses(MEMO, [invented]) == [ClaimStatus.UNCHECKED], (
        "a claim not in the document is the reader's"
    )
    cut = PLANTED["unsupported_claim"]
    assert _statuses(cut.memo, cut.claims[-1:], clipped={"S1"}) == [ClaimStatus.UNCHECKED]


def test_a_quote_from_another_source_fails_only_a_cited_source_that_lacks_the_figure() -> None:
    other = MARGIN.model_copy(update={"cites": ["S2"], "quote": "Gross margin: 46.5%"})
    sources = [*SOURCES, grounding.Source("S3", "The gross margin came in at 46.5% for the quarter.")]
    thirds = other.model_copy(update={"cites": ["S3"]})
    assert [f.status for f in grounding.ground([thirds], sources, document=MEMO)] == [
        ClaimStatus.UNCHECKED
    ], "S3 holds 46.5%: the reader quoted S1, the judge reads S3"
    shown = grounding.ground([thirds], sources, clipped={"S3"}, document=MEMO)
    assert [f.status for f in shown] == [ClaimStatus.UNCHECKED], "S3 was cut for the reader"
    assert _statuses(MEMO, [other]) == [ClaimStatus.MISCITED], "S2, read whole, never states 46.5%"


def test_a_grouped_figure_the_reader_missed_is_still_found_in_its_source() -> None:
    sources = [grounding.Source("S9", "Pune city: population 3,124,458 as per the 2011 census.")]
    claim = Claim(
        id="c8",
        statement="Pune | 3,124,458 [S9]",
        figure="3,124,458",
        cites=["S9"],
        quote="Pune has 3,124,458 people",
        quoted_figure="3,124,458",
    )
    findings = grounding.ground([claim], sources, document="| Pune | 3,124,458 [S9] |")
    assert [f.status for f in findings] == [ClaimStatus.UNCHECKED], "S9 holds 3,124,458"


def test_an_uncited_figure_grounds_in_any_source_and_fails_in_none() -> None:
    uncited = MARGIN.model_copy(update={"cites": []})
    assert _statuses(MEMO, [uncited]) == [ClaimStatus.SUPPORTED]
    nowhere = Claim(id="c7", statement="Revenue was $41.6M", figure="$41.6M")
    assert _statuses(MEMO, [nowhere]) == [ClaimStatus.UNCHECKED], "its digits are in S1, so the judge decides"


@pytest.mark.parametrize(
    ("literal", "value", "percent"),
    [
        ("$41.6M", Decimal("41600000"), False),
        ("$41.6 million", Decimal("41600000"), False),
        ("22.7%", Decimal("22.7"), True),
        ("1,204", Decimal("1204"), False),
        ("(3.2)", Decimal("-3.2"), False),
        ("₹2.5 crore", Decimal("25000000"), False),
        ("312 people", Decimal("312"), False),
    ],
)
def test_a_figure_reads_at_its_scale(literal: str, value: Decimal, percent: bool) -> None:
    figure = grounding.figure_of(literal)
    assert figure is not None
    assert (figure.value, figure.percent) == (value, percent)


def test_the_tolerance_is_the_precision_the_document_shows() -> None:
    def held(shown: str, source: str, *, approx: bool = False) -> bool:
        claim = MARGIN.model_copy(
            update={
                "figure": shown,
                "quote": f"Gross margin: {source}",
                "quoted_figure": source,
                "approx": approx,
            }
        )
        texts = [grounding.Source("S1", f"Gross margin: {source}")]
        return (
            grounding.ground([claim], texts, document=f"held at {shown}")[0].status is ClaimStatus.SUPPORTED
        )

    assert held("46.5%", "46.5%") and held("47%", "46.5%") and held("$42M", "$41.6M")
    assert not held("47%", "46.48%"), "47% claims [46.5, 47.5): a true 46.48% rounds to 46"
    assert not held("$41.9M", "$41.6M")
    assert held("$40M", "$41.6M", approx=True), "about $40M admits to its leading digit"


def test_a_formula_is_arithmetic_over_claims_and_nothing_else() -> None:
    values = {"c1": Decimal(10), "c2": Decimal(4)}
    assert grounding.evaluate("(c1 - c2) / c2 * 100", values) == Decimal(150)
    assert grounding.evaluate("c1 × c2", values) == Decimal(40)
    for hostile in ("__import__('os').system('true')", "c1.real", "c9 + 1", "c1 ** 2", "c1 / 0"):
        assert grounding.evaluate(hostile, values) is None


def test_the_reading_is_bounded_and_says_what_it_cut() -> None:
    sources = [grounding.Source(f"S{n}", "x" * 30_000) for n in (10, 2, 1, 3, 4, 5)]
    reading = grounding.reading("memo", sources)
    assert len(reading.prompt) < grounding.READING_CHARS + 10_000
    assert reading.clipped == {"S1", "S2", "S3", "S4", "S5", "S10"}
    assert reading.prompt.index("[S2]") < reading.prompt.index("[S10]"), "a stable order, ids by number"
    assert "cut at 24,000 of 30,000 characters" in reading.prompt, "a cut page says so"


def test_a_page_given_two_ids_is_read_once() -> None:
    sources = [grounding.Source("S1", "x" * 30_000), grounding.Source("S2", "x" * 30_000)]
    reading = grounding.reading("memo", sources)
    assert reading.prompt.count("x" * grounding.SOURCE_CHARS) == 1
    assert "(the same text as [S1])" in reading.prompt
    assert reading.clipped == {"S1", "S2"}, "the second id was cut as the first"


def test_no_claims_is_a_skip_never_a_pass() -> None:
    assert grounding.check([]).status is CheckStatus.SKIP


# ── the receipt path: the real reader on a scripted model, pages under their source ids ─────────


@dataclass
class _Keeper:
    """The source store's keeper for one task: kept page texts by sha."""

    texts: dict[str, str]

    async def read(self, sha: str) -> str:
        return self.texts[sha]


class _Session:
    """Only what the receipt's grounding reads: the artifact's task."""

    def __init__(self) -> None:
        self.task = SimpleNamespace(id=uuid4(), project_id=None)

    async def get(self, _model: object, _id: object) -> object:
        return self.task


async def _receipt(
    monkeypatch: pytest.MonkeyPatch, memo: str, claims: list[Claim], given: dict[str, str] | None = None
) -> tuple[Any, list[str]]:
    """The receipt's grounding checks for `memo`, the reader scripted to list `claims` (`given`: the task's
    source ids); and what it was shown."""
    shown: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        shown.append(str(messages))
        args = {"claims": [c.model_dump() for c in claims]}
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])

    row = ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get("claim_extract").model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )
    runner = build("claim_extract", TaskDeps(task_id=None), [row])

    async def catalog(_session: object) -> tuple[list[ModelSpec], dict[str, list[str]]]:
        return [row], {}

    async def settings(_session: object) -> dict[str, Any]:
        return {}

    async def ids(_task_id: object) -> dict[str, str]:
        return given or {"S1": "https://northwind.example/q3?utm=x", "S2": "https://analysts.example/mid"}

    async def stored(_session: object, _task_id: object) -> list[sources_ops.StoredPage]:
        return [
            sources_ops.StoredPage("analysts.example/mid", "b" * 64),
            sources_ops.StoredPage("northwind.example/q3", "a" * 64),
        ]

    monkeypatch.setattr(model_ops, "routed_catalog", catalog)
    monkeypatch.setattr(routing_ops, "settings_for", settings)
    monkeypatch.setattr(sources_ops, "source_ids", ids)
    monkeypatch.setattr(sources_ops, "stored", stored)
    monkeypatch.setattr(sources_ops, "keeper", lambda _t: _Keeper({"a" * 64: BRIEF, "b" * 64: MARKET}))
    monkeypatch.setattr(usage_ops, "emitter", lambda *_a, **_k: None)
    monkeypatch.setattr(grounding_ops.agents, "build", lambda *_a, **_k: runner)
    artifact = cast(Artifact, SimpleNamespace(task_id=uuid4(), step_id=None))
    session = cast(AsyncSession, _Session())
    with runner.agent.override(model=FunctionModel(model)):
        pages = await grounding_ops.kept(session, artifact.task_id)
        checks = await grounding_ops.ground(
            session, artifact, VerifierSuite.DOCUMENT, memo, files={}, pages=pages
        )
    return checks, shown


async def test_the_receipt_reads_once_and_fails_a_planted_wrong_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = PLANTED["wrong_number"]
    checks, shown = await _receipt(monkeypatch, case.memo, case.claims)
    assert len(shown) == 1, "one bounded call"
    assert "[S1] https://northwind.example/q3" in shown[0], "a kept page reads under its source id"
    assert [c.status for c in checks] == [CheckStatus.FAIL]
    assert "S1 states $41.6M, not $41.9M" in str(checks[0].actual)


async def test_the_receipt_passes_the_clean_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    checks, _ = await _receipt(monkeypatch, MEMO, CLEAN)
    assert [(c.name, c.status) for c in checks] == [("grounding:claims", CheckStatus.PASS)]


async def test_every_id_a_page_was_given_reads_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    given = {
        "S1": "https://northwind.example/q3?utm=x",
        "S2": "https://analysts.example/mid",
        "S3": "https://northwind.example/q3",
    }
    checks, shown = await _receipt(monkeypatch, MEMO, CLEAN, given)
    assert (
        "[S1] https://northwind.example/q3?utm=x" in shown[0]
        and "[S3] https://northwind.example/q3" in shown[0]
    )
    assert [(c.name, c.status) for c in checks] == [("grounding:claims", CheckStatus.PASS)], (
        "a page given two ids is cited by either"
    )


async def test_only_a_document_on_the_claims_path_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    checks, shown = await _receipt(monkeypatch, "  ", CLEAN)
    assert shown == [] and [c.status for c in checks] == [CheckStatus.SKIP]
    session = cast(AsyncSession, _Session())
    artifact = cast(Artifact, SimpleNamespace(task_id=uuid4(), step_id=None))
    assert await grounding_ops.ground(session, artifact, VerifierSuite.DECK, MEMO, files={}, pages=[]) == []
    assert (
        await grounding_ops.ground(session, artifact, VerifierSuite.DOCUMENT, None, files={}, pages=[]) == []
    ), "no suite read it"


def test_the_judge_reads_only_the_pages_the_document_cites_within_one_bound() -> None:
    pages = [
        grounding.Source(f"S{n}", f"page {n} " + "filler words " * 2_000, f"https://site{n}.example/p")
        for n in range(1, 7)
    ]
    memo = "Revenue rose [S2]; see [[1]](<https://site4.example/p>)."
    shown = "\n".join(grounding.judged_sources(pages, memo, "n0nce"))
    assert "[S2]" in shown and "[S4]" in shown, "a mark or a rendered link cites a page"
    assert not any(f"[S{n}]" in shown for n in (1, 3, 5, 6)), "an uncited page is not the judge's"
    assert len(shown) < grounding.JUDGE_CHARS + 1_000

    uncited = "\n".join(grounding.judged_sources(pages, "a memo that cites nothing", "n0nce"))
    assert all(f"[S{n}]" in uncited for n in range(1, 7)), "with no citation, every page is shown"
    assert len(uncited) < grounding.JUDGE_CHARS + 2_000, "under the same one bound"
