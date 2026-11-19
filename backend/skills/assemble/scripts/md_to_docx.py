# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mounted `NN-*.md` sections → ONE .docx in the HOUSE LOOK (ADR-025, ADR-034). Run me, don't rewrite me.

    python3 md_to_docx.py --sections /task/inputs --out /work/report.docx \
        --title "The Report" [--toc] [--images /work/images] [--subtitle "..."] [--total N]

Cover page · house styles (Inter, heading colours, keep-with-next) · house margins · page numbers
in the footer · headings→real styles · bullets→List Bullet · pipe tables→shaded repeating
header, widths on every cell · figure markers→aspect-fit pictures with NUMBERED captions
(`Figure 3. caption`; the fig id goes to alt text + keywords, never the face) or the caption as
a visible placeholder · no page break per section (the first body section starts a page; the
rest flow) · a gap in the NN sequence→a visible gap note. One save at the end; publish is the
caller's job.
"""

from __future__ import annotations

import argparse

from md_walk import Block, figure_path, gap_note, load_sections


class _Counter:
    figure = 0


def add_block(
    doc, block: Block, images_dir: str | None, counter: _Counter, *, first_heading: bool = False
) -> None:
    from plane_house.docx_theme import add_figure, add_table, figure_placeholder, heading

    if block.kind == "heading":
        heading(doc, block.text, level=min(block.level + 1, 3), first=first_heading)  # section h1 → doc h2
    elif block.kind == "bullet":
        for item in block.items:
            doc.add_paragraph(item, style="List Bullet")
    elif block.kind == "table":
        add_table(doc, block.rows)
    elif block.kind == "figure":
        counter.figure += 1
        path = figure_path(images_dir, block.fig_id)
        if path:
            add_figure(doc, path, block.text, counter.figure, fig_id=block.fig_id)
        else:
            figure_placeholder(doc, block.text, counter.figure, fig_id=block.fig_id)
    else:
        doc.add_paragraph(block.text)


def build(
    sections_dir: str,
    out: str,
    title: str,
    subtitle: str,
    toc: bool,
    images_dir: str | None,
    total: int | None = None,
) -> None:
    from plane_house.docx_theme import heading, house_document  # frame-resident (ADR-034)

    sections, gaps = load_sections(sections_dir, total)
    if not sections:
        raise SystemExit(f"no NN-*.md sections found in {sections_dir}")

    doc = house_document(title, subtitle)
    if toc:
        heading(doc, "Contents", 1)
        for section in sections:
            doc.add_paragraph(
                f"{section.number:02d}   {section.title}"
            )  # plain lines: a List Number would double-number
        doc.add_page_break()  # the body starts on its own page; sections then FLOW (no break per section)

    counter = _Counter()
    by_number = {s.number: s for s in sections}
    end = max([*by_number, *gaps], default=0)  # cover trailing gaps too when --total is given
    first = True
    for number in range(1, end + 1):
        section = by_number.get(number)
        if section is None:
            doc.add_paragraph(gap_note(number))  # visible, in place - never a silent skip
            continue
        words = 0
        for block in section.blocks:
            add_block(doc, block, images_dir, counter, first_heading=first and block.kind == "heading")
            if block.kind == "heading":
                first = False
            words += len(block.text.split()) + sum(len(i.split()) for i in block.items)
        print(f"  {section.number:02d} {section.title}: {words} words")

    doc.save(out)
    print(
        f"saved {out}: {len(sections)} sections, {len(doc.paragraphs)} paragraphs, "
        f"{counter.figure} figure(s), {len(gaps)} gap note(s)"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sections", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--toc", action="store_true")
    ap.add_argument("--images", default=None)
    ap.add_argument(
        "--total", type=int, default=None, help="expected section count (outline's section_total)"
    )
    args = ap.parse_args()
    build(args.sections, args.out, args.title, args.subtitle, args.toc, args.images, args.total)
