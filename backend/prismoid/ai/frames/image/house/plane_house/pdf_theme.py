# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The reportlab twin - the FALLBACK engine when LibreOffice cannot convert the house docx
(ADR-034). Helvetica only (no embedded system fonts), header/footer with page numbers,
tables with repeated headers and column widths, figures kept with their captions.

    from plane_house.pdf_theme import HouseDocTemplate, house_styles, table, figure
    doc = HouseDocTemplate("/work/report.pdf", title="The Report")
    styles = house_styles()
    doc.build([Paragraph("...", styles["Body"]), table([...]), figure("/work/images/f.png", "caption", 1)])
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from plane_house.split import fit_box
from plane_house.text import figure_caption, numeric_columns
from plane_house.tokens import HOUSE, Theme


def _color(hex6: str):
    from reportlab.lib import colors

    return colors.HexColor("#" + hex6)


def house_styles(theme: Theme = HOUSE):
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    s = theme.doc
    p = theme.palette
    styles = {
        "Body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=s.body,
            leading=s.leading,
            textColor=_color(p.ink),
            spaceAfter=6,
        ),
        "Title": ParagraphStyle(
            "HouseTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=s.cover_title,
            leading=s.cover_title * 1.2,
            textColor=_color(p.primary),
            alignment=0,
            spaceAfter=12,
        ),
        "Subtitle": ParagraphStyle(
            "HouseSubtitle",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=s.cover_subtitle,
            textColor=_color(p.muted),
            spaceAfter=6,
        ),
        "Heading1": ParagraphStyle(
            "HouseH1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=s.h1,
            leading=s.h1 * 1.25,
            textColor=_color(p.primary),
            spaceBefore=20,
            spaceAfter=8,
            keepWithNext=True,
        ),
        "Heading2": ParagraphStyle(
            "HouseH2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=s.h2,
            leading=s.h2 * 1.25,
            textColor=_color(p.primary),
            spaceBefore=12,
            spaceAfter=6,
            keepWithNext=True,
        ),
        "Heading3": ParagraphStyle(
            "HouseH3",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=s.h3,
            leading=s.h3 * 1.25,
            textColor=_color(p.ink),
            spaceBefore=8,
            spaceAfter=4,
            keepWithNext=True,
        ),
        "Caption": ParagraphStyle(
            "HouseCaption",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=s.caption,
            leading=s.caption * 1.3,
            textColor=_color(p.muted),
            alignment=TA_CENTER,
            spaceAfter=10,
        ),
        "Cell": ParagraphStyle(
            "HouseCell",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
            textColor=_color(p.ink),
        ),
        "CellHead": ParagraphStyle(
            "HouseCellHead",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            # The Paragraph's own colour outranks a TableStyle TEXTCOLOR: paper-white here was
            # invisible on the light header band (TableStyle tokens). Header ink, explicitly.
            textColor=_color(theme.table.header_text),
        ),
    }
    return styles


class HouseDocTemplate:  # a thin factory - reportlab's BaseDocTemplate is built lazily
    def __init__(self, path: str, *, title: str, theme: Theme = HOUSE, pagesize=None) -> None:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

        self.theme = theme
        self.title = title
        size = pagesize or LETTER
        m = theme.page_margin * inch  # top/bottom
        sm = theme.page_side_margin * inch  # left/right - the docx twin's reading measure
        self._doc = BaseDocTemplate(
            path, pagesize=size, title=title, leftMargin=sm, rightMargin=sm, topMargin=m, bottomMargin=m
        )
        frame = Frame(
            sm,
            m,
            size[0] - 2 * sm,
            size[1] - 2 * m,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self._doc.addPageTemplates([PageTemplate(id="house", frames=[frame], onPage=self._furniture)])

    def _furniture(self, canvas, doc) -> None:
        from reportlab.lib.units import inch

        p = self.theme.palette
        w, _ = doc.pagesize
        m = self.theme.page_side_margin * inch
        canvas.saveState()
        canvas.setFont("Helvetica", self.theme.footer_pt)
        canvas.setFillColor(_color(p.muted))
        canvas.drawRightString(w - m, 0.55 * inch, f"{self.theme.footer_text or self.title}   ·   {doc.page}")
        canvas.setStrokeColor(_color(p.rule))
        canvas.setLineWidth(0.5)
        canvas.line(m, 0.75 * inch, w - m, 0.75 * inch)
        canvas.restoreState()

    def build(self, story) -> None:
        self._doc.build(story)


def table(rows: list[list[str]], col_widths: list[float] | None = None, theme: Theme = HOUSE, styles=None):
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    styles = styles or house_styles(theme)
    if not rows:
        return Paragraph("", styles["Body"])
    cols = max(len(r) for r in rows)
    usable = theme.usable_w
    if col_widths is None:
        weights = [max(len(r[c]) if c < len(r) else 0 for r in rows) + 3 for c in range(cols)]
        total = float(sum(weights)) or 1.0
        col_widths = [usable * w / total for w in weights]
    data = [
        [
            Paragraph(escape(r[c] if c < len(r) else ""), styles["CellHead" if i == 0 else "Cell"])
            for c in range(cols)
        ]
        for i, r in enumerate(rows)
    ]
    t = Table(data, colWidths=[w * inch for w in col_widths], repeatRows=1, hAlign="LEFT")
    ts = theme.table
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _color(ts.header_fill)),
        ("TEXTCOLOR", (0, 0), (-1, 0), _color(ts.header_text)),
        ("LINEBELOW", (0, 0), (-1, 0), ts.header_rule_pt, _color(ts.header_text)),
        ("LINEBELOW", (0, 1), (-1, -1), ts.row_rule_pt, _color(ts.rule_color)),
        ("LINEABOVE", (0, 0), (-1, 0), ts.row_rule_pt, _color(ts.rule_color)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), ts.cell_pad_pt),
        ("BOTTOMPADDING", (0, 0), (-1, -1), ts.cell_pad_pt),
        ("LEFTPADDING", (0, 0), (-1, -1), ts.cell_side_pad_pt),
        ("RIGHTPADDING", (0, 0), (-1, -1), ts.cell_side_pad_pt),
    ]
    for c, numeric in enumerate(numeric_columns(rows, cols)):
        if numeric:
            style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if ts.zebra:
        for r in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, r), (-1, r), _color(theme.palette.surface)))
    t.setStyle(TableStyle(style))
    return t


def figure(path: str, caption: str, number: int, theme: Theme = HOUSE, styles=None):
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, KeepTogether, Paragraph, Spacer

    styles = styles or house_styles(theme)
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            w, h = img.width, img.height
    except Exception:
        w, h = 0, 0
    _, _, pw, ph = fit_box(w, h, (0, 0, theme.figure_max_w, theme.figure_max_h))
    cap = Paragraph(escape(figure_caption(number, caption)), styles["Caption"])
    return KeepTogether([Image(path, width=pw * inch, height=ph * inch), Spacer(1, 4), cap])


def figure_placeholder(caption: str, number: int, theme: Theme = HOUSE, styles=None):
    from reportlab.platypus import Paragraph

    styles = styles or house_styles(theme)
    return Paragraph(escape(figure_caption(number, caption, pending=True)), styles["Caption"])
