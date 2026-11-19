# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Planted-defect deliverables: each case is one small, real file built twice, clean and with one defect.

Runs inside a frame (python-docx, python-pptx, openpyxl and reportlab live in the frame image only):
`python3 build.py OUT_DIR` writes `OUT_DIR/<defect>/{clean,planted}<ext>` plus the case's sources, and
prints the manifest as JSON. Every zip timestamp and document date is pinned, so the bytes are reproducible.
"""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import docx
import openpyxl
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from reportlab.pdfgen import canvas

STAMP = re.compile(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*")

BRIEF = """# Northwind Q3 2026 operating brief

Figures are for the quarter ended September 2026 (Q3 2026).

- Revenue: $41.6M in Q3 2026, up from $33.9M in Q3 2025.
- Gross margin: 46.5%.
- Headcount: 312 at quarter end.
- Revenue by region: North $12.4M, South $18.1M, East $9.5M, West $1.6M; total $41.6M.
- Operating costs: payroll $14.2M, cloud $6.8M, marketing $3.1M; total $24.1M.
- Launch plan: beta opens 2026-11-02, general availability 2027-01-18, pricing review 2027-02-01.

The CFO, Dana Reyes, said: "We grew because enterprise renewals came in early, not because we cut prices."

Main risks: renewal concentration in the South region, and cloud costs rising faster than revenue.
"""
ANSWERS = {
    "answer-alpha.md": "Expand into the East region first: East has the most room to grow and the lowest churn.",
    "answer-beta.md": "Hold the East expansion until cloud costs are under control; margin is the constraint.",
}
REGIONS = {"North": 12.4, "South": 18.1, "East": 9.5, "West": 1.6}
COSTS = {"Payroll": 14.2, "Cloud": 6.8, "Marketing": 3.1}
RESULTS = "Revenue was $41.6M in Q3 2026, up from $33.9M in Q3 2025."
MARGIN = "Gross margin held at 46.5%, with 312 people at quarter end."
RISKS = (
    "The main risks are renewal concentration in the South region and cloud costs rising faster than revenue."
)


def frozen(data: bytes) -> bytes:
    """A zip-based file with every timestamp pinned (entries, core dates, embedded workbooks): same input, same bytes."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            body = source.read(info)
            if info.filename.endswith(".xlsx"):
                body = frozen(body)
            elif info.filename == "docProps/core.xml":
                body = STAMP.sub(rb"\g<1>2026-10-01T00:00:00Z", body)
            target.writestr(zipfile.ZipInfo(info.filename, date_time=(2026, 10, 1, 0, 0, 0)), body)
    return out.getvalue()


def freeze(path: Path) -> None:
    path.write_bytes(frozen(path.read_bytes()))


def save_docx(path: Path, build: Callable[[docx.document.Document], None]) -> None:
    document = docx.Document()
    build(document)
    document.save(str(path))
    freeze(path)


def save_pptx(path: Path, slides: list[Callable[[Presentation], None]]) -> None:
    deck = Presentation()
    for add in slides:
        add(deck)
    deck.save(str(path))
    freeze(path)


def titled(title: str, body: str = "") -> Callable[[Presentation], None]:
    def add(deck: Presentation) -> None:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body

    return add


def blank(deck: Presentation) -> None:
    deck.slides.add_slide(deck.slide_layouts[6])


def chart(east: float) -> Callable[[Presentation], None]:
    def add(deck: Presentation) -> None:
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Revenue by region, Q3 2026"
        data = CategoryChartData()
        data.categories = list(REGIONS)
        data.add_series(
            "Revenue ($M)", [east if name == "East" else value for name, value in REGIONS.items()]
        )
        slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.5), Inches(8), Inches(5), data
        )

    return add


def wrong_figure(path: Path, planted: bool) -> None:
    """A PDF whose revenue figure is transposed: $46.1M for the brief's $41.6M."""
    pdf = canvas.Canvas(str(path), invariant=1)
    lines = ["Northwind Q3 2026 results", RESULTS.replace("41.6", "46.1" if planted else "41.6"), MARGIN]
    for i, line in enumerate(lines):
        pdf.drawString(72, 720 - 24 * i, line)
    pdf.save()


def missing_section(path: Path, planted: bool) -> None:
    """A memo asked for Summary, Results and Risks; the planted one drops Risks."""
    parts = [
        "# Q3 2026 memo",
        "## Summary",
        "A strong quarter on early renewals.",
        "## Results",
        RESULTS,
        MARGIN,
    ]
    if not planted:
        parts += ["## Risks", RISKS]
    path.write_text("\n\n".join(parts) + "\n")


def unread_binary(path: Path, planted: bool) -> None:
    """A deck whose chart carries a fabricated East bar ($19.5M); the slide text is all grounded."""
    save_pptx(path, [titled("Q3 2026 results", RESULTS), chart(19.5 if planted else REGIONS["East"])])


