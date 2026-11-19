# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The long-form recipe's values (ADR-025): one outline step, then the parts in order sharing one cached head,
then assembly; the 30,000-word ceiling; and `[S#]` marks rendered once across the parts. Pure values, no model."""

from __future__ import annotations

import uuid
from decimal import Decimal
from itertools import pairwise

import pytest

from prismoid.ai.agents.base import definition_for
from prismoid.ai.context.brief import prompt_for
from prismoid.ai.context.citations import render_citations, render_parts
from prismoid.ai.deps import TaskDeps
from prismoid.operations import longform
from prismoid.schemas.ids import StepId
from prismoid.schemas.longform import FigureSpec, OutlineResult, SectionOutline, render_outline_md
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType, SubmittedPart
from prismoid.schemas.plan import PlanEdge, PlanFacts, StepSpec
from prismoid.schemas.sizing import LONGFORM_MAX_WORDS, SizeBucket
from prismoid.schemas.usage import Unit

PRIOR = ("/task/inputs/01-a.md", "/task/inputs/02-b.md")
SOURCES = ["/task/inputs/brief.txt"]


def _section(n: int, words: int = 1500, **extra: object) -> SectionOutline:
    return SectionOutline.model_validate(
        {"id": f"s{n:02d}", "title": f"Part {n}", "brief": f"cover topic {n}", "target_words": words, **extra}
    )


def _outline(*sections: SectionOutline) -> OutlineResult:
    return OutlineResult(
        filename="guide",
        title="The Guide",
        continuity_bible="Second person; 'the bin' always.",
        voice_exemplar="You start small.",
        summary="A guide.",
        sections=list(sections) or [_section(n) for n in (1, 2, 3, 4)],
    )


def _expand(
    outline: OutlineResult,
    *,
    units: int = 40,
    bucket: SizeBucket = SizeBucket.LONG,
    prior_sections: tuple[str, ...] = (),
    prior_figures: tuple[str, ...] = (),
) -> tuple[StepId, list[tuple[StepId, StepSpec]], list[PlanEdge]]:
    outline_step = StepId(uuid.uuid4())
    steps, edges = longform.expand(
        outline,
        source_paths=SOURCES,
        goal="a 40-page guide to composting",
        outline_step=outline_step,
        bucket=bucket,
        target_units=units,
        prior_sections=prior_sections,
        prior_figures=prior_figures,
    )
    return outline_step, steps, edges


# ── the plan ──────────────────────────────────────────────────────────────────────────────────


def test_the_plan_is_one_writer_step_designing_the_outline() -> None:
    [(_, spec)], edges, _ = longform.longform_plan("a 40-page guide", target_pages=40)
    assert (spec.agent, spec.lane_role, spec.artifact_role) == ("writer", "outline", "working")
    assert edges == []


def test_a_consensus_outline_is_proposals_and_a_merge_held_to_the_same_bounds() -> None:
    steps, edges, _ = longform.longform_plan(
        "a 40-page guide", target_pages=40, consensus=[("a", "model-a"), ("b", "model-b")]
    )
    roles = [spec.lane_role for _, spec in steps]
    assert roles == ["outline_draft", "outline_draft", "outline"]
    merge = steps[-1][1]
    assert merge.join == "consensus" and len(edges) == 2
    assert merge.contract is not None and steps[0][1].contract is not None
    assert set(steps[0][1].contract.acceptance) <= set(merge.contract.acceptance)


def test_the_outline_and_its_parts_are_the_writers_modes() -> None:
    outline = definition_for("writer", TaskDeps(lane_role="outline"))
    part = definition_for("writer", TaskDeps(lane_role="section"))
    assert outline.output_ref == "outline_result" and part.output_ref == "part"
    assert not outline.can_ask_user and not part.can_ask_user
    assert "## Writing one part" in part.prompt and "## Designing the outline" not in part.prompt
    assert "## Designing the outline" in outline.prompt


# ── the parts, in order ───────────────────────────────────────────────────────────────────────


def test_the_parts_run_in_order_and_all_feed_assembly() -> None:
    outline_step, steps, edges = _expand(_outline())
    parts = [sid for sid, spec in steps if spec.lane_role == "section"]
    [(assembly_id, assembly)] = [(sid, spec) for sid, spec in steps if spec.lane_role == "assembly"]
    chain = [(e.from_step, e.to_step) for e in edges if e.kind == "depends_on"]
    assert chain == list(pairwise([outline_step, *parts]))
    feeds = {e.from_step for e in edges if e.to_step == assembly_id and e.kind == "produces_for"}
    assert feeds == {*parts, outline_step}
    assert assembly.agent == "maker" and assembly.qa and assembly.section_total == 4
    assert all(spec.join == "quorum" for _, spec in steps), "a lost part never stalls the next"
    assert [spec.title for _, spec in steps][:4] == ["01 Part 1", "02 Part 2", "03 Part 3", "04 Part 4"]


def test_every_part_shares_one_head_and_only_its_assignment_differs() -> None:
    _, steps, _ = _expand(_outline(), prior_sections=PRIOR)
    parts = [spec for _, spec in steps if spec.lane_role == "section"]
    heads = {
        prompt_for(spec.model_copy(update={"contract": spec.contract.model_copy(update={"notes": ""})}))
        for spec in parts
        if spec.contract
    }
    assert len(heads) == 1, "the cached head must be the same bytes for every part"
    [head] = heads
    assert render_outline_md(_outline()) in head, "the whole outline rides the head"
    notes = [spec.contract.notes for spec in parts if spec.contract]
    assert len(set(notes)) == len(parts)
    assert notes[0].startswith('## Your part: 01 of 04, "Part 1"')
    assert "This is the closing part" in notes[-1] and "closing part" not in notes[0]


def test_a_part_names_its_fence_its_figure_and_its_budget() -> None:
    figure = FigureSpec(id="fig-s02-1", spec="a bin cross-section", caption_hint="Inside the bin")
    outline = _outline(
        _section(1), _section(2, not_here="watering; s03 owns it", figures=[figure]), _section(3), _section(4)
    )
    _, steps, _ = _expand(outline)
    notes = steps[1][1].contract.notes if steps[1][1].contract else ""
    assert "Out of scope (another part owns it, never restate it): watering; s03 owns it" in notes
    assert "`Figure (fig-s02-1): Inside the bin`" in notes
    assert "Target 1500 words" in notes and "past 1725 words" in notes


# ── the refusals the outline step's retry reads ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outline", "message"),
    [
        (
            _outline(_section(1), _section(1)),
            "two sections share an id - renumber them s01, s02, … uniquely",
        ),
        (
            _outline(_section(1, words=600), _section(2), _section(3), _section(4)),
            "section s01 targets 600 words - at this size (long) keep every section between 750 and 3000",
        ),
        (
            _outline(_section(1, kept_from="/task/inputs/09-z.md"), _section(2), _section(3), _section(4)),
            "section s01 sets kept_from to /task/inputs/09-z.md, which is not one of the last document's sections",
        ),
    ],
)
def test_an_outline_this_size_cannot_hold_is_refused_with_its_fix(
    outline: OutlineResult, message: str
) -> None:
    with pytest.raises(longform.InvalidOutline) as refused:
        _expand(outline, prior_sections=PRIOR)
    assert str(refused.value).startswith(message)


def test_no_outline_writes_past_the_ceiling() -> None:
    eleven = _outline(*(_section(n, words=2800) for n in range(1, 12)))  # 30,800 words
    with pytest.raises(longform.InvalidOutline) as refused:
        _expand(eleven, units=100, bucket=SizeBucket.BOOK)
    assert f"holds at most {LONGFORM_MAX_WORDS:,}" in str(refused.value)
    ten = _outline(*(_section(n, words=3000) for n in range(1, 11)))  # exactly 30,000
    _expand(ten, units=100, bucket=SizeBucket.BOOK)
    assert longform.target_words(ArtifactType.DOCUMENT, SizeBucket.BOOK, 160) == LONGFORM_MAX_WORDS


def test_a_kept_section_buys_no_figure_and_assembly_mounts_the_last_documents() -> None:
    kept = _section(1, kept_from=PRIOR[0], figures=[FigureSpec(id="fig-s01-1", spec="again")])
    outline = longform.settle_kept(_outline(kept, _section(2), _section(3), _section(4)))
    assert outline.sections[0].figures == []
    _, steps, _ = _expand(outline, prior_sections=PRIOR, prior_figures=("/task/inputs/fig-s01-1.png",))
    assembly = steps[-1][1]
    assert assembly.contract is not None and "/task/inputs/fig-s01-1.png" in assembly.contract.inputs
    assert steps[0][1].kept_from == PRIOR[0]


@pytest.mark.parametrize("kind", [ArtifactType.DOCUMENT, ArtifactType.DECK, ArtifactType.SPREADSHEET])
def test_assembly_is_judged_on_the_document_not_on_how_it_was_handed_over(kind: ArtifactType) -> None:
    spec = longform.assembly_spec(_outline(), source_paths=SOURCES, goal="a guide", kind=kind)
    assert spec.contract is not None and spec.contract.output is not None
    assert spec.contract.output.type is kind
    assert not any("/work" in line or "type='" in line for line in spec.contract.acceptance)


# ── the estimate ──────────────────────────────────────────────────────────────────────────────


def _book(image_usd: str = "0.05") -> PlanFacts:
    """Every class priced as one text row, image-gen also by the image."""
    text = {Unit.INPUT_TOKEN: Decimal("0.000001"), Unit.OUTPUT_TOKEN: Decimal("0.000005")}
    prices = {c: dict(text) for c in ModelClass} | {ModelClass.IMAGE_GEN: {Unit.IMAGE: Decimal(image_usd)}}
    return PlanFacts(prices=prices)


def test_the_estimate_grows_with_the_ask_reads_its_prices_and_stops_at_the_ceiling() -> None:
    small, big = longform.lane_estimate(30, _book("0")), longform.lane_estimate(60, _book("0"))
    assert big.sections > small.sections and big.ceiling_usd > small.ceiling_usd, "each part is a run"
    assert small.ceiling_usd > small.usd > 0
    assert longform.lane_estimate(30, _book("1.00")).usd > longform.lane_estimate(30, _book()).usd, (
        "the figures bill at the image row's price"
    )
    capped = longform.lane_estimate(160, _book("0"), bucket=SizeBucket.BOOK)
    assert capped.usd == longform.lane_estimate(100, _book("0"), bucket=SizeBucket.BOOK).usd, "the words stop"


# ── `[S#]` through assembly ───────────────────────────────────────────────────────────────────


def test_the_parts_citations_render_once_under_one_numbering() -> None:
    pages = {"S1": "https://a.example/x", "S2": "https://b.example/y", "S3": "https://c.example/z"}
    parts = ["## One\n\nFirst [S2]. Then [S1].", "## Two\n\nAgain [S2], new [S3]."]
    bodies, listed = render_parts(parts, pages)
    assert bodies == [
        "## One\n\nFirst [[1]](<https://b.example/y>). Then [[2]](<https://a.example/x>).",
        "## Two\n\nAgain [[1]](<https://b.example/y>), new [[3]](<https://c.example/z>).",
    ]
    assert (
        listed
        == "## Sources\n\n1. <https://b.example/y>\n2. <https://a.example/x>\n3. <https://c.example/z>\n"
    )
    assert render_citations("\n\n".join(parts), pages) == "\n\n".join(bodies) + "\n\n" + listed


def test_a_submitted_part_carries_its_summary_for_the_parts_after_it() -> None:
    part = SubmittedPart(content="## Part 1\n\nText [S1].", summary="It set up the bin.")
    assert part.content.startswith("## Part 1") and part.summary


def test_a_writer_definition_is_still_the_documents_writer() -> None:
    writer = definition_for("writer", TaskDeps())
    assert writer.output_ref == "document" and writer.can_ask_user
