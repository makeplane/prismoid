# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The long-form lane's values (ADR-025) - the outline, its sections, and the cost estimate.

`OutlineResult` is the outline step's typed answer (the writer's outline mode): the system publishes it as
`outline.md` (working role), and the deterministic expansion (`operations/longform.expand`) turns it into the
document's parts, written in order, and one assembly - no model in that second half.

Flat, primitives + one nesting level, every bound stated in the schema: a 25-section outline is
refused HERE with a message the outline step's retry can act on, not discovered as a stuck plan.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.sizing import WORDS_PER_PAGE as WORDS_PER_PAGE  # one yardstick

# The lane's shape constants live with the schema that enforces them; the policy layer imports
# from here so the two can never disagree about what a legal outline is.
#
# SECTION_WORDS_MIN is the ABSOLUTE floor (a section below it is a paragraph wearing a
# heading), not the bucket's: the per-bucket floor SCALES with the ask
# (`sizing.section_bounds`) and is enforced by `expand()` where the message can be
# legible. The old flat 800 here made a 12-page outline an impossible arithmetic the schema
# rejected before policy could explain - three live outline ask-gates burned on exactly it.
LONGFORM_MAX_SECTIONS = 24
SECTION_WORDS_MIN = 400
SECTION_WORDS_MAX = 3000
# 8, not 3: a DECK section is a slide-GROUP of up to 8 slides,
# and the deck lane's law is an image on every slide - the cap must fit the group. Document
# outlines still propose sparingly (their asks say "where a picture beats prose").
FIGURES_PER_SECTION_MAX = 8


class FigureSpec(BaseModel):
    """One figure a section must produce. `spec` says what to DEPICT (the image prompt is the
    writer's to compose from it); charts are NOT figures - they are matplotlib scripts."""

    model_config = ConfigDict(frozen=True)

    # The pattern is load-bearing: the id becomes a frame path
    # (`/work/{id}.png`) and the walkers' marker grep (`fig-[\w-]+`) - a model-authored `/`,
    # project or stray prefix would degrade to broken mounts and orphaned markers. Refused HERE,
    # where the outline step's retry can read why, not discovered as a placeholder deck.
    id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^fig-[a-z0-9][a-z0-9-]*$",
        description="e.g. fig-s03-1 - stable, kebab, always fig- prefixed",
    )
    spec: str = Field(min_length=1, max_length=500, description="what the image depicts")
    caption_hint: str = Field(default="", max_length=200)


class SectionOutline(BaseModel):
    """One chapter, designed so a writer who can talk to nobody can still write it."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=8, pattern=r"^s\d{2}$", description="s01…s24, in order")
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(
        min_length=1,
        max_length=2000,
        description="what this section must cover, precisely enough for a stranger to execute",
    )
    # The scope fence: the largest measured coherence lever in the literature is
    # outline DETAIL - naming what a section must NOT cover, because a sibling owns it, is the
    # pre-draft attack on cross-section redundancy. Empty = the partition is unambiguous.
    not_here: str = Field(
        default="",
        max_length=400,
        description="what this section must NOT cover because a named other section owns it",
    )
    target_words: int = Field(ge=SECTION_WORDS_MIN, le=SECTION_WORDS_MAX)
    figures: list[FigureSpec] = Field(default_factory=list[FigureSpec], max_length=FIGURES_PER_SECTION_MAX)
    # A revision's kept section: the system publishes the last document's file as this part, unchanged.
    kept_from: str = Field(
        default="",
        max_length=200,
        description="revisions only: the /task/inputs/ path of the last document's section kept unchanged",
    )


class OutlineResult(BaseModel):
    """The outline step's whole answer. The system renders it to `outline.md` and publishes it as
    a WORKING artifact; the verbatim JSON rides the `PlanEdit(kind=EXPAND)` payload."""

    model_config = ConfigDict(frozen=True)

    filename: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="kebab stem for the final deliverable, no extension",
    )
    title: str = Field(min_length=1, max_length=200)
    format_target: Literal["md", "docx", "pdf", "pptx", "xlsx"] = "md"  # pptx/xlsx: own lanes
    front_matter: str = Field(default="", max_length=1000, description="what assembly adds before s01")
    back_matter: str = Field(
        default="", max_length=1000, description="what assembly adds after the last section"
    )
    continuity_bible: str = Field(
        min_length=1,
        max_length=4000,
        description="the writers' shared law: voice, tense, terminology, what each recurring entity is called",
    )
    # Style by example: one paragraph in the document's exact target voice beats any
    # list of adjectives - every part imitates THIS. Empty = bible only.
    voice_exemplar: str = Field(
        default="",
        max_length=900,
        description="one exemplar paragraph written in the document's exact target voice",
    )
    sections: list[SectionOutline] = Field(min_length=1, max_length=LONGFORM_MAX_SECTIONS)
    summary: str = Field(min_length=1, max_length=500, description="the document in three sentences")
    # The consensus merge's record: where the proposals disagreed and what was ruled.
    # Its OWN field, sized for the job - the first live merge died 12 schema rejections in a
    # row trying to obey "record every divergence" inside summary's 500-char clamp: an
    # instruction-schema conflict no retry can win. Empty for a single outline.
    divergence_note: str = Field(
        default="",
        max_length=2500,
        description="consensus merges only: each material divergence between proposals and the ruling taken",
    )


class LaneEstimate(BaseModel):
    """The long-form lane's projected cost (`run_cost` rows at the head-row prices), read by the expansion-time
    consent gate and the wave projection."""

    model_config = ConfigDict(frozen=True)

    sections: int
    figures: int
    usd: Decimal
    ceiling_usd: Decimal


def render_outline_md(outline: OutlineResult) -> str:
    """The outline as markdown: the published `outline.md`, and the head every part shares."""
    lines = [
        f"# {outline.title}",
        "",
        f"_Target format: .{outline.format_target} · {len(outline.sections)} sections_",
        "",
        f"**Summary:** {outline.summary}",
        "",
        "## Continuity bible",
        "",
        outline.continuity_bible,
        "",
    ]
    if outline.divergence_note:
        lines += [
            "## Consensus divergences (what the proposals disagreed on, and the rulings)",
            "",
            outline.divergence_note,
            "",
        ]
    if outline.voice_exemplar:
        lines += [
            "## Voice exemplar",
            "",
            "Write EXACTLY like this paragraph:",
            "",
            f"> {outline.voice_exemplar}",
            "",
        ]
    lines += ["## Sections", ""]
    for index, section in enumerate(outline.sections):
        lines += [f"### {index + 1:02d} · {section.title}  ({section.target_words} words)", "", section.brief]
        if section.kept_from:
            lines.append(f"- **Kept unchanged from {section.kept_from}**")
        if section.not_here:
            lines.append(f"- **Not covered here** (another section owns it): {section.not_here}")
        lines += [f"- Figure `{fig.id}`: {fig.spec}" for fig in section.figures]
        lines.append("")
    if outline.front_matter:
        lines += ["## Front matter", "", outline.front_matter, ""]
    if outline.back_matter:
        lines += ["## Back matter", "", outline.back_matter, ""]
    return "\n".join(lines)
