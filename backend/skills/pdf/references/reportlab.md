# reportlab recipes for PDFs that read well

Read this when the ask goes past a title, body paragraphs, and one table - never guess an API.

## Styles worth owning

```python
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

styles = getSampleStyleSheet()          # Title, Heading1-3, BodyText, Bullet, Code
lede = ParagraphStyle("Lede", parent=styles["BodyText"], fontSize=12, leading=16,
                      spaceAfter=10, textColor="#1F3A5F")
```

`leading` (line height) trails font size by default - set it ~1.3× the size or dense text
touches itself. `spaceAfter`/`spaceBefore` on the style beats scattering `Spacer`s.

## Lists

```python
from reportlab.platypus import ListFlowable, ListItem, Paragraph
story.append(ListFlowable(
    [ListItem(Paragraph(x, styles["BodyText"])) for x in items],
    bulletType="bullet",               # or "1", "a" for numbered
))
```

Never fake a list with "• " strings - `ListFlowable` indents, wraps, and paginates correctly.

## Tables, fully dressed

```python
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle

t = Table(data, colWidths=[180, 120, 90], repeatRows=1)
t.setStyle(TableStyle([
    ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF1F5")),
    ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#B9C2CE")),
    ("ALIGN",      (1, 1), (-1, -1), "RIGHT"),      # numbers right-aligned
    ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFBFC")]),
]))
```

Cell coordinates are `(col, row)`, `-1` means last. Long prose inside a cell must itself be a
`Paragraph`, or it will not wrap.

## Page furniture (numbers, footers) - the onPage hook

```python
def stamp(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(doc.pagesize[0] - 72, 40, f"Page {doc.page}")
    canvas.drawString(72, 40, "Acme Corp - confidential")
    canvas.restoreState()

doc.build(story, onFirstPage=stamp, onLaterPages=stamp)
```

This is the honest page-number mechanism - no fields, no post-processing.

## Section breaks and keep-together

- `PageBreak()` starts a part; `KeepTogether([...])` stops a heading orphaning at a page foot.
- `CondPageBreak(2*inch)` breaks only when less than that much room remains.

## Images and charts

```python
from reportlab.platypus import Image
story.append(Image("/work/chart.png", width=6.5 * inch, height=3.2 * inch))
```

Render matplotlib at `dpi=150`+ and size the flowable in inches yourself - an unsized Image
lands at pixel size (72 dpi) and overflows the page. State every charted figure in text as
well; extraction cannot read pixels.

## Canvas patterns (when platypus is too much page for a one-pager)

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("/work/onepager.pdf", pagesize=letter)
w, h = letter                                     # origin bottom-left; y grows UP
c.setFont("Helvetica-Bold", 28); c.drawString(72, h - 96, "The headline")
c.setStrokeColor("#C9A227"); c.line(72, h - 110, w - 72, h - 110)
c.setFont("Helvetica", 11); c.drawString(72, h - 140, "Body line…")
c.showPage(); c.save()                            # exactly one save
```

`drawRightString` / `drawCentredString` align without arithmetic. Metadata:
`c.setTitle(...)`, `c.setAuthor(...)` (platypus: pass `title=`/`author=` to the template).

## Merging your build with supplied PDFs (pypdf)

```python
from pypdf import PdfReader, PdfWriter
out = PdfWriter()
for src in ("/work/cover.pdf", "/task/inputs/appendix.pdf"):
    for page in PdfReader(src).pages:
        out.add_page(page)
with open("/work/final.pdf", "wb") as fh:
    out.write(fh)
```

Build your pages with reportlab, carry theirs with pypdf, write the combined file once.
