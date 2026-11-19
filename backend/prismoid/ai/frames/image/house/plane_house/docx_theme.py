# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The house Word document (ADR-034): styles set once, a cover, page numbers in the footer,
tables with widths on every cell and a repeating shaded header, figures numbered on the face
with their ids in alt text - never in the caption a customer reads.

    from plane_house.docx_theme import house_document, heading, add_table, add_figure
    doc = house_document("Network move checklist", subtitle="August 2026", author="Plane")
    heading(doc, "Scope", 1, first=True)
    doc.add_paragraph("...")
    add_table(doc, [["Item", "Owner"], ["Racks", "Ops"]])
    add_figure(doc, "/work/images/fig-s01-1.png", "Rack layout", number=1, fig_id="fig-s01-1")
    doc.save("/work/report.docx")   # ONE save
"""

from __future__ import annotations

from plane_house.split import fit_box
from plane_house.text import figure_caption, numeric_columns
from plane_house.tokens import HOUSE, HOUSE_STAMP, Theme


def _rgb(hex6: str):
    from docx.shared import RGBColor

    return RGBColor.from_string(hex6)


def _set_font(
    style, name: str, size_pt: float, *, bold: bool | None = None, color: str | None = None
) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    style.font.name = name
    style.font.size = Pt(size_pt)
    if bold is not None:
        style.font.bold = bold
    if color:
        style.font.color.rgb = _rgb(color)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attr), name)
    # The default template's heading styles name a THEME font (`w:asciiTheme="majorHAnsi"`),
    # and a theme attribute outranks the explicit name - every heading rendered in the theme's
    # Calibri whatever the Theme said (found rendering the font candidates, 2026-08-20).
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if fonts.get(qn(attr)) is not None:
            del fonts.attrib[qn(attr)]


def add_page_number(paragraph) -> None:
    """A real PAGE field (python-docx has no API for fields; this is the fldSimple XML)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    fld.append(run)
    paragraph._p.append(fld)


def house_document(
    title: str,
    subtitle: str = "",
    author: str = "",
    theme: Theme = HOUSE,
    *,
    cover: bool = True,
    date: str = "",
):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    doc = docx.Document()
    p = theme.palette
    s = theme.doc

    normal = doc.styles["Normal"]
    _set_font(normal, theme.font_body, s.body, color=p.ink)
    normal.paragraph_format.space_after = Pt(theme.body_space_after_pt)
    # A MULTIPLE, never an exact Pt: exact line spacing clips an inline picture to one line
    # height (a 6×3.4in figure rendered as a 14pt bar - found on the first rendered page).
    normal.paragraph_format.line_spacing = round(s.leading / s.body, 2)

    for name, size, space_before in (
        ("Heading 1", s.h1, 24),
        ("Heading 2", s.h2, 14),
        ("Heading 3", s.h3, 10),
    ):
        style = doc.styles[name]
        _set_font(style, theme.font_head, size, bold=True, color=p.primary)
        style.paragraph_format.space_before = Pt(space_before)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True
    _set_font(doc.styles["Title"], theme.font_head, s.cover_title, bold=True, color=p.primary)
    _set_font(doc.styles["Caption"], theme.font_body, s.caption, color=p.muted)
    doc.styles["Caption"].font.italic = False
    for name in ("List Bullet", "List Number"):
        doc.styles[name].paragraph_format.space_after = Pt(theme.list_space_after_pt)

    section = doc.sections[0]
    section.page_width, section.page_height = Inches(theme.page_w), Inches(theme.page_h)
    section.top_margin = section.bottom_margin = Inches(theme.page_margin)
    section.left_margin = section.right_margin = Inches(theme.page_side_margin)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = footer.add_run((theme.footer_text or title) + "   ·   ")
    run.font.size = Pt(theme.footer_pt)
    run.font.color.rgb = _rgb(p.muted)
    add_page_number(footer)

    doc.core_properties.title = title
    doc.core_properties.category = HOUSE_STAMP  # the layout check's proof this is a house file
    if author:
        doc.core_properties.author = author
    if subtitle:
        doc.core_properties.subject = subtitle

    if cover:
        t = doc.add_paragraph(style="Title")
        t.paragraph_format.space_before = Inches(theme.cover_top_in)  # spacing, not blank lines
        t.add_run(title)
        if subtitle:
            sp = doc.add_paragraph()
            r = sp.add_run(subtitle)
            r.font.size = Pt(s.cover_subtitle)
            r.font.color.rgb = _rgb(p.muted)
        if date:
            dp = doc.add_paragraph()
            r = dp.add_run(date)
            r.font.size = Pt(s.cover_subtitle - 2)
            r.font.color.rgb = _rgb(p.muted)
        doc.add_page_break()
    else:
        doc.add_paragraph(style="Title").add_run(title)
        if subtitle:
            doc.add_paragraph(subtitle)
    return doc


