# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The five size buckets (ADR-029) - one vocabulary for every artifact kind.

**The table below is the single source of truth for size.** The door's Literal, the planner's
acceptance bands, the long-form lane's section math, the QA gate's tolerance, the fleet
guard, the CLI's help text and the web dial all read THIS - size appears in code nowhere
else, so the five surfaces can never disagree about what "extended" means.

The user principle the table encodes (the no-push law): the system NEVER pushes
size upward. A bucket is a target range **and a hard ceiling** - "how far it may push up the
notch" is the cap column, not a vibe. Resolution from a goal happens only on literal numbers
("15 pages", "25 slides"); "comprehensive" is not a size.

Wire values are the bucket names themselves - all five fit the existing `tasks.scale`
String(8) column (a user decision that made this feature migration-free), and the legacy
stored value `"standard"` simply IS the standard bucket. `"long"` carries over; `"auto"`
remains the unresolved sentinel the resolver replaces at first planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from prismoid.schemas.outputs import ArtifactType


class SizeBucket(StrEnum):
    BRIEF = "brief"
    STANDARD = "standard"  # the default - today's behavior, named
    EXTENDED = "extended"
    LONG = "long"
    BOOK = "book"


# Wire/stored values the door accepts beyond the five buckets: "auto" resolves at planning;
# "standard" and "long" are ALSO legacy rows/clients and map onto their buckets verbatim.
AUTO: Final = "auto"

WORDS_PER_PAGE: Final = 300  # prose pages - re-exported by schemas.longform for compat
# The long-form recipe's two lines (ADR-025): up to ONE_RUN_MAX_WORDS a deliverable is one run's output; past it,
# a typed outline then its parts in order, never past LONGFORM_MAX_WORDS (the book ceiling below holds it).
ONE_RUN_MAX_WORDS: Final = 6_000
LONGFORM_MAX_WORDS: Final = 30_000


@dataclass(frozen=True, slots=True)
class SizeSpec:
    """One (kind, bucket) cell: what `target_*`/`cap` count, the long-form outline bounds (None is
    one-shot), the size check's band and the honest `shape` sentence the acceptance carries."""

    unit: str  # "pages" | "slides" | "sheets" | "rows" | "scope"
    what: str  # what this size IS, in product words
    target_lo: int
    target_hi: int
    cap: int  # the hard ceiling - the notch it may NEVER push past
    lane: str  # "one_shot" | "longform"
    words_per_unit: int
    qa_tolerance: float
    sections_lo: int | None = None
    sections_hi: int | None = None
    section_target_words: int | None = None
    shape: str = ""  # indicative structure (sheets: rows/features; decks: notes law)


_B = SizeBucket

DOCUMENT_SIZES: Final[dict[SizeBucket, SizeSpec]] = {
    _B.BRIEF: SizeSpec("pages", "a memo, an answer, a one-pager", 1, 2, 4, "one_shot", WORDS_PER_PAGE, 0.5),
    _B.STANDARD: SizeSpec("pages", "a normal document", 3, 8, 12, "one_shot", WORDS_PER_PAGE, 0.4),
    _B.EXTENDED: SizeSpec(
        "pages",
        "a substantial report",
        10,
        25,
        30,
        "longform",
        WORDS_PER_PAGE,
        0.3,
        sections_lo=3,
        sections_hi=8,
        section_target_words=1100,
    ),
    _B.LONG: SizeSpec(
        "pages",
        "a deep report",
        30,
        60,
        80,
        "longform",
        WORDS_PER_PAGE,
        0.25,
        sections_lo=6,
        sections_hi=16,
        section_target_words=1900,
    ),
    _B.BOOK: SizeSpec(
        "pages",
        "a monograph",
        70,
        95,
        100,  # LONGFORM_MAX_WORDS / WORDS_PER_PAGE
        "longform",
        WORDS_PER_PAGE,
        0.25,
        sections_lo=12,
        sections_hi=24,
        section_target_words=2000,
    ),
}

