# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The long-form recipe (ADR-025): a typed outline, then its parts written one after another, then assembly.

Past one run's output (`sizing.runs_longform`: over `ONE_RUN_MAX_WORDS`, never past `LONGFORM_MAX_WORDS`) a document
is planned here as ONE outline step: the writer, in its outline mode, designs a typed `OutlineResult`. When it
lands, `expand_after_outline` writes, in the outline step's completion transaction, one writer step per section
chained in order and one assembly join. Every part shares one goal, the document's brief with the whole outline,
byte for byte: it is the cached head of each part's prompt, and only the part's assignment and what the parts
before it established (`part_tail`) follow it. Parts keep their `[S#]` marks; assembly renders them once, under
one numbering (`render_parts`), and the maker builds the file. No model shapes the graph: an outline that breaks
the rules fails its own attempt with a legible gotcha (`InvalidOutline`).

The expansion stays WRITE-ONLY AND FAST: the caller holds the task row's seq lock through the commit.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Sequence
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents
from prismoid.ai.agents.writer import OUTLINE, PART
from prismoid.ai.context.citations import UnknownSource, render_parts
from prismoid.ai.frames.driver import FrameDriver
from prismoid.ai.frames.layout import input_paths
from prismoid.ai.gateway.run_cost import FIGURE_SIZE, RUN_SIZES, class_line
from prismoid.ai.rules.plan import Plan, input_clause
from prismoid.ai.skills import deliverables
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanEdit as PlanEditRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import PolicyDenied
from prismoid.schemas import sizing
from prismoid.schemas.contract import Contract, Criteria, SkillPin
from prismoid.schemas.events import PlanEdited, StepNarration, TaskPlanned
from prismoid.schemas.ids import AgentName, ProjectId, StepId, TaskId
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.longform import (
    LONGFORM_MAX_SECTIONS,
    SECTION_WORDS_MAX,
    WORDS_PER_PAGE,
    LaneEstimate,
    OutlineResult,
    render_outline_md,
)
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import (
    LaneRole,
    PlanEdge,
    PlanEditKind,
    PlanEstimate,
    PlanFacts,
    StepKind,
    StepSpec,
    StepState,
)
from prismoid.schemas.plan import PlanEdit as PlanEditView
from prismoid.schemas.sizing import LONGFORM_MAX_WORDS, SizeBucket

ASSEMBLY_QUORUM: Final = 0.8  # below 80% of the parts, the document is not the deliverable that was asked for
OUTLINE_FILENAME: Final = "outline.md"  # the outline's published name: assembly mounts it, QA reads it
SOURCES_FILENAME: Final = "sources.md"  # the rendered `[S#]` numbering's list, mounted after the last part
_WRITER: Final = AgentName("writer")


class InvalidOutline(Exception):
    """The outline cannot expand; the message is the outline step's retry gotcha."""


# ── the plan: one outline step ───────────────────────────────────────────────────────────────


def target_words(kind: ArtifactType, bucket: SizeBucket, target_units: int | None) -> int:
    """The document's words: the asked units (or the bucket's midband), never past `LONGFORM_MAX_WORDS`."""
    spec = sizing.spec_for(kind, bucket)
    units = target_units or (spec.target_lo + spec.target_hi) // 2
    return min(LONGFORM_MAX_WORDS, units * (spec.words_per_unit or WORDS_PER_PAGE))


def section_count(
    target_units: int, kind: ArtifactType = ArtifactType.DOCUMENT, bucket: SizeBucket = SizeBucket.LONG
) -> int:
    """How many parts the outline should carry, within the bucket's bounds."""
    total = target_words(kind, bucket, target_units)
    _floor, lo, hi, per_section = sizing.section_bounds(kind, bucket, total)
    return max(lo, min(hi, round(total / max(1, per_section))))


