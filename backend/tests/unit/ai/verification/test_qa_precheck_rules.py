# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA pre-check's tables, one input per check and per reader, pinned to the exact rows.

`run` walks its checks in a fixed order over what the one reader (`suites/extract.py`) read; the rows'
order, names and details are what the composer reads, so they are pinned byte-exact here. The
expected values were taken from the pre-check before it became a table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import prismoid.ai.verification.qa_precheck as precheck
from prismoid.ai.verification.suites.extract import Block, Extracted

DANGLE = ("the clause runs on and then breaks " * 8).strip()
OUTLINE = "### 01 · Alpha  (100 words)\n\n### 02 · Beta\n\n### 03 · Gamma  (50 words)\n"
BROKEN = (
    "# Report\n\n# Report\n\n## Beta\n\nBody of beta with fig-1 and [a link]( ) here.\n\n## Alpha\n\n"
    + DANGLE
    + ',\n\nAnd then she said, "We will'
)
LAYOUT_ROW = {"name": "qa:layout:pages", "ok": False, "detail": "x" * 600}


def ok(name: str) -> dict[str, Any]:
    return {"name": name, "ok": True, "detail": ""}


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(precheck, "EXTRACTED", str(tmp_path / "extracted.md"))
    return tmp_path


@pytest.fixture
def layout_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def fake(*args: object) -> list[dict[str, Any]]:
        calls.append(args)
        return [dict(LAYOUT_ROW)]

    monkeypatch.setattr(precheck, "layout_rows", fake)
    return calls


def test_every_text_check_fails_in_order_with_its_exact_detail(work: Path) -> None:
    (work / "outline.md").write_text(OUTLINE)
    (work / "broken.md").write_text(BROKEN)
    checks = precheck.run(
        {
            "deliverable": str(work / "broken.md"),
            "outline": str(work / "outline.md"),
            "target_words": 100,
            "tolerance": 0.3,
            "figure_ids": ["fig-1", "fig-2"],
        }
    )
    assert checks == [
        ok("qa:extraction"),
        {
            "name": "qa:sections",
            "ok": False,
            "detail": "missing from the body (Contents-only or absent): Gamma sections appear out of outline order",
        },
        {"name": "qa:size", "ok": True, "detail": "80 words vs target 100 ±30% (70–130)"},
        {"name": "qa:figures", "ok": False, "detail": "no caption or marker for: fig-2"},
        {
            "name": "qa:truncation",
            "ok": False,
            "detail": "1 paragraph(s) end mid-clause. the document's last line does not close like a "
            "sentence: …'And then she said, \"We will'",
        },
        {"name": "qa:headings", "ok": False, "detail": "2 top-level titles duplicated: # Report"},
        {"name": "qa:links", "ok": False, "detail": "1 link(s) with empty targets"},
    ]
    assert (work / "extracted.md").read_text() == BROKEN


def test_a_plain_answer_skips_size_and_has_no_headings_row(work: Path) -> None:
    (work / "plain.md").write_text("Plain prose that ends well.\n")
    assert precheck.run({"deliverable": str(work / "plain.md")}) == [
        ok("qa:extraction"),
        {"name": "qa:size", "ok": True, "detail": "skipped - no size target on the task (5 words measured)"},
        ok("qa:truncation"),
        ok("qa:links"),
    ]


def test_empty_text_stops_after_the_extraction_row(work: Path) -> None:
    (work / "empty.md").write_text("  \n\n ")
    assert precheck.run({"deliverable": str(work / "empty.md")}) == [
        {"name": "qa:extraction", "ok": False, "detail": "extracted text is empty"}
    ]
    assert not (work / "extracted.md").exists()


def test_an_unreadable_supported_file_is_one_extraction_row(work: Path) -> None:
    del work
    assert precheck.run({"deliverable": "/nonexistent/doc.md"}) == [
        {
            "name": "qa:extraction",
            "ok": False,
            "detail": "the deliverable could not be read: [Errno 2] No such file or directory: '/nonexistent/doc.md'",
        }
    ]


def test_an_unsupported_extension_exits_3() -> None:
    with pytest.raises(SystemExit) as died:
        precheck.run({"deliverable": "/work/answer.weird"})
    assert died.value.code == 3


def _deck(path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Hello"
    slide.notes_slide.notes_text_frame.text = "say hi"
    deck.slides.add_slide(deck.slide_layouts[6])
    deck.save(str(path))


def _book(path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Data"
    sheet.append(["a", 1])
    sheet.append([None, None])
    sheet.append(["b", None])
    book.create_sheet("Blank")
    book.save(str(path))


def test_the_pptx_reader_counts_slides_and_reads_notes(tmp_path: Path) -> None:
    _deck(tmp_path / "d.pptx")
    read = precheck.extract(str(tmp_path / "d.pptx"))
    assert (read.markdown, read.units) == ("## Slide 1\n\nHello\n\nNotes: say hi\n\n## Slide 2", 2)


def test_the_xlsx_reader_counts_content_sheets(tmp_path: Path) -> None:
    _book(tmp_path / "b.xlsx")
    read = precheck.extract(str(tmp_path / "b.xlsx"))
    assert (read.markdown, read.units) == (
        "## Data\n\n| a | 1 |\n| --- | --- |\n|  |  |\n| b |  |\n\n## Blank",
        1,
    )


def test_a_deck_measures_slides_and_appends_its_layout_rows(
    work: Path, layout_calls: list[tuple[object, ...]]
) -> None:
    _deck(work / "d.pptx")
    checks = precheck.run(
        {"deliverable": str(work / "d.pptx"), "unit": "slides", "target_units": 2, "kind": "deck"}
    )
    assert checks == [
        ok("qa:extraction"),
        {"name": "qa:size", "ok": True, "detail": "2 slides vs target 2 ±30% (1–2)"},
        ok("qa:truncation"),
        ok("qa:headings"),
        ok("qa:links"),
        {"name": "qa:layout:pages", "ok": False, "detail": "x" * 500},
    ]
    assert [call[1:] for call in layout_calls] == [("deck", "slides", 2, 0.0, None)]


def test_a_workbook_measures_sheets_and_has_no_layout(
    work: Path, layout_calls: list[tuple[object, ...]]
) -> None:
    _book(work / "b.xlsx")
    checks = precheck.run({"deliverable": str(work / "b.xlsx"), "unit": "sheets", "target_units": 4})
    assert checks == [
        ok("qa:extraction"),
        {"name": "qa:size", "ok": False, "detail": "1 sheets vs target 4 ±30% (2–5)"},
        ok("qa:truncation"),
        ok("qa:headings"),
        ok("qa:links"),
    ]
    assert layout_calls == []


def test_a_pages_document_hands_layout_the_resolved_words_unit(
    work: Path, layout_calls: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    del work
    monkeypatch.setattr(precheck, "extract", lambda path: Extracted((Block("text", "Done."),), units=3))
    checks = precheck.run(
        {
            "deliverable": "/x/report.docx",
            "unit": "pages",
            "target_units": 10,
            "tolerance": 0.2,
            "rendition": "/x/r.pdf",
        }
    )
    assert checks == [
        ok("qa:extraction"),
        {"name": "qa:size", "ok": True, "detail": "skipped - no size target on the task (1 words measured)"},
        ok("qa:truncation"),
        ok("qa:links"),
        {"name": "qa:layout:pages", "ok": False, "detail": "x" * 500},
    ]
    assert layout_calls == [("/x/report.docx", "", "words", 10, 0.2, "/x/r.pdf")]