DECK_SIZES: Final[dict[SizeBucket, SizeSpec]] = {
    _B.BRIEF: SizeSpec("slides", "a lightning talk / exec summary", 5, 8, 10, "one_shot", 130, 0.5),
    _B.STANDARD: SizeSpec("slides", "a normal talk deck", 10, 16, 20, "one_shot", 130, 0.4),
    _B.EXTENDED: SizeSpec(
        "slides",
        "a full seminar",
        18,
        30,
        36,
        "longform",
        130,
        0.3,
        sections_lo=3,
        sections_hi=7,
        section_target_words=780,  # a 4-8 slide group, notes included
        shape="every slide carries speaker notes; one idea per slide",
    ),
    _B.LONG: SizeSpec(
        "slides",
        "a workshop / training session",
        32,
        50,
        60,
        "longform",
        130,
        0.25,
        sections_lo=6,
        sections_hi=12,
        section_target_words=780,
        shape="every slide carries speaker notes; one idea per slide",
    ),
    _B.BOOK: SizeSpec(
        "slides",
        "a course / multi-hour curriculum deck",
        55,
        80,
        100,
        "longform",
        130,
        0.25,
        sections_lo=10,
        sections_hi=20,
        section_target_words=780,
        shape="every slide carries speaker notes; one idea per slide",
    ),
}

SPREADSHEET_SIZES: Final[dict[SizeBucket, SizeSpec]] = {
    _B.BRIEF: SizeSpec(
        "sheets",
        "one quick table",
        1,
        1,
        2,
        "one_shot",
        600,
        0.5,
        shape="≤ ~50 rows: headers, data, a totals row",
    ),
    _B.STANDARD: SizeSpec(
        "sheets",
        "a working workbook",
        2,
        4,
        5,
        "one_shot",
        600,
        0.4,
        shape="~50–300 rows across sheets; formulas and totals; one summary sheet",
    ),
    _B.EXTENDED: SizeSpec(
        "sheets",
        "a real model",
        4,
        8,
        10,
        "longform",
        600,
        0.3,
        sections_lo=4,
        sections_hi=8,
        section_target_words=600,  # one section = one sheet spec
        shape="~300–1,500 rows; cross-sheet formulas; an assumptions sheet and a charts/dashboard sheet",
    ),
    _B.LONG: SizeSpec(
        "sheets",
        "a scenario model",
        8,
        12,
        14,
        "longform",
        600,
        0.25,
        sections_lo=8,
        sections_hi=12,
        section_target_words=600,
        shape="~1,500–5,000 rows; scenario sheets; a sensitivity table; a data dictionary",
    ),
    _B.BOOK: SizeSpec(
        "sheets",
        "a full financial/operations model",
        12,
        20,
        24,
        "longform",
        600,
        0.25,
        sections_lo=12,
        sections_hi=20,
        section_target_words=600,
        shape="a multi-module workbook: inputs / engine / outputs / scenarios / data dictionary",
    ),
}

COLLECTION_SIZES: Final[dict[SizeBucket, SizeSpec]] = {
    # Always one_shot: a collection is a single typed call. long/book carry HONEST bands the
    # judge enforces (no forbidden door; the ceiling is stated in the size ADR).
    _B.BRIEF: SizeSpec("rows", "a short list", 5, 12, 20, "one_shot", 40, 0.5),
    _B.STANDARD: SizeSpec("rows", "a project-scale collection", 10, 30, 45, "one_shot", 40, 0.4),
    _B.EXTENDED: SizeSpec("rows", "a wide collection", 30, 80, 120, "one_shot", 40, 0.3),
    _B.LONG: SizeSpec("rows", "a catalog", 80, 150, 220, "one_shot", 40, 0.25),
    _B.BOOK: SizeSpec("rows", "an inventory", 150, 300, 400, "one_shot", 40, 0.25),
}

# Code buckets are ADVISORY scope language only - never enforced counts. The spec
# rows exist so every kind has all five cells; `unit="scope"` marks them non-enforcing.
CODE_SIZES: Final[dict[SizeBucket, SizeSpec]] = {
    _B.BRIEF: SizeSpec("scope", "one focused change or fix", 1, 1, 1, "one_shot", 0, 1.0),
    _B.STANDARD: SizeSpec("scope", "a module and its tests", 1, 1, 1, "one_shot", 0, 1.0),
    _B.EXTENDED: SizeSpec("scope", "a multi-module feature with tests and docs", 1, 1, 1, "one_shot", 0, 1.0),
    _B.LONG: SizeSpec("scope", "several components working together", 1, 1, 1, "one_shot", 0, 1.0),
    _B.BOOK: SizeSpec("scope", "a full project scaffold", 1, 1, 1, "one_shot", 0, 1.0),
}