def lane_estimate(
    target_pages: int,
    known: PlanFacts,
    *,
    kind: ArtifactType = ArtifactType.DOCUMENT,
    bucket: SizeBucket = SizeBucket.LONG,
) -> LaneEstimate:
    """The recipe's cost at `known`'s head-row prices: the outline, each part and the assembly as one run each,
    and the figures."""
    sections = section_count(target_pages, kind, bucket)
    if kind is ArtifactType.DECK:
        figures = max(1, target_pages - sections - 1)  # one image per content slide
    else:
        figures = max(1, round(target_pages / 8)) if kind is ArtifactType.DOCUMENT else 0
    made = deliverables.format_for(kind)
    assembler = made.model_class if made is not None else agents.get("maker").model_class
    writer = RUN_SIZES["writer"]
    lines = (
        class_line("outline", OUTLINE.model_class, writer, known),
        class_line("parts", PART.model_class, writer.times(sections), known),
        class_line("assembly", assembler, RUN_SIZES["maker"], known),
        class_line("figures", ModelClass.IMAGE_GEN, FIGURE_SIZE.times(figures), known),
    )
    projected = PlanEstimate.of(lines, None)
    return LaneEstimate(
        sections=sections, figures=figures, usd=projected.usd, ceiling_usd=projected.ceiling_usd
    )


_DESIGN_ASKS: Final = (
    "Produce the typed outline: sections that partition the material with no overlap and no gap, a brief per "
    "section a stranger could execute, a `not_here` fence naming what each section must NOT cover because a "
    "sibling owns it, word targets that sum to ~{words:,} words, figure specs where a picture beats prose, the "
    "continuity bible every part obeys, and a `voice_exemplar` paragraph in the document's exact voice."
)

# Parts are markdown for every kind; only what a section MEANS changes, and assembly with it.
_KIND_ASKS: Final = {
    ArtifactType.DECK: (
        " This deliverable is a PRESENTATION (`format_target: pptx`): each section is a SLIDE-GROUP whose brief "
        "lists its slides (a title and the one idea each carries); its word target covers slide content and "
        "speaker notes. The deck opens with ONE title slide and each group with ONE divider slide, and those "
        "count toward the asked total: budget the content slides to (ask - groups - 1). Give EVERY content "
        "slide its own FigureSpec (id fig-sNN-K, in slide order) describing its full-bleed visual."
    ),
    ArtifactType.SPREADSHEET: (
        " This deliverable is a WORKBOOK (`format_target: xlsx`): each section is ONE SHEET whose brief names "
        "its purpose, its columns, the rows' grain, and every DERIVED column's derivation in words "
        "('Revenue = Ships x Due'); no figure specs (sheets chart from their own data)."
    ),
}


def _revision_asks(prior_sections: Sequence[str]) -> str:
    """A revision's ask: the last document's section files, and what a kept or revised section does with them."""
    if not prior_sections:
        return ""
    listed = "".join(f"\n- {path}" for path in prior_sections)
    return (
        "\n\nThis turn REVISES the document this thread delivered last. The markdown it was assembled from is "
        f"mounted read-only, one file per section:{listed}\nRead them before you design. A section you keep sets "
        "`kept_from` to its file's path (target_words about its length): the system publishes it unchanged. The "
        "brief of a section you revise names the file it starts from."
    )


def _design_acceptance(
    target_pages: int, paths: Sequence[str], kind: ArtifactType, bucket: SizeBucket
) -> list[str]:
    total = target_words(kind, bucket, target_pages)
    floor, lo, hi, _target = sizing.section_bounds(kind, bucket, total)
    return [
        "the outline partitions the subject: no two sections overlap, nothing the goal requires is missing, "
        "and contested ground is fenced with `not_here`",
        f"the outline FITS its size ({sizing.size_marker(kind, bucket, target_pages)}): {lo}-{hi} sections, "
        f"each targeting {floor}-{SECTION_WORDS_MAX} words - MERGE sections to fit rather than asking",
        f"section word targets sum to within 15% of {total:,}, and never past {LONGFORM_MAX_WORDS:,}",
        "every brief is executable by a writer who sees only the outline, the sources and the parts before it",
        *input_clause(paths),
    ]


def _outline_spec(
    goal: str, asks: str, acceptance: list[str], shown: list[str], *, marker: str, role: LaneRole
) -> StepSpec:
    """One writer step in an outline mode: the merge's (or the only) `outline`, or a consensus `outline_draft`."""
    return StepSpec(
        kind=StepKind.STEP,
        agent=_WRITER,
        lane_role=role,
        artifact_role="working",
        title=f"Design the document - {marker}"[:120],
        contract=Contract(
            goal=f"Design the deliverable ({marker}) that answers: {goal}\n\n{asks}",
            acceptance=acceptance,
            inputs=shown,
            output=OutputRef(type=ArtifactType.DOCUMENT, title=f"Outline - {goal[:60]}"),
        ),
    )


