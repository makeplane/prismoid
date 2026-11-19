# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mounted `NN-*.md` sections → ONE .pdf in the HOUSE LOOK (ADR-025, ADR-034).

    python3 md_to_pdf.py --sections /task/inputs --out /work/report.pdf \
        --title "The Report" [--toc] [--images /work/images] [--subtitle "..."] [--total N] \
        [--engine soffice|reportlab]

Two engines, one look. `soffice` (default): build the house .docx with md_to_docx's exact
machinery, then convert it through LibreOffice - the PDF is the Word file's twin (same fonts,
real page numbers, repeating table headers), and the only engine that embeds the house face (Inter).
`reportlab`: the instant, dependency-free fallback (Helvetica; house furniture hand-drawn).
The default tries soffice and FALLS BACK to reportlab when the conversion fails or times out,
printing which engine won - that line is the pre-flight. Same grammar as md_to_docx.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from xml.sax.saxutils import escape  # reportlab Paragraph parses markup - "R&D"/"a < b" crash it

from md_walk import Block, figure_path, gap_note, load_sections


def flowables_for(block: Block, styles, images_dir: str | None, counter: dict) -> list:
    from plane_house.pdf_theme import figure, figure_placeholder, table
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, Spacer

    if block.kind == "heading":
        return [Paragraph(escape(block.text), styles[f"Heading{min(block.level + 1, 3)}"])]
    if block.kind == "bullet":
        items = [ListItem(Paragraph(escape(item), styles["Body"]), leftIndent=12) for item in block.items]
        return [ListFlowable(items, bulletType="bullet", bulletFontSize=8, leftIndent=14), Spacer(1, 6)]
    if block.kind == "table":
        return [table(block.rows, styles=styles), Spacer(1, 10)]
    if block.kind == "figure":
        counter["figure"] += 1
        path = figure_path(images_dir, block.fig_id)
        if path:
            return [figure(path, block.text, counter["figure"], styles=styles)]
        return [figure_placeholder(block.text, counter["figure"], styles=styles)]
    return [Paragraph(escape(block.text), styles["Body"])]


def build_reportlab(
    sections_dir: str,
    out: str,
    title: str,
    subtitle: str,
    toc: bool,
    images_dir: str | None,
    total: int | None,
) -> None:
    from plane_house.pdf_theme import HouseDocTemplate, house_styles
    from reportlab.platypus import PageBreak, Paragraph, Spacer

    sections, gaps = load_sections(sections_dir, total)
    if not sections:
        raise SystemExit(f"no NN-*.md sections found in {sections_dir}")

    styles = house_styles()
    story = [Spacer(1, 180), Paragraph(escape(title), styles["Title"])]
    if subtitle:
        story.append(Paragraph(escape(subtitle), styles["Subtitle"]))
    story.append(PageBreak())
    if toc:
        story.append(Paragraph("Contents", styles["Heading1"]))
        for section in sections:
            story.append(Paragraph(escape(f"{section.number:02d}  {section.title}"), styles["Body"]))
        story.append(PageBreak())

    counter = {"figure": 0}
    by_number = {s.number: s for s in sections}
    end = max([*by_number, *gaps], default=0)
    for number in range(1, end + 1):
        section = by_number.get(number)
        if section is None:
            story.append(Paragraph(escape(gap_note(number)), styles["Caption"]))
            continue
        words = 0
        for block in section.blocks:
            story.extend(flowables_for(block, styles, images_dir, counter))
            words += len(block.text.split()) + sum(len(i.split()) for i in block.items)
        print(f"  {section.number:02d} {section.title}: {words} words")

    HouseDocTemplate(out, title=title).build(story)
    print(
        f"saved {out} [engine=reportlab]: {len(sections)} sections, {counter['figure']} figure(s), {len(gaps)} gap note(s)"
    )


def build_soffice(
    sections_dir: str,
    out: str,
    title: str,
    subtitle: str,
    toc: bool,
    images_dir: str | None,
    total: int | None,
) -> bool:
    """The docx twin, converted. True on success; False = caller falls back."""
    from md_to_docx import build as build_docx
    from plane_house.render import to_pdf

    workdir = tempfile.mkdtemp(prefix="md_to_pdf-", dir="/tmp")
    docx_path = os.path.join(workdir, os.path.splitext(os.path.basename(out))[0] + ".docx")
    build_docx(sections_dir, docx_path, title, subtitle, toc, images_dir, total)
    pdf = to_pdf(docx_path, os.path.join(workdir, "pdf"))
    if not pdf:
        print("soffice conversion failed - falling back to reportlab")
        return False
    shutil.copyfile(pdf, out)
    print(f"saved {out} [engine=soffice]: converted from the house .docx twin")
    return True


def build(
    sections_dir: str,
    out: str,
    title: str,
    subtitle: str,
    toc: bool,
    images_dir: str | None,
    total: int | None = None,
    engine: str = "soffice",
) -> None:
    if engine == "soffice" and build_soffice(sections_dir, out, title, subtitle, toc, images_dir, total):
        return
    build_reportlab(sections_dir, out, title, subtitle, toc, images_dir, total)


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
    ap.add_argument("--engine", choices=("soffice", "reportlab"), default="soffice")
    args = ap.parse_args()
    build(args.sections, args.out, args.title, args.subtitle, args.toc, args.images, args.total, args.engine)
