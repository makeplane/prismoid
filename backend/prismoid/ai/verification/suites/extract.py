# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one reader of a deliverable's words, in document order: every suite and the QA precheck read through it.

Runs inside the frame beside the script that loads it by exact path (`COMPANIONS` in pipeline.py; the QA
precheck ships it as `suites/extract.py` beside itself). python-docx, python-pptx, openpyxl and pdfplumber are
pinned in the frame image only and imported lazily, so the `.md` and `.html` paths stay stdlib.

A reader meets a document top to bottom, so the blocks come in that order: a `.docx` table sits under the
heading it belongs to, a slide reads title first and then top to bottom, and a chart's series read as a table
(categories down, one column per series) so a plotted figure is grounded like any cell. Extraction is the
judge's eyes on a binary: the checks run on a document's WORDS, never its markup or its zip (a `width="600"`
attribute must not become an ungrounded figure), and the judge, which never reads binary bytes, reads the text
`write_extract` leaves.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, cast

EXTRACT_ROWS = 200  # a sheet is read this far down and across; past either, `truncated` says so
EXTRACT_COLS = 30

BlockKind = Literal["heading", "text", "table", "chart", "notes", "slide", "sheet"]


@dataclass(frozen=True, slots=True)
class Block:
    """One piece of a document as a reader meets it, written as markdown."""

    kind: BlockKind
    text: str


@dataclass(frozen=True, slots=True)
class Extracted:
    """A document's blocks in reading order; `units` counts its pages, slides or content sheets (0: none)."""

    blocks: tuple[Block, ...]
    units: int = 0
    truncated: bool = False

    @property
    def markdown(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)


class UnsupportedFormat(ValueError):
    """No reader knows this extension: the reader's gap, never the document's."""


def extract(path: str, *, values: Callable[[Any], dict[str, float]] | None = None) -> Extracted:
    """Read `path` by its extension; `values(sheet)` gives a workbook's computed numbers by cell, for its formulas."""
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsx":
        return _xlsx(path, values)
    reader = _READERS.get(suffix)
    if reader is None:
        raise UnsupportedFormat(f"no reader for {suffix or 'a file without an extension'}: {path}")
    return reader(path)


def write_extract(job: dict[str, object], text: str) -> None:
    """Leave what the suite read for the judge, which cannot open a binary; best-effort, cut as the job says."""
    if extract_path := job.get("extract_path"):
        cap = int(str(job["extract_chars"]))
        kept = text if len(text) <= cap else text[:cap] + str(job["extract_cut"])
        with contextlib.suppress(OSError):
            Path(str(extract_path)).write_text(kept, encoding="utf-8")


def _table(rows: list[list[str]]) -> str:
    """Rows as a GFM table, the first row its header."""
    cells = [[cell.strip().replace("|", "\\|").replace("\n", " ") for cell in row] for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in cells]
    lines.insert(1, "|" + " --- |" * len(cells[0]))
    return "\n".join(lines)


def _plain(path: str) -> Extracted:
    return Extracted((Block("text", Path(path).read_text(encoding="utf-8")),))


def _docx(path: str) -> Extracted:
    """Paragraphs and tables interleaved as the body holds them; empty paragraphs say nothing."""
    import docx  # pyright: ignore[reportMissingImports] - frame-only, lazy on purpose
    from docx.table import (  # pyright: ignore[reportMissingImports]
        Table,  # pyright: ignore[reportUnknownVariableType]  # untyped outside the frame
    )
    from docx.text.paragraph import (  # pyright: ignore[reportMissingImports]
        Paragraph,  # pyright: ignore[reportUnknownVariableType]  # untyped outside the frame
    )

    document = cast("Any", docx.Document(path))
    blocks: list[Block] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            para = cast("Any", Paragraph(child, document))
            if para.text.strip():
                blocks.append(_docx_paragraph(para))
        elif child.tag.endswith("}tbl"):
            rows = [[cell.text for cell in row.cells] for row in cast("Any", Table(child, document)).rows]
            if any(cell.strip() for row in rows for cell in row):
                blocks.append(Block("table", _table(rows)))
    return Extracted(tuple(blocks))


def _docx_paragraph(para: Any) -> Block:
    """A heading keeps its level (Title is the one `#`, Heading N is N+1), a list item its `- `."""
    style: str = (para.style.name if para.style is not None else "") or ""
    level = style.removeprefix("Heading ")
    if style == "Title":
        return Block("heading", f"# {para.text}")
    if level.isdigit():
        return Block("heading", "#" * min(int(level) + 1, 6) + f" {para.text}")
    return Block("text", ("- " if style.startswith("List") else "") + para.text)


def _pptx(path: str) -> Extracted:
    """Each slide under its number: its shapes in reading order, then its speaker notes."""
    from pptx import Presentation  # pyright: ignore[reportMissingImports] - frame-only

    deck = cast("Any", Presentation(path))
    blocks: list[Block] = []
    for number, slide in enumerate(deck.slides, 1):
        blocks.append(Block("slide", f"## Slide {number}"))
        blocks += _slide_blocks(slide)
        # has_notes_slide FIRST: python-pptx CREATES a notes slide on attribute access.
        notes = slide.notes_slide.notes_text_frame if slide.has_notes_slide else None
        if notes is not None and notes.text.strip():
            blocks.append(Block("notes", f"Notes: {notes.text.strip()}"))
    return Extracted(tuple(blocks), units=len(deck.slides))