def longform_plan(
    goal: str,
    inputs: Sequence[TaskInput] = (),
    *,
    target_pages: int,
    consensus: Sequence[tuple[str, str]] = (),
    kind: ArtifactType = ArtifactType.DOCUMENT,
    bucket: SizeBucket = SizeBucket.LONG,
    doc_format: str | None = None,
    prior_sections: Sequence[str] = (),
) -> Plan:
    """The outline step; at `quality=max` (`consensus`: two or more families) proposals and a merge instead."""
    paths = input_paths(i.filename for i in inputs)
    shown = [*paths, *prior_sections]
    marker = sizing.size_marker(kind, bucket, target_pages)
    asks = _DESIGN_ASKS.format(words=target_words(kind, bucket, target_pages))
    asks += _KIND_ASKS.get(kind, " This deliverable is a document; set `format_target` accordingly.")
    if doc_format is not None:
        asks += f" The user chose the file format: set `format_target: {doc_format}` exactly."
    asks += _revision_asks(prior_sections)
    acceptance = _design_acceptance(target_pages, paths, kind, bucket)
    criteria = Criteria(must=[goal], tone="", audience="")
    if len(consensus) < 2:
        return (
            [
                (
                    StepId(uuid.uuid4()),
                    _outline_spec(goal, asks, acceptance, shown, marker=marker, role="outline"),
                )
            ],
            [],
            criteria,
        )
    proposals = [
        (
            StepId(uuid.uuid4()),
            _outline_spec(
                goal,
                f"You are ONE of {len(consensus)} designers proposing INDEPENDENTLY; a merge reconciles the "
                f"proposals, so commit to your own best structure. {asks}",
                acceptance,
                shown,
                marker=marker,
                role="outline_draft",
            ).model_copy(
                update={"model_ref": model_ref, "title": f"Outline proposal - {family} ({marker})"[:120]}
            ),
        )
        for family, model_ref in consensus
    ]
    merge = _outline_spec(
        goal,
        f"MERGE the mounted outline proposals (proposal-*.md at /task/inputs/) into THE outline. Where they "
        "agree, keep it; where they disagree, CHOOSE, and record each material divergence and your ruling in "
        f"`divergence_note`, one line each. Take the best sections, fences and bible lines from any proposal. {asks}",
        [
            "every material divergence between the proposals is named in divergence_note with its ruling",
            *acceptance,
        ],
        shown,
        marker=marker,
        role="outline",
    ).model_copy(update={"join": "consensus", "title": f"Merge {len(consensus)} outline proposals"[:120]})
    merge_id = StepId(uuid.uuid4())
    edges = [PlanEdge(from_step=pid, to_step=merge_id, kind="produces_for") for pid, _ in proposals]
    return [*proposals, (merge_id, merge)], edges, criteria


# ── the expansion: the parts in order, then assembly ─────────────────────────────────────────

_PART_SHAPES: Final = {
    ArtifactType.DECK: (
        "This part is a SLIDE-GROUP. One `## Slide: <title>` heading per slide, dash bullets (one idea per slide), "
        "a `Notes:` paragraph per slide with what a presenter would SAY, and each slide's `Figure (fig-…): caption` "
        "marker inside its own slide block. The word target covers bullets and notes together."
    ),
    ArtifactType.SPREADSHEET: (
        "This part is ONE SHEET. Pipe tables with a header row and data rows at the brief's grain, and a "
        "`Derivation:` bullet for every derived column naming its arithmetic in words; invent no figures."
    ),
}


def _document_goal(outline: OutlineResult, goal: str) -> str:
    """The goal every part shares, byte for byte: the cached head of each part's prompt."""
    return (
        f'Write "{outline.title}" part by part: each step writes one part, in order, and the system joins them '
        "into one document. Write only the part your assignment names.\n\n"
        f"The person asked: {goal}\n\nThe outline every part follows (its continuity bible outranks your own "
        f"preferences):\n\n{render_outline_md(outline)}"
    )


