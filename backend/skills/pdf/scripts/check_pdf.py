#!/usr/bin/env python3
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Check a PDF BEFORE publishing it - run me: python3 check_pdf.py /work/report.pdf

Runs inside the frame with the pinned pypdf; opens the file read-only and never rewrites it.
Proves the two things a PDF deliverable must be: openable, and made of real text (the verifier
extracts your text and grounds its numbers - a rasterized or empty PDF fails). Prints the head
of the extracted text so you can read your own document back. Exit 0 = clean; exit 1 otherwise.
"""

from __future__ import annotations

import os
import sys

from pypdf import PdfReader

WORK_DIR = os.environ.get("PRISMOID_WORK_DIR", "/work")  # a device Machine's /work lives elsewhere


def main() -> int:
    if "--layout" in sys.argv:  # the house geometry pass (ADR-034): render + look
        sys.argv.remove("--layout")
        from plane_house.render import main as render_main  # frame-resident

        return render_main([sys.argv[1], "--pages", f"{WORK_DIR}/pages"]) if len(sys.argv) == 2 else 2
    if len(sys.argv) != 2:
        print(f"usage: python3 check_pdf.py {WORK_DIR}/report.pdf")
        return 2
    path = sys.argv[1]
    try:
        reader = PdfReader(path)
        pages = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as broken:  # any unreadable PDF is the same verdict: not publishable
        print(f"NOT CLEAN - the file does not open as a PDF: {broken}")
        return 1

    print(f"{pages} page(s) · {len(text)} extracted characters")
    if text:
        print("--- extracted head ---")
        print(text[:600])

    problems: list[str] = []
    if pages == 0:
        problems.append("zero pages")
    if not text:
        problems.append(
            "no extractable text - the content is rasterized or empty; keep the content as "
            "real text (Paragraph/drawString), not pixels"
        )
    if problems:
        print(f"\nNOT CLEAN - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nclean: opens, paginated, text extracts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
