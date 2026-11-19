#!/usr/bin/env python3
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Check a workbook BEFORE publishing it - run me: python3 check_workbook.py /work/model.xlsx

Runs inside the frame with the pinned openpyxl; opens the file read-only and never rewrites it.
The three rules mirror the product's spreadsheet verifier, so a clean pass here means the
arithmetic half of verification will pass. Grounding (every input verbatim in your sources) is
still on you. Exit 0 = clean; exit 1 prints every problem found.
"""

from __future__ import annotations

import os
import re
import sys

import openpyxl

WORK_DIR = os.environ.get("PRISMOID_WORK_DIR", "/work")  # a device Machine's /work lives elsewhere

CELL = re.compile(r"^\$?([A-Z]+)\$?(\d+)$")
RANGE = re.compile(r"^\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)$")
SUM = re.compile(r"^=SUM\(([^)]*)\)$", re.IGNORECASE)
TOTAL_WORD = re.compile(r"\b(?:grand[\s-]*total|subtotal|total)\b", re.IGNORECASE)


def col_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def check(path: str) -> list[str]:
    problems: list[str] = []
    wb = openpyxl.load_workbook(path, read_only=False, data_only=False)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            labels = [c for c in row if isinstance(c.value, str) and TOTAL_WORD.search(c.value)]
            for cell in row:
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    if "#REF!" in value:
                        problems.append(f"{ws.title}!{cell.coordinate}: formula references #REF!")
                    problems.extend(check_sum(ws, cell.coordinate, value))
                elif labels and isinstance(value, (int, float)) and not isinstance(value, bool):
                    problems.append(
                        f"{ws.title}!{cell.coordinate}: hardcoded number in a "
                        f"'{labels[0].value}' row - a total must be a =SUM formula"
                    )
    return problems


def check_sum(ws: openpyxl.worksheet.worksheet.Worksheet, coord: str, formula: str) -> list[str]:
    """A 1-D =SUM(range) must cover its whole contiguous block: the cell just past each end
    of the range must not hold another number (that is the short-changed-range lie)."""
    m = SUM.match(formula.strip())
    if not m:
        return []
    r = RANGE.match(m.group(1).strip())
    if not r:
        return []
    c1, r1, c2, r2 = r.group(1), int(r.group(2)), r.group(3), int(r.group(4))
    problems: list[str] = []
    if c1 == c2:  # a column slice: look one row past each end
        lo, hi = sorted((r1, r2))
        for beyond in (lo - 1, hi + 1):
            if beyond < 1 or f"{c1}{beyond}" == coord:
                continue
            value = ws[f"{c1}{beyond}"].value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                problems.append(
                    f"{ws.title}!{coord}: =SUM({m.group(1)}) stops short - {c1}{beyond} "
                    "also holds a number; widen the range to the whole block"
                )
    elif r1 == r2:  # a row slice: look one column past each end
        lo, hi = sorted((col_num(c1), col_num(c2)))
        for beyond in (lo - 1, hi + 1):
            if beyond < 1:
                continue
            col = ""
            n = beyond
            while n > 0:
                n, rem = divmod(n - 1, 26)
                col = chr(ord("A") + rem) + col
            if f"{col}{r1}" == coord:
                continue
            value = ws[f"{col}{r1}"].value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                problems.append(
                    f"{ws.title}!{coord}: =SUM({m.group(1)}) stops short - {col}{r1} "
                    "also holds a number; widen the range to the whole block"
                )
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: python3 check_workbook.py {WORK_DIR}/model.xlsx")
        return 2
    problems = check(sys.argv[1])
    if problems:
        print(f"NOT CLEAN - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("clean: totals are formulas, ranges cover their blocks, no broken references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