def _assignment(outline: OutlineResult, index: int, kind: ArtifactType, prior_sections: Sequence[str]) -> str:
    """One part's own assignment: the tail of its prompt, after the shared head."""
    section = outline.sections[index]
    lines = [
        f'## Your part: {index + 1:02d} of {len(outline.sections):02d}, "{section.title}"',
        "",
        f"Brief: {section.brief}",
    ]
    if section.not_here:
        lines.append(f"Out of scope (another part owns it, never restate it): {section.not_here}")
    if section.kept_from:
        lines.append(
            f"Kept from last turn: {section.kept_from} is published as this part, unchanged, by the system. Read "
            "it, submit `KEPT` as the content, and summarize the file."
        )
    lines += [
        f"Revise from {path}: copy what your brief keeps word for word; never paraphrase kept text."
        for path in prior_sections
        if path != section.kept_from and PurePosixPath(path).name in section.brief
    ]
    if kind in _PART_SHAPES:
        lines.append(_PART_SHAPES[kind])
    lines.append(
        f"Target {section.target_words} words, a budget and not a floor: past {section.target_words * 115 // 100} "
        "words, cut before you close."
    )
    lines += [
        f"Place the caption line `Figure ({fig.id}): {fig.caption_hint or fig.spec}` where it serves the text; "
        f"the image is generated for you and depicts: {fig.spec}"
        for fig in section.figures
    ]
    lines.append(
        "This is the closing part: conclude the document." if index + 1 == len(outline.sections) else ""
    )
    return "\n".join(lines).rstrip()


def part_specs(
    outline: OutlineResult,
    *,
    goal: str,
    source_paths: Sequence[str],
    kind: ArtifactType,
    prior_sections: Sequence[str] = (),
) -> list[tuple[StepId, StepSpec]]:
    """One writer step per section, in order. Every part's goal, acceptance and inputs are the same bytes;
    its assignment rides `contract.notes`. Titles are `NN <title>`, so the parts mount in document order."""
    shared = _document_goal(outline, goal)
    acceptance = [
        "the part covers every point its brief names, and nothing another part owns",
        "it continues from the parts before it without restating them; its seams are invisible",
        "terminology, voice and tense follow the continuity bible exactly",
        "its length stays near its word target and never runs 40% past it",
        *input_clause(source_paths),
    ]
    specs: list[tuple[StepId, StepSpec]] = []
    for index, section in enumerate(outline.sections):
        number = f"{index + 1:02d}"
        specs.append(
            (
                StepId(uuid.uuid4()),
                StepSpec(
                    kind=StepKind.STEP,
                    agent=_WRITER,
                    lane_role="section",
                    artifact_role="working",
                    join="quorum",  # a lost part settles its successor's join: the next part still runs
                    title=f"{number} {section.title}"[:120],
                    kept_from=section.kept_from or None,
                    contract=Contract(
                        goal=shared,
                        acceptance=acceptance,
                        inputs=[*source_paths, *prior_sections],
                        output=OutputRef(type=ArtifactType.DOCUMENT, title=f"{number} {section.title}"[:80]),
                        notes=_assignment(outline, index, kind, prior_sections),
                    ),
                ),
            )
        )
    return specs


def _build_note(outline: OutlineResult, kind: ArtifactType, extension: str) -> str:
    """The assemble walker's one command for this format, or empty for markdown."""
    lanes = {
        ArtifactType.DECK: ("md_to_pptx.py", " --images /work/images"),
        ArtifactType.SPREADSHEET: ("md_to_xlsx.py", ""),
    }
    walker, flags = lanes.get(kind, ("", ""))
    if not walker and extension in ("docx", "pdf"):
        walker = f"md_to_{extension}.py"
        flags = " --toc --images /work/images" + (" --engine soffice" if extension == "pdf" else "")
    if not walker:
        walker, flags = "concat_md.py", " --toc"
    return (
        f"\n\nBuild with the assemble skill's walker: `python3 /skills/assemble/scripts/{walker} --sections "
        f"/task/inputs --out /work/{outline.filename}.{extension} --title {shlex.quote(outline.title[:60])}"
        f"{flags}`, then patch only what it cannot know (workbooks: a REAL formula for every column a "
        "`Derivation:` line names), render the pages (`python3 -m plane_house.render <file> --pages /work/pages`), "
        "READ two or three of them, fix what you see, and publish."
    )


