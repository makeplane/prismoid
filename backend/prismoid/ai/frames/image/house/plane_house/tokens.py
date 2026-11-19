# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one house identity - every number a builder needs, in one place (ADR-034).

A `Theme` is a value; `HOUSE` is the default. Every builder takes `theme: Theme = HOUSE` so a
per-Project override is a later argument, not a later refactor - seam only, nothing wires it yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Palette:
    ink: str = "1F2933"  # body text
    paper: str = "FFFFFF"  # page / slide ground
    primary: str = "1F3A5F"  # headings, title slides, table headers
    accent: str = "C9A227"  # the one highlight (big numbers, rules under titles)
    rule: str = "D9DEE3"  # hairlines, table grid
    muted: str = "6B7785"  # captions, footers, notes
    surface: str = "F3F5F7"  # zebra rows, caption bars


@dataclass(frozen=True, slots=True)
class DeckScale:
    title: int = 36
    section: int = 40
    subtitle: int = 20
    body: int = 18
    body_min: int = 14  # never shrink below this - split the slide instead
    caption: int = 11
    footer: int = 10
    big_number: int = 72
    table: int = 14


@dataclass(frozen=True, slots=True)
class DocScale:
    body: float = 11
    leading: float = 14  # points between baselines in body text
    h1: float = 20
    h2: float = 15
    h3: float = 12.5
    caption: float = 9.5
    cover_title: float = 28
    cover_subtitle: float = 14


@dataclass(frozen=True, slots=True)
class TableStyle:
    """The house table: no vertical rules, a rule under the header, hairlines between rows,
    numbers right-aligned, room to breathe. Shared by docx, pptx and pdf."""

    header_fill: str = "F3F5F7"  # surface - the header sits on a light band, not a navy bar
    header_text: str = "1F3A5F"  # primary
    header_rule_pt: float = 1.5  # the one heavy line, under the header
    row_rule_pt: float = 0.5  # hairline between body rows
    rule_color: str = "D9DEE3"
    zebra: bool = False
    cell_pad_pt: float = 5.0  # top/bottom padding inside a cell
    cell_side_pad_pt: float = 7.0
    font_pt: float = 10.0  # documents
    deck_font_pt: float = 14.0  # slides
    deck_row_h_in: float = 0.46


@dataclass(frozen=True, slots=True)
class Theme:
    name: str = "house"
    palette: Palette = field(default_factory=Palette)
    # Inter, chosen by eye against Lato/Open Sans/Calibri on rendered pages (2026-08-20). The
    # frame image installs it (fonts-inter), so every PDF rendition embeds it exactly; a .docx
    # or .pptx opened on a machine without Inter falls back to the reader's sans.
    font_body: str = "Inter"
    font_head: str = "Inter"
    deck: DeckScale = field(default_factory=DeckScale)
    doc: DocScale = field(default_factory=DocScale)
    table: TableStyle = field(default_factory=TableStyle)
    # Deck canvas (inches) and the safe area inside it; the title band above the content.
    slide_w: float = 13.333
    slide_h: float = 7.5
    slide_margin: float = 0.5
    title_top: float = 0.4
    title_h: float = 0.95
    content_top: float = 1.55
    footer_h: float = 0.6
    # Document page furniture (inches / points).
    page_w: float = 8.5
    page_h: float = 11.0
    page_margin: float = 0.8  # top/bottom
    page_side_margin: float = 0.9  # left/right - a reading measure, not a frame
    cover_top_in: float = 2.6  # where the cover title sits
    figure_max_w: float = 6.0
    figure_max_h: float = 7.0
    body_space_after_pt: float = 6.0
    list_space_after_pt: float = 3.0
    footer_pt: float = 8.5
    # Density caps: past these the builders SPLIT, never shrink.
    max_bullets_per_slide: int = 6
    max_words_per_slide: int = 70
    max_table_rows_per_slide: int = 10
    footer_text: str = ""  # empty = the deliverable's own title

    @property
    def usable_w(self) -> float:
        return self.page_w - 2 * self.page_side_margin


HOUSE = Theme()

# The stamp every house-built file carries in its core properties (`category`): the layout
# check reads it back - "built on the house" is a floor, not advice.
HOUSE_STAMP = "plane_house/1"

# Aliases the plan and the skills name directly.
SLIDE_W, SLIDE_H = HOUSE.slide_w, HOUSE.slide_h
MAX_BULLETS_PER_SLIDE = HOUSE.max_bullets_per_slide
MAX_WORDS_PER_SLIDE = HOUSE.max_words_per_slide
