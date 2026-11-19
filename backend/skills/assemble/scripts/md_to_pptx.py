# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mounted `NN-*.md` slide-group sections → ONE .pptx in the HOUSE LOOK (ADR-029, ADR-034).

    python3 md_to_pptx.py --sections /task/inputs --out /work/deck.pptx \
        --title "The Deck" [--subtitle "..."] [--images /work/images]

The grammar is what the section writers are taught to emit for decks: every `## Slide: <title>`
(or any heading) starts a slide; dash bullets fill its body; a paragraph starting `Notes:`
becomes SPEAKER NOTES; `Figure (fig-…): caption` markers and `![](path)` images place a
picture from --images (or stand as their caption when generation failed - the degrade law);
pipe tables become NATIVE tables. The layout is chosen from the content: picture + bullets →
image beside text, picture alone → full-bleed with a caption bar, table → table slide, one
short numeric line → big number, else bullets - and bullets past the density caps SPLIT onto
`(cont.)` slides (the count is printed; the sizing band must allow for it). Figure ids live in
speaker notes + alt text, never on the slide face. Each section opens with a divider; a missing
section number becomes a visible gap slide, never a silent skip. Harmonized `NN-…-final.md`
files are preferred over raw sections by `load_sections` (ADR-028), same as every walker.
"""

from __future__ import annotations

import argparse
import re

from md_walk import Block, Section, figure_path, gap_note, load_sections

_BIG_NUMBER = re.compile(r"^[^\w]{0,3}[\d.,]+\s*[%xX]?\s*[A-Za-z]{0,3}$")


def _slide_groups(section: Section) -> list[tuple[str, list[Block]]]:
    """Split a section's blocks into (slide title, blocks) runs at each heading."""
    groups: list[tuple[str, list[Block]]] = []
    title, blocks = "", []
    for block in section.blocks:
        if block.kind == "heading":
            heading = block.text.removeprefix("Slide:").strip()
            if heading.lower() == section.title.lower() and not groups and not blocks:
                continue  # the group's own H1 is the divider, not a content slide
            if title or blocks:
                groups.append((title or section.title, blocks))
            title, blocks = heading, []
        else:
            blocks.append(block)
    if title or blocks:
        groups.append((title or section.title, blocks))
    return groups


def _emit_slide(deck, title: str, blocks: list[Block], images_dir: str | None, stats: dict) -> None:
    bullets: list[str] = []
    notes: list[str] = []
    figures: list[Block] = []
    tables: list[Block] = []
    for block in blocks:
        if block.kind == "bullet":
            bullets.extend(block.items)
        elif block.kind == "para":
            if block.text.lower().startswith("notes:"):
                notes.append(block.text[6:].strip())
            else:
                bullets.append(block.text)
        elif block.kind == "figure":
            figures.append(block)
        elif block.kind == "table":
            tables.append(block)
    note_text = "\n".join(notes)

    # Figures: the first embeds; a failed one keeps its caption as a visible line; extras
    # become their own full-bleed slides so no picture is silently dropped.
    placed = False
    for fig in figures:
        path = figure_path(images_dir, fig.fig_id)
        caption = fig.text or ""
        if path and not placed:
            if bullets:
                deck.image_right(title, bullets, path, caption, fig_id=fig.fig_id, notes=note_text)
            else:
                deck.image_full(path, caption, heading=title, fig_id=fig.fig_id, notes=note_text)
            placed = True
            stats["pictures"] += 1
        elif path:
            deck.image_full(path, caption, heading=title, fig_id=fig.fig_id)
            stats["pictures"] += 1
        else:
            bullets.append(f"[figure pending - {caption or fig.fig_id}]")
            stats["placeholders"] += 1
            # the id still has to be findable: notes carry it (precheck reads notes + alt text)
            note_text = (note_text + "\n" if note_text else "") + f"Figure ({fig.fig_id}): {caption}"

    if not placed:
        if tables and not bullets:
            made = deck.table(title, tables[0].rows, notes=note_text)
            stats["continuations"] += len(made) - 1
        elif len(bullets) == 1 and _BIG_NUMBER.match(bullets[0].strip()) and not tables:
            deck.big_number(bullets[0].strip(), title, notes=note_text)
        else:
            made = deck.bullets(title, bullets, notes=note_text)
            stats["continuations"] += len(made) - 1
    for table in tables if placed or bullets else tables[1:]:
        made = deck.table(title, table.rows)
        stats["continuations"] += len(made) - 1


def build(sections_dir: str, out: str, title: str, subtitle: str, images_dir: str | None) -> None:
    from plane_house.pptx_theme import HouseDeck  # frame-resident (ADR-034)

    sections, gaps = load_sections(sections_dir)
    if not sections:
        raise SystemExit(f"no NN-*.md sections found in {sections_dir}")

    deck = HouseDeck(title, subtitle)
    stats = {"pictures": 0, "placeholders": 0, "continuations": 0}
    by_number = {s.number: s for s in sections}
    end = max([*by_number, *gaps], default=0)
    for number in range(1, end + 1):
        section = by_number.get(number)
        if section is None:
            deck.section("Missing section", number)
            deck.paragraph("Missing section", gap_note(number))
            continue
        deck.section(section.title, number)
        for slide_title, blocks in _slide_groups(section):
            _emit_slide(deck, slide_title, blocks, images_dir, stats)
    deck.closing("Thank you", subtitle)
    deck.save(out)
    print(
        f"saved {out}: {deck.slide_count} slides, {len(sections)} groups, {len(gaps)} gap note(s), "
        f"{stats['pictures']} picture(s), {stats['placeholders']} placeholder(s), "
        f"{stats['continuations']} continuation slide(s) from density splits"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--images", default=None)
    args = ap.parse_args()
    build(args.sections, args.out, args.title, args.subtitle, args.images)