def assembly_spec(
    outline: OutlineResult,
    *,
    source_paths: Sequence[str],
    goal: str,
    kind: ArtifactType,
    kept_figures: Sequence[str] = (),
) -> StepSpec:
    """The maker's join: the parts, already rendered under one citation numbering, built into the asked format."""
    extension = {ArtifactType.DECK: "pptx", ArtifactType.SPREADSHEET: "xlsx"}.get(kind, outline.format_target)
    figures = (
        [] if kind is ArtifactType.SPREADSHEET else [(s, fig) for s in outline.sections for fig in s.figures]
    )
    figure_brief = "".join(
        f"\n- `{fig.id}` (section {s.id}): {fig.spec} - caption: {fig.caption_hint or fig.spec}"
        for s, fig in figures
    ) + "".join(
        f"\n- `{PurePosixPath(path).stem}`: a kept section's, bought for the last document"
        for path in kept_figures
    )
    figures_note = (
        "Figures are ALREADY GENERATED at /task/inputs/fig-*.png: copy them into /work/images/ and embed each "
        f"where its caption line sits:{figure_brief}\nOnly a listed figure MISSING from /task/inputs/ is generated "
        "with generate_image; if that fails, keep its caption line as a placeholder."
        if figure_brief
        else "This document has no generated figures."
    )
    total = len(outline.sections)
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("maker"),
        lane_role="assembly",
        artifact_role="deliverable",
        qa=True,  # the pre-delivery editorial gate; the receipt still signs after delivery
        tolerates_missing_inputs=True,  # a lost part costs a visible gap, never the document
        join="quorum",
        join_quorum=ASSEMBLY_QUORUM,
        section_total=total,
        title=f"Assemble - {outline.title}"[:120],
        contract=Contract(
            goal=(
                f'Assemble "{outline.title}" as {outline.filename}.{extension} from the finished parts mounted at '
                f"/task/inputs/ (01-*.md to {total:02d}-*.md, in order; their citations already rendered, with "
                f"the list in {SOURCES_FILENAME}) and the outline. NEVER retype a part: the walker reads the "
                "mounted files, so content flows disk to disk.\n\n"
                f"Front matter: {outline.front_matter or 'a title page and a table of contents'}\n"
                f"Back matter: {outline.back_matter or 'none beyond the final part'}, then {SOURCES_FILENAME} "
                f"when it is mounted.\n\n{figures_note}\n\nIf a part is MISSING, insert a visible gap note "
                "(\"[Part N, '<title>', could not be produced]\") where it belongs; never renumber around it."
                + _build_note(outline, kind, extension)
                + "\n\nEnd your reply with the note the requester reads: the document's title and two or three "
                "sentences of its substance, then ONE line on the build."
            ),
            acceptance=[
                f"the assembled document is the deliverable the task asked for: {goal[:180]}",
                "every part present at /task/inputs/ appears exactly once, complete and in order, read from disk",
                "any missing part is marked by a visible gap note naming it",
                (
                    "slides use real title/body layouts and every slide carries speaker notes"
                    if kind is ArtifactType.DECK
                    else "every derived column a Derivation line names carries a REAL formula, and totals cross-foot"
                    if kind is ArtifactType.SPREADSHEET
                    else "headings use real heading styles; the table of contents matches them"
                ),
                *(
                    [
                        "every figure is embedded at its caption, or its caption stands as a visible placeholder"
                    ]
                    if figures
                    else []
                ),
                *input_clause(source_paths),
            ],
            inputs=[*source_paths, *kept_figures],
            output=OutputRef(type=kind, title=outline.title[:80]),
        ),
    )


def settle_kept(outline: OutlineResult) -> OutlineResult:
    """A kept section buys no figure: its markers point at the figures the last document already has."""
    if not any(section.kept_from and section.figures for section in outline.sections):
        return outline
    sections = [s.model_copy(update={"figures": []}) if s.kept_from else s for s in outline.sections]
    return outline.model_copy(update={"sections": sections})


