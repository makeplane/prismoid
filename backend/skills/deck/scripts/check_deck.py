#!/usr/bin/env python3
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Check a deck BEFORE publishing it - run me: python3 check_deck.py /work/deck.pptx

Runs inside the frame with the pinned python-pptx; opens the file read-only and never rewrites
it. Prints every slide's text so you can read your own deck back (wrong order, missing points,
and leftover placeholder text hide in code you just wrote), and flags the defects a viewer
would see instantly. Exit 0 = clean; exit 1 prints every problem found.
"""

from __future__ import annotations

import os
import sys

from pptx import Presentation

WORK_DIR = os.environ.get("PRISMOID_WORK_DIR", "/work")  # a device Machine's /work lives elsewhere


def shape_texts(shape) -> list[str]:  # untyped param: pptx types exist only in the frame
    if shape.shape_type == 6 and hasattr(shape, "shapes"):  # a group: recurse
        out: list[str] = []
        for member in shape.shapes:
            out.extend(shape_texts(member))
        return out
    if getattr(shape, "has_text_frame", False):
        text = "\n".join(p.text for p in shape.text_frame.paragraphs).strip()
        return [text] if text else []
    return []


def check(path: str) -> tuple[list[str], list[str]]:
    prs = Presentation(path)
    problems: list[str] = []
    dump: list[str] = []
    if not prs.slides:
        return ["the deck has zero slides"], dump

    width, height = int(prs.slide_width), int(prs.slide_height)
    for index, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            texts.extend(shape_texts(shape))
            left, top = getattr(shape, "left", None), getattr(shape, "top", None)
            w, h = getattr(shape, "width", None), getattr(shape, "height", None)
            if None in (left, top, w, h):
                continue  # placeholder inheriting position from its layout - fine
            if left < 0 or top < 0 or left + w > width or top + h > height:
                problems.append(
                    f"slide {index}: shape {shape.shape_id} ('{shape.name}') sits outside the "
                    "slide - move or shrink it, it renders cut off or invisible"
                )
        title = slide.shapes.title
        if title is not None and not title.text.strip():
            problems.append(f"slide {index}: the title placeholder is empty")
        if not texts:
            problems.append(f"slide {index}: no text at all - a slide must make its point")
        dump.append(f"--- slide {index} ---")
        dump.extend(texts)
    return problems, dump


def main() -> int:
    if "--layout" in sys.argv:  # the house geometry pass (ADR-034): render + look
        sys.argv.remove("--layout")
        from plane_house.render import main as render_main  # frame-resident

        return render_main([sys.argv[1], "--pages", f"{WORK_DIR}/pages"]) if len(sys.argv) == 2 else 2
    if len(sys.argv) != 2:
        print(f"usage: python3 check_deck.py {WORK_DIR}/deck.pptx")
        return 2
    try:
        problems, dump = check(sys.argv[1])
    except Exception as broken:  # an unopenable file is the same verdict, said plainly
        print(f"NOT CLEAN - the file does not open as a .pptx: {broken}")
        return 1
    for line in dump:
        print(line)
    if problems:
        print(f"\nNOT CLEAN - {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nclean: every shape on-canvas, every slide titled and non-empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
