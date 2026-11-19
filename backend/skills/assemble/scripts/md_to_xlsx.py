# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mounted `NN-*.md` sheet-spec sections → ONE .xlsx, disk to disk (ADR-029) - the workbook
target.

    python3 md_to_xlsx.py --sections /task/inputs --out /work/model.xlsx --title "The Model"

One section = ONE SHEET (the extended+ spreadsheet lane's contract): pipe tables become cell
rows (numbers coerced, header row bold + frozen), headings become bold divider rows, bullets
and paragraphs - including the writers' `Derivation:` lines - become note rows beneath the
data. THE WALKER LAYS DATA ONLY: the maker's assembly step patches real formulas over any
cells whose derivations the section stated (its prompt says so) - a text "B×C" is a note, not
a formula, and pretending otherwise would be painted arithmetic. Gap numbers become a visible
"Gaps" sheet, never a silent skip. `-final` files are preferred (ADR-028)."""

from __future__ import annotations

import argparse
import re

from md_walk import gap_note, load_sections

_SHEET_BAD = re.compile(r"[\\/*?:\[\]]")


def _sheet_name(title: str, used: set[str]) -> str:
    name = _SHEET_BAD.sub("", title).strip()[:31] or "Sheet"
    base, n = name, 2
    while name.lower() in used:
        name = f"{base[:28]} {n}"
        n += 1
    used.add(name.lower())
    return name


def _cell(value: str) -> tuple[object, str | None]:
    """(cell value, number_format | None). Data semantics survive coercion (review): "45%"
    is 0.45 wearing a percent format, never the number 45; a leading-zero token ("007",
    "0042") is an identifier and stays text - int() would silently rewrite it."""
    text = value.strip()
    if not text:
        return None, None
    plain = text.replace(",", "")
    if plain.endswith("%"):
        try:
            return float(plain[:-1]) / 100.0, ("0.0%" if "." in plain else "0%")
        except ValueError:
            return text, None
    if len(plain) > 1 and plain[0] == "0" and plain.isdigit():
        return text, None  # a leading-zero id, not a number
    try:
        return int(plain), None
    except ValueError:
        pass
    try:
        return float(plain), None
    except ValueError:
        return text, None


def build(sections_dir: str, out: str, title: str) -> None:
    import openpyxl  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
    from openpyxl.styles import Font

    book = openpyxl.Workbook()
    book.remove(book.active)
    bold = Font(bold=True)
    used: set[str] = set()

    sections, gaps = load_sections(sections_dir)
    if not sections:
        raise SystemExit(f"no NN-*.md sections found in {sections_dir}")

    for section in sections:
        tab = _sheet_name(section.title, used)
        sheet = book.create_sheet(tab)
        row = 1
        widths: dict[int, int] = {}
        # Excel tabs truncate at 31 chars and strip \/*?:[] - when that mangles the title, the
        # FULL title must exist in-sheet or qa:sections can never find it (review M7): a bold
        # A1 row carries it and the tab stays the short label.
        if tab.strip().lower() != section.title.strip().lower():
            title_cell = sheet.cell(row=1, column=1, value=section.title)
            title_cell.font = bold
            row = 3
        title_skipped = False
        for block in section.blocks:
            if block.kind == "heading":
                if block.text.lower() == section.title.lower() and not title_skipped:
                    title_skipped = True
                    continue  # the tab (or the A1 title row) already carries the section title
                cell = sheet.cell(row=row, column=1, value=block.text)
                cell.font = bold
                row += 2
            elif block.kind == "table":
                for r, table_row in enumerate(block.rows):
                    for c, value in enumerate(table_row, start=1):
                        content, number_format = _cell(value)
                        data_cell = sheet.cell(row=row, column=c, value=content)
                        if number_format:
                            data_cell.number_format = number_format
                        widths[c] = min(60, max(widths.get(c, 10), len(value.strip()) + 2))
                    if r == 0:
                        for c in range(1, len(table_row) + 1):
                            sheet.cell(row=row, column=c).font = bold
                        if row == 1:
                            sheet.freeze_panes = "A2"
                    row += 1
                row += 1
            elif block.kind == "bullet":
                for item in block.items:
                    sheet.cell(row=row, column=1, value=f"• {item}")
                    row += 1
                row += 1
            elif block.kind in ("para", "figure"):
                sheet.cell(row=row, column=1, value=block.text)
                row += 2
        for c, width in widths.items():
            sheet.column_dimensions[sheet.cell(row=1, column=c).column_letter].width = width

    if gaps:
        sheet = book.create_sheet("Gaps")
        for r, number in enumerate(gaps, start=1):
            sheet.cell(row=r, column=1, value=gap_note(number))

    if not book.sheetnames:
        raise SystemExit("nothing to write")
    book.save(out)
    print(f"saved {out}: {len(book.sheetnames)} sheet(s) ({len(sections)} specs, {len(gaps)} gap note(s))")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    args = ap.parse_args()
    build(args.sections, args.out, args.title)