def _kept_figures(outline: OutlineResult, prior_figures: Sequence[str]) -> list[str]:
    """The last document's figures assembly still mounts: a kept section's, unless this outline buys it again."""
    if not any(section.kept_from for section in outline.sections):
        return []
    bought = {fig.id for section in outline.sections for fig in section.figures}
    return [path for path in prior_figures if PurePosixPath(path).stem not in bought]


def _check(
    outline: OutlineResult,
    *,
    kind: ArtifactType,
    bucket: SizeBucket,
    target_units: int | None,
    prior_sections: Sequence[str],
) -> None:
    """Raise `InvalidOutline` naming the fix for an outline this size cannot hold."""
    ids = [s.id for s in outline.sections]
    if len(set(ids)) != len(ids):
        raise InvalidOutline("two sections share an id - renumber them s01, s02, … uniquely")
    floor, _lo, hi, _target = sizing.section_bounds(kind, bucket, target_words(kind, bucket, target_units))
    if len(outline.sections) > min(LONGFORM_MAX_SECTIONS, hi):
        raise InvalidOutline(
            f"{len(outline.sections)} sections is more than this size allows ({bucket.value}: at most "
            f"{min(LONGFORM_MAX_SECTIONS, hi)}) - merge subsections until the outline fits, never ask to grow it"
        )
    words = sum(s.target_words for s in outline.sections)
    if words > LONGFORM_MAX_WORDS:
        raise InvalidOutline(
            f"the sections target {words:,} words; a document written in parts holds at most "
            f"{LONGFORM_MAX_WORDS:,} - cut the word targets until they fit"
        )
    for section in outline.sections:
        if section.kept_from and section.kept_from not in prior_sections:
            raise InvalidOutline(
                f"section {section.id} sets kept_from to {section.kept_from}, which is not one of the last "
                f"document's sections ({', '.join(prior_sections) or 'there are none'}) - name one, or leave it empty"
            )
        if not floor <= section.target_words <= SECTION_WORDS_MAX:
            raise InvalidOutline(
                f"section {section.id} targets {section.target_words} words - at this size ({bucket.value}) keep "
                f"every section between {floor} and {SECTION_WORDS_MAX} (split or merge instead)"
            )


def expand(
    outline: OutlineResult,
    *,
    source_paths: Sequence[str],
    goal: str,
    outline_step: StepId,
    kind: ArtifactType = ArtifactType.DOCUMENT,
    bucket: SizeBucket = SizeBucket.LONG,
    target_units: int | None = None,
    prior_sections: Sequence[str] = (),
    prior_figures: Sequence[str] = (),
) -> tuple[list[tuple[StepId, StepSpec]], list[PlanEdge]]:
    """The outline as its parts, chained in order, and one assembly, as pure values; raises `InvalidOutline`.
    The floor reads the task's target, never the outline's own sum, so a retry never chases a moving floor."""
    _check(outline, kind=kind, bucket=bucket, target_units=target_units, prior_sections=prior_sections)
    parts = part_specs(
        outline, goal=goal, source_paths=source_paths, kind=kind, prior_sections=prior_sections
    )
    assembly_id = StepId(uuid.uuid4())
    kept = _kept_figures(outline, prior_figures)
    assembly = assembly_spec(outline, source_paths=source_paths, goal=goal, kind=kind, kept_figures=kept)
    ids = [outline_step, *(sid for sid, _ in parts)]
    edges = [
        *(PlanEdge(from_step=before, to_step=after, kind="depends_on") for before, after in pairwise(ids)),
        *(PlanEdge(from_step=sid, to_step=assembly_id, kind="produces_for") for sid, _ in parts),
        PlanEdge(
            from_step=outline_step, to_step=assembly_id, kind="produces_for"
        ),  # the outline and its figures
    ]
    return [*parts, (assembly_id, assembly)], edges


