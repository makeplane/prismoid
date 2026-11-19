#!/usr/bin/env python3
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Check a .docx BEFORE publishing it - run me: python3 check_docx.py /work/report.docx

Runs inside the frame with the pinned python-docx; opens the file read-only and never rewrites
it. Prints the heading outline and body stats so you can read your own document back, and flags
what the verifier (or any reader) would refuse. Exit 0 = clean; exit 1 prints every problem.
"""

from __future__ import annotations

import os
import sys

from docx import Document

WORK_DIR = os.environ.get("PRISMOID_WORK_DIR", "/work")  # a device Machine's /work lives elsewhere

FAKE_LIST_STARTS = ("• ", "- ", "* ", "· ")


def check(path: str) -> tuple[list[str], list[str]]:
    doc = Document(path)
    problems: list[str] = []
    outline: list[str] = []
    words = 0
    headings = 0
    fake_lists = 0

    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        words += len(text.split())
        style = p.style.name if p.style is not None else ""
        if style.startswith("Heading") or style == "Title":
            headings += 1
            outline.append(f"{style}: {text}")
        elif text.startswith(FAKE_LIST_STARTS) and not style.startswith("List"):
            fake_lists += 1

    cells = 0
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                if cell.text.strip():
                    cells += 1
                    words += len(cell.text.split())

    if words == 0:
        problems.append("the document body is empty - decoration without content is not a deliverable")
    if fake_lists:
        problems.append(
            f"{fake_lists} paragraph(s) start with a typed bullet glyph but are not List-styled - "
            "use style='List Bullet' / 'List Number'"
        )
    if words > 250 and headings == 0:
        problems.append(
            f"{words} words with zero headings - give the reader a structure (add_heading levels 1-2)"
        )
    return problems, [
        f"~{words} words · {headings} heading(s) · {len(doc.tables)} table(s) · {cells} filled cell(s)",
        *outline,
    ]


def main() -> int:
    if "--layout" in sys.argv:  # the house geometry pass (ADR-034): render + look
        sys.argv.remove("--layout")
        from plane_house.render import main as render_main  # frame-resident

        return render_main([sys.argv[1], "--pages", f"{WORK_DIR}/pages"]) if len(sys.argv) == 2 else 2
    if len(sys.argv) != 2:
        print(f"usage: python3 check_docx.py {WORK_DIR}/report.docx")
        return 2
    try:
        problems, dump = check(sys.argv[1])
    except Exception as broken:  # an unopenable file is the same verdict, said plainly
        print(f"NOT CLEAN - the file does not open as a .docx: {broken}")
        return 1
    for line in dump:
        print(line)
    if problems:
        print(f"\nNOT CLEAN - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nclean: structured, styled lists, non-empty body")
    return 0


if __name__ == "__main__":
    sys.exit(main())
