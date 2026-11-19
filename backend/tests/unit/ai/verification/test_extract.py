# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one reader reads a document the way a person does: top to bottom, tables and charts where they sit.

Imported directly, the one legitimate exception to `suites/__init__.py`'s rule (as the other suite tests do).
python-docx is frame-pinned, so the `.docx` golden lives in the frame: tests/integration/verification/test_docx_suite.py.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from pptx import Presentation
from pptx.chart.data import ChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from prismoid.ai.verification import pipeline
from prismoid.ai.verification.suites.extract import EXTRACT_ROWS, UnsupportedFormat, extract, write_extract


def test_a_slide_reads_title_first_then_top_to_bottom_with_its_chart_and_notes(tmp_path: Path) -> None:
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(
        Inches(1), Inches(5), Inches(4), Inches(1)
    ).text_frame.text = "Source: the brief."
    data = ChartData()
    data.categories = ["North", "East"]
    data.add_series("Revenue ($M)", [12.4, 19.5])
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(6), Inches(3), data)
    slide.shapes.add_textbox(
        Inches(1), Inches(0.5), Inches(4), Inches(1)
    ).text_frame.text = "Revenue by region"
    slide.notes_slide.notes_text_frame.text = "East leads."  # pyright: ignore[reportOptionalMemberAccess]
    path = tmp_path / "deck.pptx"
    deck.save(str(path))

    read = extract(str(path))
    assert read.units == 1
    assert [block.kind for block in read.blocks] == ["slide", "text", "chart", "text", "notes"]
    assert read.markdown == (
        "## Slide 1\n\n"
        "Revenue by region\n\n"
        "|  | Revenue ($M) |\n| --- | --- |\n| North | 12.4 |\n| East | 19.5 |\n\n"
        "Source: the brief.\n\n"
        "Notes: East leads."
    )


def test_a_workbook_shows_formulas_with_their_numbers_and_says_when_it_was_cut(tmp_path: Path) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Model"
    for n in range(EXTRACT_ROWS + 5):
        sheet.append([f"row {n}", n])
    sheet["C1"] = "=B2+B3"
    book.create_sheet("Empty")
    path = tmp_path / "model.xlsx"
    book.save(str(path))

    read = extract(str(path), values=lambda ws: {"C1": 3.0} if ws.title == "Model" else {})
    assert (read.units, read.truncated) == (1, True)
    lines = read.markdown.splitlines()
    assert lines[:3] == [
        "## Model",
        "",
        f"(the first {EXTRACT_ROWS} rows and 30 columns of 205 x 3 are shown; the rest was NOT read)",
    ]
    assert lines[4:6] == ["| row 0 | 0 | =B2+B3 → 3 |", "| --- | --- | --- |"]
    assert lines[-1] == "## Empty"


def test_an_html_page_reads_words_not_markup(tmp_path: Path) -> None:
    file = tmp_path / "page.html"
    file.write_text(
        '<!doctype html><html lang="en"><head><title>Harbor Report</title>'
        "<style>body { width: 600px; }</style>"
        "<script>const secret = 12345;</script></head>"
        '<body><h1>Harbors</h1><img src="data:," width="600">'
        "<p>Traffic grew 12% in 2025.</p></body></html>"
    )
    text = extract(str(file)).markdown
    assert text == "Harbor Report\n\nHarbors\n\nTraffic grew 12% in 2025."


def test_an_unknown_extension_is_the_readers_gap(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormat):
        extract(str(tmp_path / "answer.weird"))


def test_the_judges_copy_past_the_cap_says_it_was_cut(tmp_path: Path) -> None:
    out = tmp_path / "extracted.md"
    job: dict[str, object] = {
        "extract_path": str(out),
        "extract_chars": 120,
        "extract_cut": pipeline.EXTRACT_CUT,
    }
    write_extract(job, "x" * 500)
    text = out.read_text()
    assert text.endswith(pipeline.EXTRACT_CUT) and len(text) == 120 + len(pipeline.EXTRACT_CUT)