def tables_out_of_order(path: Path, planted: bool) -> None:
    """Two headed tables; the planted report puts each table under the other's heading."""
    tables = [REGIONS, COSTS][:: -1 if planted else 1]

    def build(document: docx.document.Document) -> None:
        document.add_heading("Q3 2026 breakdown", 1)
        for heading, rows in zip(["Revenue by region", "Operating costs"], tables, strict=True):
            document.add_heading(heading, 2)
            table = document.add_table(rows=0, cols=2)
            for name, value in rows.items():
                cells = table.add_row().cells
                cells[0].text, cells[1].text = name, f"${value}M"

    save_docx(path, build)


def broken_formula(path: Path, planted: bool) -> None:
    """A model whose share formula names a sheet that does not exist ('Input' for 'Inputs')."""
    book = openpyxl.Workbook()
    inputs = book.active
    inputs.title = "Inputs"
    inputs.append(["Region", "Revenue ($M)"])
    for name, value in REGIONS.items():
        inputs.append([name, value])
    inputs.append(["Total", "=SUM(B2:B5)"])
    summary = book.create_sheet("Summary")
    sheet = "Input" if planted else "Inputs"
    summary.append(["South share of revenue", f"={sheet}!B3/{sheet}!B6"])
    book.save(str(path))
    freeze(path)


def empty_slide(path: Path, planted: bool) -> None:
    """A three-slide deck whose middle slide is blank in the planted copy."""
    middle = blank if planted else titled("Margin and team", MARGIN)
    save_pptx(path, [titled("Q3 2026 results", RESULTS), middle, titled("Risks", RISKS)])


def truncated_text(path: Path, planted: bool) -> None:
    """A report whose last paragraph stops mid-sentence."""
    outlook = "Next quarter we will" if planted else "Next quarter we will report on both risks."

    def build(document: docx.document.Document) -> None:
        document.add_heading("Q3 2026 report", 1)
        for text in (RESULTS, MARGIN, RISKS, outlook):
            document.add_paragraph(text)

    save_docx(path, build)


def fabricated_quote(path: Path, planted: bool) -> None:
    """A fleet comparison table; the planted one quotes a consensus neither answer stated."""
    beta = "both advisors agree the East expansion is safe" if planted else "margin is the constraint"
    rows = [
        "| Advisor | Position | In their words |",
        "|---|---|---|",
        '| alpha | Expand East first | "East has the most room to grow" |',
        f'| beta | Wait on cloud costs | "{beta}" |',
    ]
    path.write_text("# Should Northwind expand East?\n\n" + "\n".join(rows) + "\n")


def wrong_total(path: Path, planted: bool) -> None:
    """A regional table whose total row does not add up ($42.6M for $41.6M)."""
    rows = [f"| {name} | {value} |" for name, value in REGIONS.items()]
    total = "42.6" if planted else "41.6"
    table = ["| Region | Revenue ($M) |", "|---|---|", *rows, f"| Total | {total} |"]
    path.write_text("# Q3 2026 revenue by region\n\n" + "\n".join(table) + "\n")


def stale_date(path: Path, planted: bool) -> None:
    """A launch tracker whose GA date is last year's (2025-01-18 for the brief's 2027-01-18)."""
    dates = {"beta": "2026-11-02", "ga": "2025-01-18" if planted else "2027-01-18", "pricing": "2027-02-01"}
    tracker = {
        "collection_version": 1,
        "title": "Launch plan",
        "fields": [
            {"key": "milestone", "label": "Milestone", "type": "text"},
            {"key": "due", "label": "Due", "type": "date"},
        ],
        "rows": [{"id": key, "cells": {"milestone": key, "due": due}} for key, due in dates.items()],
        "views": [{"name": "Timeline", "layout": "table", "columns": ["milestone", "due"]}],
    }
    path.write_text(json.dumps(tracker, indent=2) + "\n")


# defect -> (artifact type, file extension, the sources it is checked against, the builder)
CASES: dict[str, tuple[str, str, dict[str, str], Callable[[Path, bool], None]]] = {
    "wrong_figure": ("document", ".pdf", {"brief.md": BRIEF}, wrong_figure),
    "missing_section": ("document", ".md", {"brief.md": BRIEF}, missing_section),
    "unread_binary": ("deck", ".pptx", {"brief.md": BRIEF}, unread_binary),
    "tables_out_of_order": ("document", ".docx", {"brief.md": BRIEF}, tables_out_of_order),
    "broken_formula": ("spreadsheet", ".xlsx", {"brief.md": BRIEF}, broken_formula),
    "empty_slide": ("deck", ".pptx", {"brief.md": BRIEF}, empty_slide),
    "truncated_text": ("document", ".docx", {"brief.md": BRIEF}, truncated_text),
    "fabricated_quote": ("fleet_report", ".md", ANSWERS, fabricated_quote),
    "wrong_total": ("document", ".md", {"brief.md": BRIEF}, wrong_total),
    "stale_date": ("collection", ".collection.json", {"brief.md": BRIEF}, stale_date),
}


def main(out: Path) -> None:
    manifest: list[dict[str, object]] = []
    for defect, (kind, ext, sources, make) in CASES.items():
        folder = out / defect
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in sources.items():
            (folder / name).write_text(text)
        for variant in ("clean", "planted"):
            make(folder / f"{variant}{ext}", variant == "planted")
        manifest.append({"defect": defect, "type": kind, "ext": ext, "sources": sorted(sources)})
    print(json.dumps(manifest))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
