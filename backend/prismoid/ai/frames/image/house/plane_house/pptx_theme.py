# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`HouseDeck` - a 16:9 deck in the house look, one method per slide layout (ADR-034).

Everything is positioned in inches inside the safe area; images go through `fit_box` into a
region no text box shares, so text and picture cannot overlap by construction. Figure ids are
written to speaker notes and picture alt text - never the slide face. Built on python-pptx's
default package with a programmatic master (no template file): titles stay REAL placeholders
(the preview, the checkers and the verifier read `slide.shapes.title`), bodies are text boxes
we own outright.

    from plane_house.pptx_theme import HouseDeck
    deck = HouseDeck("Q3 review", subtitle="Board pack")
    deck.section("Revenue", 1)
    deck.bullets("Services carried the quarter", ["+23% services", "Flat hardware"], notes="...")
    deck.image_right("Churn fell", ["..."], "/work/images/fig-s02-1.png", "Monthly churn", fig_id="fig-s02-1")
    deck.table("Pipeline by region", [["Region", "Q2", "Q3"], ["EMEA", "1.2", "1.4"]])
    deck.big_number("$4.2M", "Q3 revenue", "up 23% on services strength")
    deck.closing("Questions")
    deck.save("/work/deck.pptx")   # ONE save, then publish - never re-save after