def heading(doc, text: str, level: int = 1, *, first: bool = False):
    """A real heading. No page break per section: H1 gets its project from the style; only the
    first body section after front matter (`first=True`) starts a fresh page."""
    para = doc.add_heading(text, level=max(1, min(level, 3)))
    if first and level == 1:
        para.paragraph_format.page_break_before = True
    return para


def add_table(doc, rows: list[list[str]], widths: list[float] | None = None, theme: Theme = HOUSE):
    """The house table (TableStyle): no vertical rules, a light header band with one heavy rule
    under it, hairlines between rows, numbers right-aligned, padding on every cell, widths on
    EVERY cell (Word ignores them anywhere else), header row repeated across pages."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    if not rows:
        return None
    t = theme.table
    cols = max(len(r) for r in rows)
    if widths is None:
        weights = [max(len(r[c]) if c < len(r) else 0 for r in rows) + 3 for c in range(cols)]
        total = float(sum(weights)) or 1.0
        widths = [theme.usable_w * w / total for w in weights]
    numeric = numeric_columns(rows, cols)

    table = doc.add_table(rows=len(rows), cols=cols)
    table.autofit = False
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge, sz, color in (
        ("top", t.row_rule_pt, t.rule_color),
        ("bottom", t.row_rule_pt, t.rule_color),
        ("insideH", t.row_rule_pt, t.rule_color),
    ):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(int(sz * 8)))  # eighths of a point
        el.set(qn("w:color"), color)
        borders.append(el)
    for edge in ("left", "right", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "nil")
        borders.append(el)
    tblPr.append(borders)
    mar = OxmlElement("w:tblCellMar")
    for side, pts in (
        ("top", t.cell_pad_pt),
        ("bottom", t.cell_pad_pt),
        ("left", t.cell_side_pad_pt),
        ("right", t.cell_side_pad_pt),
    ):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(int(pts * 20)))  # twips
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tblPr.append(mar)

    for r, row in enumerate(rows):
        tr = table.rows[r]
        if r == 0:
            tr._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for c in range(cols):
            cell = tr.cells[c]
            cell.width = Inches(widths[c])
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.space_before = Pt(0)
            if numeric[c]:  # the header follows its numbers
                para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            run = para.add_run(row[c] if c < len(row) else "")
            run.font.size = Pt(t.font_pt)
            run.font.bold = r == 0
            tcPr = cell._tc.get_or_add_tcPr()
            if r == 0:
                run.font.color.rgb = _rgb(t.header_text)
                _shade(tcPr, t.header_fill)
                tcb = OxmlElement("w:tcBorders")
                bottom = OxmlElement("w:bottom")
                bottom.set(qn("w:val"), "single")
                bottom.set(qn("w:sz"), str(int(t.header_rule_pt * 8)))
                bottom.set(qn("w:color"), t.header_text)
                tcb.append(bottom)
                tcPr.append(tcb)
            elif t.zebra and r % 2 == 0:
                _shade(tcPr, theme.palette.surface)
    doc.add_paragraph()  # breathing room after the table
    return table


def _shade(tcPr, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def add_figure(doc, path: str, caption: str, number: int, *, fig_id: str = "", theme: Theme = HOUSE):
    """A picture aspect-fit inside the figure box, the caption `Figure N. caption` beneath in
    Caption style, the id in the picture's alt text + the document keywords (precheck reads
    both) - never on the face."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches

    try:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.width, img.height
    except Exception:
        w, h = 0, 0
    _, _, pw, ph = fit_box(w, h, (0, 0, theme.figure_max_w, theme.figure_max_h))
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.keep_with_next = True
    para.paragraph_format.line_spacing = 1.0
    para.add_run().add_picture(path, width=Inches(pw), height=Inches(ph) if h else None)
    if fig_id:
        shape = doc.inline_shapes[-1]
        shape._inline.docPr.set("descr", fig_id)
        kw = doc.core_properties.keywords or ""
        doc.core_properties.keywords = (kw + ", " if kw else "") + fig_id
    cap = doc.add_paragraph(style="Caption")
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.add_run(figure_caption(number, caption))
    return para


def figure_placeholder(doc, caption: str, number: int, *, fig_id: str = ""):
    """The degrade law: a figure that never landed keeps its numbered caption on the face and
    its id in the keywords, so the reader sees an honest gap and QA still finds the id."""
    cap = doc.add_paragraph(style="Caption")
    cap.add_run(figure_caption(number, caption, pending=True))
    if fig_id:
        kw = doc.core_properties.keywords or ""
        doc.core_properties.keywords = (kw + ", " if kw else "") + fig_id
    return cap
