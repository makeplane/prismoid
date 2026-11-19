# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The size law (ADR-029), asserted - the table's invariants, the resolution law over the dials, and
above all the NO-PUSH pin: the system never chooses a bigger size than the person dialed."""

from __future__ import annotations

import pytest

from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.sizing import (
    BUCKET_ORDER,
    LONGFORM_MAX_WORDS,
    SIZING,
    SizeBucket,
    bucket_for_units,
    fleet_allows,
    normalize,
    resolve,
    runs_longform,
    section_bounds,
    size_clause,
    size_marker,
    spec_for,
)

KINDS = list(SIZING)
B = SizeBucket


# ── the table's invariants ────────────────────────────────────────────────────────────────────


def test_every_kind_has_all_five_cells() -> None:
    for kind, table in SIZING.items():
        assert set(table) == set(SizeBucket), f"{kind} is missing buckets"


@pytest.mark.parametrize("kind", KINDS)
def test_bands_are_sane_and_monotonic(kind: ArtifactType) -> None:
    prev_cap = 0
    for bucket in BUCKET_ORDER:
        spec = SIZING[kind][bucket]
        assert spec.target_lo <= spec.target_hi <= spec.cap, f"{kind}/{bucket} band inverted"
        if spec.unit != "scope":  # code cells are advisory placeholders
            assert spec.cap >= prev_cap, f"{kind}/{bucket} cap shrank vs the previous bucket"
            prev_cap = spec.cap


def test_book_documents_fit_the_structural_ceiling() -> None:
    # 24 sections × 3,000 words is the outline schema's hard shape; book must fit inside it.
    from prismoid.schemas.longform import LONGFORM_MAX_SECTIONS, SECTION_WORDS_MAX, WORDS_PER_PAGE

    book = SIZING[ArtifactType.DOCUMENT][B.BOOK]
    assert book.cap * WORDS_PER_PAGE <= LONGFORM_MAX_SECTIONS * SECTION_WORDS_MAX


def test_longform_cells_carry_section_bounds() -> None:
    for kind, table in SIZING.items():
        for bucket, spec in table.items():
            if spec.lane == "longform":
                assert spec.sections_lo and spec.sections_hi and spec.section_target_words, (
                    f"{kind}/{bucket} is long-form but has no section bounds"
                )


# ── normalize: legacy values keep meaning what they meant ─────────────────────────────────────


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("auto", None),
        (None, None),
        ("standard", B.STANDARD),  # legacy stored value IS the standard bucket
        ("long", B.LONG),
        ("brief", B.BRIEF),
        ("extended", B.EXTENDED),
        ("book", B.BOOK),
        ("garbage", None),  # an unknown stored value reads as auto, never a crash
    ],
)
def test_normalize(wire: str | None, expected: SizeBucket | None) -> None:
    assert normalize(wire) is expected


# ── the no-push law ───────────────────────────────────────────────────────────────────────────


def test_no_dial_is_standard() -> None:
    assert resolve("auto", ArtifactType.DOCUMENT, None) == (B.STANDARD, None)
    assert resolve(None, ArtifactType.DOCUMENT, None) == (B.STANDARD, None)


def test_a_count_resolves_to_the_smallest_covering_bucket() -> None:
    assert resolve("auto", ArtifactType.DOCUMENT, 2) == (B.BRIEF, 2)
    assert resolve("auto", ArtifactType.DOCUMENT, 15) == (B.EXTENDED, 15)
    assert resolve("auto", ArtifactType.DOCUMENT, 45) == (B.LONG, 45)
    assert resolve(None, ArtifactType.DOCUMENT, 100) == (B.BOOK, 100)


def test_units_clamp_down_never_up() -> None:
    # Past book's cap the number clamps to the ceiling; a chosen bucket clamps its number too.
    bucket, units = resolve("auto", ArtifactType.DOCUMENT, 1000)
    assert bucket is B.BOOK and units == SIZING[ArtifactType.DOCUMENT][B.BOOK].cap
    bucket, units = resolve("brief", ArtifactType.DOCUMENT, 50)
    assert bucket is B.BRIEF and units == SIZING[ArtifactType.DOCUMENT][B.BRIEF].cap


def test_explicit_bucket_beats_the_count() -> None:
    bucket, units = resolve("extended", ArtifactType.DOCUMENT, 25)
    assert bucket is B.EXTENDED and units == 25


# ── lanes, fleet, sections ────────────────────────────────────────────────────────────────────


def test_longform_truth_table() -> None:
    doc = ArtifactType.DOCUMENT
    assert not runs_longform(doc, B.BRIEF) and not runs_longform(doc, B.STANDARD)
    assert not runs_longform(doc, B.EXTENDED), "a 17-page midband is 5,100 words: one run writes it"
    assert not runs_longform(doc, B.EXTENDED, 20), "6,000 words is still one run's output"
    assert runs_longform(doc, B.EXTENDED, 21), "6,300 words is past one run: the parts are written in order"
    assert runs_longform(doc, B.LONG) and runs_longform(doc, B.BOOK)
    assert not runs_longform(ArtifactType.DECK, B.EXTENDED) and runs_longform(ArtifactType.DECK, B.BOOK)
    assert not runs_longform(ArtifactType.SPREADSHEET, B.LONG) and runs_longform(
        ArtifactType.SPREADSHEET, B.BOOK
    )
    assert not runs_longform(ArtifactType.COLLECTION, B.BOOK)  # organizer is one typed call, always
    assert not runs_longform(ArtifactType.CODE, B.BOOK)


def test_the_longest_document_is_written_within_the_longform_ceiling() -> None:
    book = SIZING[ArtifactType.DOCUMENT][B.BOOK]
    assert book.cap * book.words_per_unit == LONGFORM_MAX_WORDS


def test_fleet_allows_single_breath_only() -> None:
    assert fleet_allows(B.BRIEF) and fleet_allows(B.STANDARD)
    assert not fleet_allows(B.EXTENDED) and not fleet_allows(B.LONG) and not fleet_allows(B.BOOK)


def test_section_floor_scales_with_the_ask() -> None:
    # The gap-zone fix: 15pp (4,500 words) extended → floor 562, NOT the old flat 800 that
    # made "7 sections × 800 ≥ 5,600 > 4,500×1.15" an impossible arithmetic the architect
    # could only escape by asking the user (burned live, three times).
    floor, lo, hi, _target = section_bounds(ArtifactType.DOCUMENT, B.EXTENDED, 15 * 300)
    assert floor == max(400, 4500 // hi) < 800
    assert lo >= 3 and hi <= 8
    # And at long scale the floor stays civilised for real outlines:
    floor, _, hi, _ = section_bounds(ArtifactType.DOCUMENT, B.LONG, 45 * 300)
    assert floor == max(400, 13500 // hi)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("bucket", list(SizeBucket))
def test_floor_never_exceeds_schema_ceiling(kind: ArtifactType, bucket: SizeBucket) -> None:
    from prismoid.schemas.longform import SECTION_WORDS_MAX

    spec = spec_for(kind, bucket)
    if spec.lane != "longform":
        return
    words = spec.cap * spec.words_per_unit
    floor, _, _, _ = section_bounds(kind, bucket, words)
    assert floor <= SECTION_WORDS_MAX, f"{kind}/{bucket}: floor {floor} above the 3000 ceiling"


# ── marking + acceptance bands ────────────────────────────────────────────────────────────────


def test_size_marker_reads_like_a_label() -> None:
    assert size_marker(ArtifactType.DOCUMENT, B.EXTENDED, 15) == "extended · ~15 pages"
    assert size_marker(ArtifactType.DECK, B.EXTENDED, 25) == "extended · ~25 slides"
    assert size_marker(ArtifactType.SPREADSHEET, B.LONG, 10) == "long · ~10 sheets"
    assert size_marker(ArtifactType.CODE, B.BRIEF, None).startswith("brief · ")


def test_standard_without_units_carries_no_size_lecture() -> None:
    # Today's behavior, byte-identical: a plain ask gets no band lines (except collections,
    # whose 10–30 language predates buckets and now lives in the table).
    assert size_clause(ArtifactType.DOCUMENT, B.STANDARD) == []
    assert size_clause(ArtifactType.DECK, B.STANDARD) == []
    assert size_clause(ArtifactType.SPREADSHEET, B.STANDARD) == []


def test_collection_band_survives_table_move() -> None:
    lines = size_clause(ArtifactType.COLLECTION, B.STANDARD)
    assert any("10–30 rows" in line for line in lines)
    assert any("hard ceiling" in line for line in lines)


def test_explicit_buckets_carry_band_and_ceiling() -> None:
    lines = size_clause(ArtifactType.DOCUMENT, B.EXTENDED, units=15)
    assert any("~15 pages" in line and "HARD ceiling 30" in line for line in lines)
    lines = size_clause(ArtifactType.SPREADSHEET, B.EXTENDED)
    assert any("shape:" in line for line in lines)  # the workbook's indicative structure


def test_bucket_for_units_edges() -> None:
    doc = ArtifactType.DOCUMENT
    assert bucket_for_units(doc, 4) is B.BRIEF  # at the cap = still brief
    assert bucket_for_units(doc, 5) is B.STANDARD
    assert bucket_for_units(doc, 12) is B.STANDARD
    assert bucket_for_units(doc, 13) is B.EXTENDED
    assert bucket_for_units(doc, 1000) is B.BOOK