SIZING: Final[dict[ArtifactType, dict[SizeBucket, SizeSpec]]] = {
    ArtifactType.DOCUMENT: DOCUMENT_SIZES,
    ArtifactType.DECK: DECK_SIZES,
    ArtifactType.SPREADSHEET: SPREADSHEET_SIZES,
    ArtifactType.COLLECTION: COLLECTION_SIZES,
    ArtifactType.CODE: CODE_SIZES,
}


@dataclass(frozen=True, slots=True)
class WebEffort:
    """How much of the web one step may read for a deliverable of this size: its `web_search` and
    `web_fetch` ceilings per attempt. They only ever lower the deployment's own caps
    (`PRISMOID_SEARCH_MAX_PER_STEP`, `PRISMOID_WORKER_FETCH_MAX_PER_STEP`)."""

    searches: int
    fetches: int


def lowered(cap: int, effort: int | None) -> int:
    """A deployment cap under a step's web effort: the effort only ever lowers it, never raises it."""
    return cap if effort is None else min(cap, effort)


# Research depth by size, every kind alike: a brief deliverable (a memo, a note) reads a few sources
# well; every other size keeps the deployment's caps: a 200-word memo at full reach ran 8 searches
# and 15 fetches in 348 s; at this depth it takes about 122 s and reads as well. Standard - the size most
# steps wear - is not cut: no eval has yet shown a smaller reach costs its answers nothing.
WEB_EFFORT: Final[dict[SizeBucket, WebEffort]] = {
    _B.BRIEF: WebEffort(searches=3, fetches=4),
}


def web_effort_for(scale: str | None) -> WebEffort | None:
    """The task's stamped size → its steps' web effort, or None for the deployment's caps (any
    size but brief, or a size not resolved yet)."""
    bucket = normalize(scale)
    return None if bucket is None else WEB_EFFORT.get(bucket)


# ── what a wire value means, and the one marker every surface prints ─────────────────────────
# These live HERE, beside the table they read: the API serializers echo the marker too, and the
# planning passes, the long-form lane, the QA gate and the cost gate decide from the same cells.

# Legacy wire/stored values → buckets. "standard" IS the standard bucket (no translation);
# "long" predates the vocabulary and keeps meaning long. "auto" stays the unresolved sentinel.
LEGACY_SCALE: Final[dict[str, SizeBucket]] = {
    "standard": SizeBucket.STANDARD,
    "long": SizeBucket.LONG,
}


def normalize(scale: str | None) -> SizeBucket | None:
    """Wire/stored `scale` → a bucket, or None for auto/unknown (the resolver's business)."""
    if scale is None or scale == AUTO:
        return None
    if scale in LEGACY_SCALE:
        return LEGACY_SCALE[scale]
    try:
        return SizeBucket(scale)
    except ValueError:
        return None  # an unknown stored value reads as auto rather than crashing a list view


def spec_for(kind: ArtifactType | None, bucket: SizeBucket) -> SizeSpec:
    """The cell. An unmapped kind (image, page, …) and None both read as DOCUMENT - prose
    semantics are the honest default for anything without its own column."""
    table = SIZING.get(kind or ArtifactType.DOCUMENT) or SIZING[ArtifactType.DOCUMENT]
    return table[bucket]


def size_marker(kind: ArtifactType | None, bucket: SizeBucket, units: int | None) -> str:
    """The ONE string every surface stamps - plan titles, prompts, CLI, the web chip, and the
    API's `size_marker` echo. "extended · ~15 pages"; unit-less kinds (code) show the scope
    sentence instead of a count."""
    spec = spec_for(kind, bucket)
    if spec.unit == "scope":
        return f"{bucket.value} · {spec.what}"
    shown = units if units is not None else (spec.target_lo + spec.target_hi) // 2
    return f"{bucket.value} · ~{shown} {spec.unit}"


# ── the size law's decisions: rows in (a dial, a count), never the request's words ───────────

