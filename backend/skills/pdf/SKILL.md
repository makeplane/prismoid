---
description: >-
  Load when the deliverable is a PDF file - a fixed-layout .pdf report, one-pager, fact sheet, or handout, or when existing PDFs must be merged, split, or reassembled. Not for: editable Word files, plain prose answers, spreadsheets, decks, or code.
capability: fast-executor
allowed-tools: write_file shell publish generate_image
produces: document
format: pdf
---
You build `.pdf` files whose layout is fixed and whose text is real. The writing follows report
law (lead with the conclusion, ground every figure); this skill is the page.


## The house look (ADR-034) - build on `plane_house`, then LOOK before you publish
The frame carries `plane_house` (on `PYTHONPATH`): the one visual identity every deliverable
wears - palette, type scale, margins, 16:9 canvas, numbered figures, page numbers. Build on its
builders instead of a bare `Document()` / `Presentation()`; a default-template file is a
defect the QA gate names. Then **render and read the pages**:
`python3 -m plane_house.render /work/<file> --pages /work/pages` converts the file to PDF,
rasterizes a sample of pages to `/work/pages/page-NN.png`, and prints the layout findings -
open two or three PNGs with `read_file` (an image comes back as pixels) and fix what you see.
The render only READS your file: the one-save-then-publish law still holds. Internal figure
ids (`fig-…`) never appear on a face - the builders put them in alt text / speaker notes /
keywords, which is where QA looks for them.

For a PDF the house path is the Word twin: build the `.docx` with `plane_house.docx_theme`,
then `from plane_house.render import to_pdf; to_pdf("/work/report.docx", "/work/pdf")` -
LibreOffice renders the same fonts, page numbers and repeating table headers Word would, and
the PDF is byte-identical in look to the `.docx`. Reach for reportlab directly
(`plane_house.pdf_theme.HouseDocTemplate`, `house_styles`, `table`, `figure`) only when the
conversion fails or the page must be drawn (forms, exact coordinates).

## Building with reportlab (pinned in the sandbox)
Write ONE Python script to `/work`, run it with the shell (`python3 /work/build.py`), save the
PDF ONCE, then publish it as type `document`.

The workhorse is **platypus** - a story of flowables poured into a page template:

```python
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

doc = SimpleDocTemplate("/work/report.pdf", pagesize=letter,
                        leftMargin=inch, rightMargin=inch, title="Q3 Review")
styles = getSampleStyleSheet()
story = [Paragraph("Q3 grew 23% on services strength", styles["Title"]),
         Spacer(1, 12),
         Paragraph("Revenue reached $4.2M ...", styles["BodyText"])]
doc.build(story)
```

- **Inline markup, not Unicode tricks.** `Paragraph` text is mini-XML: `<b>`, `<i>`,
  `<font color="...">`, and `<sub>/<super>` for subscripts. **Never use Unicode
  sub/superscript glyphs** (₂, ²) - the built-in fonts lack them and render tofu boxes. And
  escape literal `&` `<` `>` as `&amp; &lt; &gt;`, or the paragraph fails to parse.
- **Fonts: the built-in set only** - Helvetica, Times-Roman, Courier and their bold/italic
  variants. The sandbox has no system fonts to embed; naming one anyway is an error, not a
  fallback.
- **Tables**: `Table(data, colWidths=[...])` styled with `TableStyle` commands
  (`GRID`, `BACKGROUND`, `FONTNAME`, `ALIGN`, spans). Pass `repeatRows=1` or a long table
  loses its header at every page break.
- **Charts ride as images, numbers stay as text.** matplotlib → `savefig(..., dpi=150)` →
  `platypus.Image(path, width=...)`. Any figure that exists only inside the chart's pixels is
  invisible to extraction - state the numbers in text too, or nothing can verify them.
- **Precision one-pagers** use the canvas directly (`canvas.Canvas`, `drawString(x, y, ...)`) -
  and the origin is the **bottom-left**, y grows upward; measuring from the top is the classic
  first bug.

## Figures
**Charts are computed, illustrations are generated - never the other way around.** A data chart
(bars, lines, anything with an axis) is built with matplotlib inside your build script,
`savefig(dpi=150)`, embedded with `add_picture`/`platypus.Image` - every plotted number is a real
number, and you state the key figures in prose too, or nothing can verify them. An *illustrative*
image (a cover, a concept visual, imagery no dataset produces) comes from the `generate_image` tool:
generate to `/work/images/*.png`, embed it, caption it, and reference it in the text. If
`generate_image` reports failure, keep a captioned placeholder and continue - an illustration is never
worth the document.

## Working with existing PDFs (pypdf, pinned)
When the task hands you PDFs, carry pages - never rebuild content you already have:
`PdfReader(src).pages` → `PdfWriter().add_page(page)` → merge, split, reorder, rotate
(`page.rotate(90)`). Extract text to read a source: `page.extract_text()`.

## What the verifier will do
It reads your PDF's text back with pypdf and checks every substantial number against the
sources in `/task/inputs/`. A PDF whose text extracts empty - content rasterized into images -
fails at extraction; keep the content as real text. And publish means done: **one save, one
publish, never touch the file after** - a re-save changes the bytes and harvest fails on the
hash mismatch.

## Your bundled tools (beside this file)
- `scripts/check_pdf.py` - pre-flight before publish:
  `python3 /skills/pdf/scripts/check_pdf.py /work/report.pdf`. Confirms the file opens, counts
  pages, and proves the text actually extracts (printing the head so you can read it back).
  Read-only; it never changes your bytes.
- `references/reportlab.md` - deeper recipes (table styling, two-column flow, page furniture,
  canvas patterns) when the ask goes past the basics.

## Gotchas
*Append-only. Every task failure with a fix becomes a line here (§11).*
- Canvas coordinates measure from the bottom-left - a layout computed from the top renders
  upside-down-ish and off-page.
- A bare `&` in Paragraph text kills the build with a parse error - escape it.
- `Table` without `repeatRows=1` drops its header after the first page break.