def _slide_blocks(slide: Any) -> list[Block]:
    """A slide's shapes as a reader takes them in: the title, then top to bottom, left to right."""
    title = slide.shapes.title
    title_id = title.shape_id if title is not None else None
    blocks: list[Block] = []
    for shape in sorted(slide.shapes, key=lambda s: (s.shape_id != title_id, s.top or 0, s.left or 0)):
        if shape.has_text_frame:
            lines = ("".join(run.text for run in para.runs) for para in shape.text_frame.paragraphs)
            if text := "\n".join(line for line in lines if line.strip()):
                blocks.append(Block("text", text))
        elif shape.has_table:
            blocks.append(
                Block("table", _table([[cell.text for cell in row.cells] for row in shape.table.rows]))
            )
        elif shape.has_chart:
            blocks += (Block("chart", _table(rows)) for rows in _chart_tables(shape.chart))
    return blocks


def _chart_tables(chart: Any) -> list[list[list[str]]]:
    """Each plot's data as rows: a header of series names, then one row per category."""
    tables: list[list[list[str]]] = []
    for plot in chart.plots:
        series = list(plot.series)
        columns = [list(s.values) for s in series]
        rows = [["", *(str(s.name) for s in series)]]
        for i, category in enumerate(plot.categories):
            rows.append(
                [str(category), *(_number(column[i] if i < len(column) else None) for column in columns)]
            )
        tables.append(rows)
    return tables


def _number(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def _xlsx(path: str, values: Callable[[Any], dict[str, float]] | None) -> Extracted:
    """Each sheet as a table, a formula beside the number it came to; past the caps `truncated` says so."""
    import openpyxl  # pyright: ignore[reportMissingImports, reportMissingModuleSource] - frame-only

    book = cast("Any", openpyxl.load_workbook(path, data_only=False))
    blocks: list[Block] = []
    content, truncated = 0, False
    for sheet in book.worksheets:
        blocks.append(Block("sheet", f"## {sheet.title}"))
        computed = values(sheet) if values else {}
        cells = sheet.iter_rows(max_row=EXTRACT_ROWS, max_col=min(sheet.max_column, EXTRACT_COLS))
        rows = [[_cell(cell.value, computed.get(cell.coordinate)) for cell in row] for row in cells]
        while rows and not any(rows[-1]):
            rows.pop()  # trailing all-empty rows say nothing
        if not rows:
            continue
        content += 1
        if sheet.max_row > EXTRACT_ROWS or sheet.max_column > EXTRACT_COLS:
            truncated = True
            blocks.append(
                Block(
                    "text",
                    f"(the first {EXTRACT_ROWS} rows and {EXTRACT_COLS} columns of {sheet.max_row} x "
                    f"{sheet.max_column} are shown; the rest was NOT read)",
                )
            )
        blocks.append(Block("table", _table(rows)))
    return Extracted(tuple(blocks), units=content, truncated=truncated)


def _cell(value: object, computed: float | None) -> str:
    """One cell's text: a formula keeps its text and shows the number it came to, when that is known."""
    if isinstance(value, str) and value.startswith("="):
        return value if computed is None else f"{value} → {computed:.10g}"
    return "" if value is None else str(value)


def _pdf(path: str) -> Extracted:
    """Each page's text in layout order; a page with no words adds no block."""
    import pdfplumber  # pyright: ignore[reportMissingImports] - frame-only, lazy on purpose

    with cast("Any", pdfplumber).open(path) as pdf:
        pages = [(page.extract_text() or "").strip() for page in pdf.pages]
    return Extracted(tuple(Block("text", page) for page in pages if page), units=len(pages))


class _HTMLText(HTMLParser):
    """Text nodes in order, the document's `<title>` kept apart, `script`/`style` bodies skipped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str = ""
        self._skip = 0
        self._svg = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "svg":
            self._svg += 1
        elif tag == "title" and not self._svg:  # an inline <svg><title> is an icon's label
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "svg" and self._svg:
            self._svg -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if self._skip or not text:
            return
        if not self._in_title:
            self.parts.append(text)
        elif not self.title:  # first wins - a stray later <title> never rewrites it
            self.title = text


def _html(path: str) -> Extracted:
    """A page's words, never its markup: the `<title>` first, then the text nodes in order."""
    parser = _HTMLText()
    # utf-8, not the locale's: the bytes the sha attested must read the same on every host
    parser.feed(Path(path).read_bytes().decode("utf-8", errors="replace"))
    parser.close()
    text = "\n\n".join(([parser.title] if parser.title else []) + parser.parts).strip()
    return Extracted((Block("text", text),) if text else ())


_READERS: dict[str, Callable[[str], Extracted]] = {
    ".md": _plain,
    ".txt": _plain,
    ".docx": _docx,
    ".pptx": _pptx,
    ".pdf": _pdf,
    ".html": _html,
}