async def expand_after_outline(
    session: AsyncSession, task: Task, outline_step: PlanStep, outline: OutlineResult
) -> int:
    """The outline as its parts and assembly in the caller's transaction, the first part READY; raises
    `InvalidOutline` for the outline step's ladder. Commits nothing."""
    dialed = dialed_format(
        task.deliverable_format,
        outline.format_target,
        is_document=task.deliverable == ArtifactType.DOCUMENT.value,
    )
    if dialed is not None:  # the dial outranks the model
        logfire.info(
            "longform: outline format {got} overridden by the dial {want}",
            got=outline.format_target,
            want=dialed,
        )
        outline = outline.model_copy(update={"format_target": dialed})
    outline = settle_kept(outline)
    from prismoid.operations.execution import mounts  # the engine imports this module: read at call time

    source_paths = input_paths(TaskInput.model_validate(i).filename for i in task.inputs)
    shown = StepSpec.model_validate(outline_step.spec).contract
    figures: Sequence[Artifact] = (
        await mounts.prior_figures(session, task) if any(s.kept_from for s in outline.sections) else ()
    )
    steps, edges = expand(
        outline,
        source_paths=list(source_paths),
        goal=task.goal,
        outline_step=StepId(outline_step.id),
        kind=ArtifactType(task.deliverable) if task.deliverable else ArtifactType.DOCUMENT,
        bucket=sizing.normalize(task.scale) or SizeBucket.LONG,
        target_units=task.target_pages,
        prior_sections=[p for p in (shown.inputs if shown else []) if p not in source_paths],
        prior_figures=input_paths(mounts.upstream_filename(a) for a in figures),
    )
    from prismoid.operations.planning_steps import hand_downstream, write_steps  # the one persist path

    existing = await PlanStep.for_task(session, task.id)
    rows = await write_steps(
        session, task, steps, edges, base_ordinal=max((s.ordinal for s in existing), default=-1) + 1
    )
    await hand_downstream(session, task.id, outline_step.id, rows)
    rows[0].state = StepState.READY.value  # the first part's only upstream is the outline completing now
    pins = await _resolve_pins(session, assembly_skill_refs(outline.format_target), task)
    if pins:
        assembly = rows[-1]
        spec = StepSpec.model_validate(assembly.spec)
        assembly.spec = spec.model_copy(update={"skill_pins": [*spec.skill_pins, *pins]}).model_dump(
            mode="json"
        )
    await _record_expansion(session, task, outline)
    return len(steps)


def dialed_format(deliverable_format: str | None, format_target: str, *, is_document: bool) -> str | None:
    """The task's stamped document format when it must override the outline's, else None."""
    if deliverable_format is None or not is_document or deliverable_format not in ("md", "docx", "pdf"):
        return None
    return deliverable_format if format_target != deliverable_format else None


def assembly_skill_refs(format_target: str) -> list[str]:
    """The skills assembly asks for: the assemble walkers, then the format's own skill."""
    format_skill = {"docx": "docx", "pdf": "pdf", "pptx": "deck", "xlsx": "model"}
    return ["assemble"] + ([format_skill[format_target]] if format_target in format_skill else [])


async def _resolve_pins(session: AsyncSession, refs: list[str], task: Task) -> list[SkillPin]:
    """Each ref resolved for the task's creator; fail-soft: a ref that cannot be pinned costs the pin."""
    from prismoid.operations import skills as skill_ops

    pins: list[SkillPin] = []
    for ref in refs:
        try:
            pins.append(await skill_ops.resolve_ref(session, ref, actor_id=task.creator_actor_id))
        except Exception:
            logfire.warn("longform: skill {ref} not pinnable; assembly runs without it", ref=ref)
    return pins


async def _record_expansion(session: AsyncSession, task: Task, outline: OutlineResult) -> None:
    """The wider plan on the record: the `plan_edits` row, `plan.edited`, then a fresh `task.planned`."""
    from prismoid.operations.planning_steps import plan_view

    now = utcnow()
    project = ProjectId(task.project_id) if task.project_id else None
    reason = f"outline expanded: {len(outline.sections)} parts"
    session.add(
        PlanEditRow(
            task_id=task.id,
            kind=PlanEditKind.EXPAND.value,
            reason=reason,
            payload=outline.model_dump(mode="json"),
        )
    )
    figures = sum(len(s.figures) for s in outline.sections)
    edit = PlanEditView(
        kind=PlanEditKind.EXPAND,
        reason=f"Outline ready - {len(outline.sections)} parts, {figures} figures",
        at=now,
    )
    await TaskEvent.append(
        session,
        PlanEdited(
            seq=await task.next_seq(session), task_id=TaskId(task.id), project_id=project, at=now, edit=edit
        ),
    )
    view = await plan_view(session, task)
    await TaskEvent.append(
        session,
        TaskPlanned(
            seq=await task.next_seq(session),
            task_id=TaskId(task.id),
            project_id=project,
            at=utcnow(),
            plan=view,
        ),
    )