BUCKET_ORDER: Final = (
    SizeBucket.BRIEF,
    SizeBucket.STANDARD,
    SizeBucket.EXTENDED,
    SizeBucket.LONG,
    SizeBucket.BOOK,
)


def bucket_for_units(kind: ArtifactType | None, units: int) -> SizeBucket:
    """The smallest bucket whose CAP covers the asked number; past book's cap it is book
    (and the number clamps to book's cap - the ceiling is the ceiling)."""
    for bucket in BUCKET_ORDER:
        if units <= spec_for(kind, bucket).cap:
            return bucket
    return SizeBucket.BOOK


def resolve(scale: str | None, kind: ArtifactType | None, units: int | None) -> tuple[SizeBucket, int | None]:
    """THE resolution law over the dials: an explicit bucket > an explicit count > `standard`. The count
    clamps DOWN to the bucket's cap, never up (the no-push law)."""
    chosen = normalize(scale)
    if chosen is None:
        chosen = bucket_for_units(kind, units) if units is not None else SizeBucket.STANDARD
    cap = spec_for(kind, chosen).cap
    return chosen, (min(units, cap) if units is not None else None)


def runs_longform(kind: ArtifactType | None, bucket: SizeBucket, units: int | None = None) -> bool:
    """Does this ask run the long-form lane (outline, parts in order, assembly, QA)? Only past one run's
    output: its words, the asked units or the bucket's midband, exceed `ONE_RUN_MAX_WORDS`."""
    spec = spec_for(kind, bucket)
    target = units if units is not None else (spec.target_lo + spec.target_hi) // 2
    return spec.lane == "longform" and target * spec.words_per_unit > ONE_RUN_MAX_WORDS


def fleet_allows(bucket: SizeBucket) -> bool:
    """Fleet answers are single-breath (a seat is one model call plus its verify rung) -
    brief and standard only, by user decision."""
    return bucket in (SizeBucket.BRIEF, SizeBucket.STANDARD)


def section_bounds(
    kind: ArtifactType | None, bucket: SizeBucket, total_words: int
) -> tuple[int, int, int, int]:
    """(floor_words, sections_lo, sections_hi, section_target_words) for the outline. The floor SCALES with
    the ask - `max(400, total // sections_hi)` - because a flat 800-word floor was wrong below ~20 pages."""
    spec = spec_for(kind, bucket)
    hi = spec.sections_hi or 1
    lo = spec.sections_lo or 1
    floor = max(400, total_words // max(1, hi))
    return floor, lo, hi, spec.section_target_words or max(floor, total_words // max(1, lo))


def size_clause(kind: ArtifactType | None, bucket: SizeBucket, units: int | None = None) -> list[str]:
    """The acceptance lines for a plan's size (band and ceiling); empty for a non-collection kind at
    `standard` with no explicit units, so a plain ask gets no size lecture."""
    spec = spec_for(kind, bucket)
    if spec.unit == "scope":
        return [f"the scope matches the ask: {spec.what} - no gold-plating beyond it"]
    if kind is ArtifactType.COLLECTION:
        lo, hi = spec.target_lo, spec.target_hi
        # The user's own number rides the acceptance like every other kind's: an explicit "40 rows"
        # sits in the brief itself, not only in the goal text it may paraphrase away.
        band = (
            f"the ask names ~{units} rows - honor it - inside the {lo}–{hi} band ({spec.what})"
            if units is not None
            else f"{lo}–{hi} rows is the band ({spec.what})"
        )
        return [
            f"the collection DECOMPOSES the ask, never echoes it: {band}; fewer than ~{lo} "
            "requires the summary to say why the subject is genuinely narrower - and "
            f"{spec.cap} rows is the hard ceiling",
            "every row is a real, distinct thing - no filler rows to hit a count",
        ]
    if bucket is SizeBucket.STANDARD and units is None:
        return []
    target = units if units is not None else (spec.target_lo + spec.target_hi) // 2
    lines = [
        f"size: {spec.what} - target ~{target} {spec.unit} "
        f"(band {spec.target_lo}–{spec.target_hi}, HARD ceiling {spec.cap}); the target is a "
        f"budget, not a floor - never pad toward it, never run past the ceiling"
    ]
    if spec.shape:
        lines.append(f"shape: {spec.shape}")
    return lines