"""

from __future__ import annotations

from plane_house.split import estimate_overflow, fit_box, split_bullets
from plane_house.text import numeric_columns
from plane_house.tokens import HOUSE, HOUSE_STAMP, Theme

Box = tuple[float, float, float, float]  # left, top, width, height - inches


def _rgb(hex6: str):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex6)


def image_size(path: str) -> tuple[int, int]:
    """Pixel size via Pillow (matplotlib's pin). A file Pillow cannot read reports 0×0 and
    `fit_box` fills the region - the slide still builds."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return 0, 0


def _cell_borders(cell, *, bottom: tuple[float, str] | None, top: tuple[float, str] | None) -> None:
    """python-pptx has no border API: write the a:lnL/R/T/B elements - sides off, the given
    horizontals on (the house table has no vertical rules)."""
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    tcPr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        for old in tcPr.findall(qn(tag)):
            tcPr.remove(old)
    specs = {"a:lnL": None, "a:lnR": None, "a:lnT": top, "a:lnB": bottom}
    for tag, spec in specs.items():
        ln = tcPr.makeelement(qn(tag), {})
        if spec is None:
            ln.set("w", "0")
            ln.append(ln.makeelement(qn("a:noFill"), {}))
        else:
            width, color = spec
            ln.set("w", str(int(Pt(width))))
            fill = ln.makeelement(qn("a:solidFill"), {})
            fill.append(fill.makeelement(qn("a:srgbClr"), {"val": color}))
            ln.append(fill)
        # schema order: lnL, lnR, lnT, lnB come BEFORE fill elements in a:tcPr
        tcPr.insert(list(specs).index(tag), ln)


class HouseDeck:
    def __init__(self, title: str, subtitle: str = "", theme: Theme = HOUSE, author: str = "") -> None:
        from pptx import Presentation
        from pptx.util import Inches

        self.theme = theme
        self.title_text = title
        self.prs = Presentation()
        self.prs.slide_width = Inches(theme.slide_w)
        self.prs.slide_height = Inches(theme.slide_h)
        self.prs.core_properties.title = title
        self.prs.core_properties.category = HOUSE_STAMP  # the layout check's proof this is a house deck
        if author:
            self.prs.core_properties.author = author
        self._content_count = 0
        self.title(title, subtitle)

    # ── geometry ────────────────────────────────────────────────────────────────────────────
    @property
    def m(self) -> float:
        return self.theme.slide_margin

    @property
    def content_box(self) -> Box:
        """The region under the title band and above the footer."""
        t = self.theme
        return (self.m, t.content_top, t.slide_w - 2 * self.m, t.slide_h - t.content_top - t.footer_h)

    def _halves(self, gap: float = 0.4) -> tuple[Box, Box]:
        left, top, w, h = self.content_box
        half = (w - gap) / 2
        return (left, top, half, h), (left + half + gap, top, half, h)

    # ── primitives ──────────────────────────────────────────────────────────────────────────
    def _slide(self, layout_index: int = 5):
        return self.prs.slides.add_slide(self.prs.slide_layouts[layout_index])

    def _fill(self, slide, hex6: str) -> None:
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb(hex6)

    def _title(
        self,
        slide,
        text: str,
        *,
        size: int | None = None,
        color: str | None = None,
        box: Box | None = None,
        align: str = "left",
    ) -> None:
        """Style the slide's REAL title placeholder: explicit position, one run."""
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Inches, Pt

        shape = slide.shapes.title
        if shape is None:
            shape = slide.shapes.add_textbox(
                Inches(self.m), Inches(0.5), Inches(self.theme.slide_w - 2 * self.m), Inches(1.0)
            )
        left, top, w, h = box or (
            self.m,
            self.theme.title_top,
            self.theme.slide_w - 2 * self.m,
            self.theme.title_h,
        )
        shape.left, shape.top, shape.width, shape.height = Inches(left), Inches(top), Inches(w), Inches(h)
        tf = shape.text_frame
        tf.clear()
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE if align == "center" else MSO_ANCHOR.BOTTOM
        para = tf.paragraphs[0]
        para.alignment = PP_ALIGN.CENTER if align == "center" else PP_ALIGN.LEFT
        run = para.add_run()
        run.text = text
        run.font.name = self.theme.font_head
        run.font.size = Pt(size or self.theme.deck.title)
        run.font.bold = True
        run.font.color.rgb = _rgb(color or self.theme.palette.primary)

    def _textbox(
        self,
        slide,
        box: Box,
        paragraphs: list[tuple[str, int, float, str, bool]],
        *,
        anchor: str = "top",
        align: str = "left",
    ):
        """A text box we own: `paragraphs` = (text, level, size_pt, color_hex, bold)."""
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Inches, Pt

        left, top, w, h = box
        shape = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(w), Inches(h))
        tf = shape.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = {
            "top": MSO_ANCHOR.TOP,
            "middle": MSO_ANCHOR.MIDDLE,
            "bottom": MSO_ANCHOR.BOTTOM,
        }[anchor]
        for n, (text, level, size, color, bold) in enumerate(paragraphs):
            para = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
            para.level = level
            para.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[
                align
            ]
            para.space_after = Pt(size * 0.45)
            run = para.add_run()
            run.text = text
            run.font.name = self.theme.font_body
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = _rgb(color)
        return shape

    def _bullets_box(
        self, slide, box: Box, items: list[str], size: int | None = None, color: str | None = None
    ):
        """Real bullet paragraphs (buChar via XML - python-pptx text boxes have no bullets by
        default, and a typed "•" is the fake-list tell the docx skill forbids)."""
        from pptx.oxml.ns import qn
        from pptx.util import Inches

        size = size or self.theme.deck.body
        shape = self._textbox(
            slide, box, [(item, 0, size, color or self.theme.palette.ink, False) for item in items]
        )
        for para in shape.text_frame.paragraphs:
            pPr = para._p.get_or_add_pPr()
            pPr.set("marL", str(int(Inches(0.3))))
            pPr.set("indent", str(int(-Inches(0.3))))
            for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
                for old in pPr.findall(qn(tag)):
                    pPr.remove(old)
            bu_clr = pPr.makeelement(qn("a:buClr"), {})
            srgb = bu_clr.makeelement(qn("a:srgbClr"), {"val": self.theme.palette.accent})
            bu_clr.append(srgb)
            pPr.append(bu_clr)
            pPr.append(pPr.makeelement(qn("a:buChar"), {"char": "•"}))
        return shape

    def _picture(self, slide, path: str, box: Box, fig_id: str = "", *, anchor: str = "center"):
        from pptx.util import Inches

        w, h = image_size(path)
        left, top, pw, ph = fit_box(w, h, box)
        if anchor == "top":
            top = box[1]
        pic = slide.shapes.add_picture(path, Inches(left), Inches(top), Inches(pw), Inches(ph))
        if fig_id:
            pic._element.nvPicPr.cNvPr.set("descr", fig_id)  # alt text: the id's home on the face
        return pic

    def _caption(self, slide, box: Box, text: str):
        return self._textbox(
            slide, box, [(text, 0, self.theme.deck.caption, self.theme.palette.muted, False)]
        )

    def _footer(self, slide, dark: bool = False) -> None:
        color = self.theme.palette.paper if dark else self.theme.palette.muted
        y = self.theme.slide_h - self.theme.footer_h + 0.05
        self._textbox(
            slide,
            (self.m, y, 8.0, 0.35),
            [(self.theme.footer_text or self.title_text, 0, self.theme.deck.footer, color, False)],
            anchor="middle",
        )
        self._textbox(
            slide,
            (self.theme.slide_w - self.m - 1.2, y, 1.2, 0.35),
            [(str(len(self.prs.slides)), 0, self.theme.deck.footer, color, False)],
            anchor="middle",
            align="right",
        )

    def _rule(self, slide, y: float) -> None:
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches, Pt

        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(self.m), Inches(y), Inches(1.2), Pt(3))
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(self.theme.palette.accent)
        shape.line.fill.background()
        shape.name = "accent rule"

    def notes(self, slide, text: str) -> None:
        if not text:
            return
        frame = slide.notes_slide.notes_text_frame
        frame.text = (frame.text + "\n" + text).strip() if frame.text else text

    def _content(self, heading: str, notes: str = ""):
        slide = self._slide(5)
        self._title(slide, heading)
        self._rule(slide, self.theme.title_top + self.theme.title_h + 0.05)
        self._footer(slide)
        self.notes(slide, notes)
        self._content_count += 1
        return slide

    # ── layouts ─────────────────────────────────────────────────────────────────────────────
    def title(self, title: str, subtitle: str = ""):
        slide = self._slide(0)
        self._fill(slide, self.theme.palette.primary)
        self._title(
            slide,
            title,
            size=44,
            color=self.theme.palette.paper,
            box=(self.m, 2.2, self.theme.slide_w - 2 * self.m, 1.8),
        )
        # python-pptx's layout 0 carries a subtitle placeholder; we own the text box instead and
        # remove the placeholder so the checker never sees an empty one.
        for shape in list(slide.placeholders):
            if shape.placeholder_format.idx != 0:
                shape._element.getparent().remove(shape._element)
        if subtitle:
            self._textbox(
                slide,
                (self.m, 4.1, self.theme.slide_w - 2 * self.m, 1.0),
                [(subtitle, 0, self.theme.deck.subtitle, self.theme.palette.surface, False)],
            )
        return slide

    def section(self, title: str, number: int | None = None, notes: str = ""):
        slide = self._slide(5)
        self._fill(slide, self.theme.palette.primary)
        if number is not None:
            self._textbox(
                slide,
                (self.m, 1.9, 4.0, 0.8),
                [(f"{number:02d}", 0, self.theme.deck.subtitle, self.theme.palette.accent, True)],
            )
        self._title(
            slide,
            title,
            size=self.theme.deck.section,
            color=self.theme.palette.paper,
            box=(self.m, 2.6, self.theme.slide_w - 2 * self.m, 1.8),
        )
        self._footer(slide, dark=True)
        self.notes(slide, notes)
        return slide

    def bullets(self, heading: str, items: list[str], notes: str = "", box: Box | None = None) -> list:
        """One or MORE slides: past the density caps the bullets split onto `(cont.)` slides.
        Returns every slide made, so the caller can count continuations honestly."""
        chunks = split_bullets(items, self.theme.max_bullets_per_slide, self.theme.max_words_per_slide) or [
            []
        ]
        slides = []
        for n, chunk in enumerate(chunks):
            slide = self._content(heading if n == 0 else f"{heading} (cont.)", notes if n == 0 else "")
            region = box or self.content_box
            size = self.theme.deck.body
            if chunk and estimate_overflow("\n".join(chunk), region[2], region[3], size):
                size = (
                    self.theme.deck.body_min
                )  # one step down, never further - the caps should have split first
            if chunk:
                self._bullets_box(slide, region, chunk, size=size)
            slides.append(slide)
        return slides

    def paragraph(self, heading: str, text: str, notes: str = ""):
        slide = self._content(heading, notes)
        self._textbox(
            slide, self.content_box, [(text, 0, self.theme.deck.body, self.theme.palette.ink, False)]
        )
        return slide

    def image_right(
        self,
        heading: str,
        items: list[str],
        image_path: str,
        caption: str = "",
        *,
        fig_id: str = "",
        notes: str = "",
    ):
        return self._image_beside(
            heading, items, image_path, caption, fig_id=fig_id, notes=notes, image_on="right"
        )

    def image_left(
        self,
        heading: str,
        items: list[str],
        image_path: str,
        caption: str = "",
        *,
        fig_id: str = "",
        notes: str = "",
    ):
        return self._image_beside(
            heading, items, image_path, caption, fig_id=fig_id, notes=notes, image_on="left"
        )

    def _image_beside(self, heading, items, image_path, caption, *, fig_id, notes, image_on):
        """Returns the slide carrying the picture. Bullets past the density caps are NOT dropped:
        they continue on `(cont.)` bullet slides after it (the half-width box holds less than a
        full one, so the split is against the half)."""
        slide = self._content(heading, notes)
        a, b = self._halves()
        text_box, img_box = (a, b) if image_on == "right" else (b, a)
        left, top, w, h = img_box
        cap_h = 0.5 if caption else 0.0
        pic = self._picture(slide, image_path, (left, top, w, h - cap_h), fig_id=fig_id, anchor="top")
        if caption:
            pic_bottom = (pic.top + pic.height) / 914400
            self._caption(slide, (left, pic_bottom + 0.1, w, cap_h - 0.05), caption)
        chunks = (
            split_bullets(items, self.theme.max_bullets_per_slide, self.theme.max_words_per_slide // 2)
            if items
            else []
        )
        if chunks:
            size = self.theme.deck.body
            if estimate_overflow("\n".join(chunks[0]), text_box[2], text_box[3], size):
                size = self.theme.deck.body_min
            self._bullets_box(slide, text_box, chunks[0], size=size)
        if fig_id:
            self.notes(slide, f"Figure ({fig_id}): {caption}".strip())
        for chunk in chunks[1:]:
            self._bullets_box(self._content(f"{heading} (cont.)"), self.content_box, chunk)
        return slide

    def image_full(
        self, image_path: str, caption: str = "", *, heading: str = "", fig_id: str = "", notes: str = ""
    ):
        """A picture owning the slide, the caption in a bar beneath. With `heading`, the title
        band stays; without it, the image takes the safe area."""
        if heading:
            slide = self._content(heading, notes)
            left, top, w, h = self.content_box
        else:
            slide = self._slide(6)
            self._footer(slide)
            self.notes(slide, notes)
            self._content_count += 1
            left, top, w, h = (
                self.m,
                self.m,
                self.theme.slide_w - 2 * self.m,
                self.theme.slide_h - self.m - 0.75,
            )
        cap_h = 0.55 if caption else 0.0
        self._picture(slide, image_path, (left, top, w, h - cap_h), fig_id=fig_id)
        if caption:
            self._caption(slide, (left, top + h - cap_h + 0.05, w, cap_h - 0.05), caption)
        if fig_id:
            self.notes(slide, f"Figure ({fig_id}): {caption}".strip())
        return slide

    def two_column(
        self,
        heading: str,
        left_items: list[str],
        right_items: list[str],
        *,
        left_title: str = "",
        right_title: str = "",
        notes: str = "",
    ):
        """Two bullet columns. Each column holds at most `max_bullets_per_slide` items - past
        that, make a second two-column slide; this layout never splits for you."""
        slide = self._content(heading, notes)
        for box, title, items in (
            (self._halves()[0], left_title, left_items),
            (self._halves()[1], right_title, right_items),
        ):
            left, top, w, h = box
            if title:
                self._textbox(
                    slide,
                    (left, top, w, 0.5),
                    [(title, 0, self.theme.deck.subtitle, self.theme.palette.primary, True)],
                )
                top, h = top + 0.55, h - 0.55
            cap = self.theme.max_bullets_per_slide
            self._bullets_box(
                slide,
                (left, top, w, h),
                items[:cap],
                size=self.theme.deck.body_min if len(items) > 4 else self.theme.deck.body,
            )
        return slide

    def table(
        self, heading: str, rows: list[list[str]], *, col_widths: list[float] | None = None, notes: str = ""
    ) -> list:
        """A NATIVE table (header row + body), continued onto `(cont.)` slides past the row cap
        with the header repeated. Column widths from content unless given (inches)."""
        from pptx.util import Inches, Pt

        if not rows:
            return [self._content(heading, notes)]
        header, body = rows[0], rows[1:]
        cols = max(len(r) for r in rows)
        left, top, w, _h = self.content_box
        if col_widths is None:
            weights = [max(len(r[c]) if c < len(r) else 0 for r in rows) + 4 for c in range(cols)]
            total = float(sum(weights)) or 1.0
            col_widths = [w * wt / total for wt in weights]
        cap = self.theme.max_table_rows_per_slide
        pages = [body[i : i + cap] for i in range(0, len(body), cap)] or [[]]
        ts = self.theme.table
        numeric = numeric_columns(rows, cols)
        slides = []
        for n, page in enumerate(pages):
            slide = self._content(heading if n == 0 else f"{heading} (cont.)", notes if n == 0 else "")
            shape = slide.shapes.add_table(
                len(page) + 1,
                cols,
                Inches(left),
                Inches(top),
                Inches(w),
                Inches(ts.deck_row_h_in * (len(page) + 1)),
            )
            table = shape.table
            tblPr = table._tbl.tblPr
            tblPr.set("bandRow", "0")
            tblPr.set("firstRow", "0")
            for c, cw in enumerate(col_widths):
                table.columns[c].width = Inches(cw)
            for r, row in enumerate([header, *page]):
                for c in range(cols):
                    cell = table.cell(r, c)
                    cell.text = ""
                    cell.margin_top = cell.margin_bottom = Pt(ts.cell_pad_pt)
                    cell.margin_left = cell.margin_right = Pt(ts.cell_side_pad_pt)
                    para = cell.text_frame.paragraphs[0]
                    if numeric[c]:  # the header follows its numbers
                        from pptx.enum.text import PP_ALIGN

                        para.alignment = PP_ALIGN.RIGHT
                    run = para.add_run()
                    run.text = row[c] if c < len(row) else ""
                    run.font.name = self.theme.font_body
                    run.font.size = Pt(ts.deck_font_pt)
                    run.font.bold = r == 0
                    run.font.color.rgb = _rgb(ts.header_text if r == 0 else self.theme.palette.ink)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = _rgb(ts.header_fill if r == 0 else self.theme.palette.paper)
                    _cell_borders(
                        cell,
                        bottom=(ts.header_rule_pt, ts.header_text)
                        if r == 0
                        else (ts.row_rule_pt, ts.rule_color),
                        top=(ts.row_rule_pt, ts.rule_color) if r == 0 else None,
                    )
            slides.append(slide)
        return slides

    def big_number(self, value: str, label: str, support: str = "", *, heading: str = "", notes: str = ""):
        slide = self._content(heading or label, notes)
        left, top, w, _h = self.content_box
        self._textbox(
            slide,
            (left, top + 0.3, w, 1.6),
            [(value, 0, self.theme.deck.big_number, self.theme.palette.accent, True)],
            anchor="middle",
            align="center",
        )
        self._textbox(
            slide,
            (left, top + 2.0, w, 0.7),
            [(label, 0, self.theme.deck.subtitle, self.theme.palette.primary, True)],
            align="center",
        )
        if support:
            self._textbox(
                slide,
                (left + w * 0.15, top + 2.8, w * 0.7, 1.2),
                [(support, 0, self.theme.deck.body, self.theme.palette.muted, False)],
                align="center",
            )
        return slide

    def closing(self, text: str = "Thank you", sub: str = ""):
        slide = self._slide(5)
        self._fill(slide, self.theme.palette.primary)
        self._title(
            slide,
            text,
            size=self.theme.deck.section,
            color=self.theme.palette.paper,
            box=(self.m, 2.6, self.theme.slide_w - 2 * self.m, 1.6),
            align="center",
        )
        if sub:
            self._textbox(
                slide,
                (self.m, 4.3, self.theme.slide_w - 2 * self.m, 0.8),
                [(sub, 0, self.theme.deck.subtitle, self.theme.palette.surface, False)],
                align="center",
            )
        self._footer(slide, dark=True)
        return slide

    # ── out ─────────────────────────────────────────────────────────────────────────────────
    @property
    def slide_count(self) -> int:
        return len(self.prs.slides)

    def save(self, path: str) -> None:
        """ONE save. Publish after; never re-save (the harvest hash law)."""
        self.prs.save(path)