# ── the run: a part's tail, assembly's guard and its one citation render ─────────────────────


async def part_tail(session: AsyncSession, task: Task, step: PlanStep, assignment: str) -> str:
    """A part's own prompt tail: its assignment, then what each part before it in the same document (the parts
    sharing its head) established, from their summaries."""
    mine = StepSpec.model_validate(step.spec).contract
    steps = [s for s in await PlanStep.for_task(session, task.id) if s.ordinal < step.ordinal]
    earlier = sorted(
        (s for s in steps if _part_of(StepSpec.model_validate(s.spec), mine)), key=lambda s: s.ordinal
    )
    if not earlier:
        return f"\n\n{assignment}\n\nNo part is written yet: yours opens the document."
    told: dict[object, str] = {}
    for said in await TaskEvent.of_types(session, task.id, ["step.narration"]):
        if isinstance(said, StepNarration) and said.step_id is not None:
            told[said.step_id] = said.markdown
    lines = [
        f"- {s.title}: {told[s.id]}"
        if s.step_state is StepState.DONE and s.id in told
        else f"- {s.title}: not written (a gap note stands here)"
        for s in earlier
    ]
    return f"\n\n{assignment}\n\n## Written so far\n\n" + "\n".join(lines)


def _part_of(spec: StepSpec, mine: Contract | None) -> bool:
    """Whether `spec` is a part of the document whose shared contract is `mine`."""
    return (
        spec.lane_role == "section"
        and spec.contract is not None
        and mine is not None
        and spec.contract.goal == mine.goal
    )


async def assembly_ready_guard(session: AsyncSession, task: Task, step: PlanStep) -> None:
    """The floor: below `join_quorum` of the parts feeding THIS assembly (never an earlier turn's), assembly
    refuses honestly instead of shipping a hollow book."""
    spec = StepSpec.model_validate(step.spec)
    if spec.lane_role != "assembly" or not spec.section_total:
        return
    floor = spec.join_quorum or ASSEMBLY_QUORUM
    fed = {e.from_step_id for e in await PlanEdgeRow.for_task(session, task.id) if e.to_step_id == step.id}
    done = sum(
        1
        for s in await PlanStep.for_task(session, task.id)
        if s.id in fed
        and StepSpec.model_validate(s.spec).lane_role == "section"
        and s.step_state is StepState.DONE
    )
    if done < spec.section_total * floor:
        raise PolicyDenied(
            f"only {done} of {spec.section_total} parts survived - below the {floor:.0%} floor, the assembled "
            "document would not be the deliverable that was asked for. Retry the task or narrow the ask."
        )


async def render_parts_mounted(
    frame: FrameDriver, spec: StepSpec, inputs: Sequence[str], sources: dict[str, str]
) -> tuple[str, ...]:
    """Assembly's mounted parts with their `[S#]` marks linked under ONE numbering, its list as `sources.md`
    after the last part. Fail-soft: an unknown id leaves the marks as written."""
    named = set(spec.contract.inputs) if spec.contract is not None else set()
    parts = sorted(
        p for p in inputs if p not in named and PurePosixPath(p).name[:2].isdigit() and p.endswith(".md")
    )
    texts = [(await frame.read_bytes(p)).decode() for p in parts]
    try:
        bodies, listed = render_parts(texts, sources)
    except UnknownSource:
        logfire.warn("longform: a part cites an id this task never read; its marks stay as written")
        return tuple(inputs)
    for path, body, text in zip(parts, bodies, texts, strict=True):
        if body != text:
            await frame.write_bytes(path, body.encode(), mode=0o444, owner="system")
    if not listed or not parts:
        return tuple(inputs)
    path = str(
        PurePosixPath(parts[-1]).parent / f"{(spec.section_total or len(parts)) + 1:02d}-{SOURCES_FILENAME}"
    )
    await frame.write_bytes(path, listed.encode(), mode=0o444, owner="system")
    return (*inputs, path)
