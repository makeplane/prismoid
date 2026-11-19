---
description: >-
  Load when the deliverable is a Word file - a .docx the reader opens in Microsoft Word: a formatted report, letter, proposal, or template with real headings, tables, or a title page. Not for: plain prose answers, PDFs, spreadsheets, decks, or code.
capability: fast-executor
allowed-tools: write_file shell publish generate_image
produces: document
format: docx
---
You build `.docx` files a professional would hand over - real structure, clean typography,
content that survives a sceptical read. The writing follows report law (lead with the
conclusion, ground every figure, cut what survives its own deletion); this skill is the file.


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

```python
from plane_house.docx_theme import house_document, heading, add_table, add_figure, figure_placeholder
doc = house_document("Network move checklist", subtitle="August 2026", author="Plane")  # cover + styles + footer page numbers
heading(doc, "Scope", 1, first=True)          # the first body section starts its page; the rest FLOW
doc.add_paragraph("…")
add_table(doc, [["Item", "Owner"], ["Racks", "Ops"]])              # widths on every cell, shaded repeating header
add_figure(doc, "/work/images/fig-s01-1.png", "Rack layout", number=1, fig_id="fig-s01-1")  # "Figure 1. Rack layout"
doc.save("/work/report.docx")                 # ONE save
```
No page break per section - only the cover's and the first body heading's; a break per
section is the orphan-page tell the layout check flags.

## Building with python-docx (pinned in the sandbox)
Write ONE Python script to `/work`, run it with the shell (`python3 /work/build.py`), save the
document ONCE (`doc.save(...)`), then publish it as type `document`.

- **Structure is styles, not looks.** Headings are `doc.add_heading("...", level=1)` - a bolded
  paragraph pretending to be a heading has no outline entry, and the verifier's extraction (and
  every screen reader) sees plain body text. `level=0` is the document title.
- **Lists are styles too**: `doc.add_paragraph("item", style="List Bullet")` /
  `"List Number"` (nested: `"List Bullet 2"`). **Never type `•` or `-`** - a typed glyph has no
  list behavior and reads as body text.
- **New paragraph ≠ newline.** A `\n` inside a run does not create a paragraph; call
  `add_paragraph` per paragraph, and `run.add_break()` for a rare intentional line break.
- **Inline emphasis lives on runs**: `p.add_run("the key figure").bold = True`. Font across the
  document: `house_document` sets `Normal` once (Inter, the house face; by hand: `doc.styles["Normal"].font.name = "Inter"`,
  `.size = Pt(11)`) instead of styling every run.
- **Tables need widths on every cell.** `t = doc.add_table(rows=r, cols=c)`,
  `t.style = "Table Grid"`, `t.autofit = False`, then per cell
  `row.cells[i].width = Inches(...)` - a width set anywhere else is ignored by Word. Bold the
  header row via its runs. Cell text: `cell.text = "..."` replaces; for styling, use
  `cell.paragraphs[0].add_run(...)`.
- **Page furniture**: `section = doc.sections[0]`; margins `section.left_margin = Inches(1)`;
  headers/footers via `section.header.paragraphs[0]`; `doc.add_page_break()` before a new
  part; cover metadata on `doc.core_properties.title / .author`.
- **Images**: `doc.add_picture(path, width=Inches(6))` - give one dimension, the aspect holds.
- Stick to the built-in styles (`Heading 1–3`, `List Bullet/Number`, `Table Grid`, `Quote`).
  Custom styles are XML surgery - not worth it for a deliverable.

## Editing an existing .docx
`Document(path)` opens it - but Word splits visible text across arbitrary runs, so
find-and-replace on runs is unreliable. For surgical edits, treat the file as what it is, a ZIP
of XML: unpack with stdlib `zipfile`, edit `word/document.xml` as text (never pretty-print it -
whitespace is significant), re-zip. Rebuild-from-scratch beats surgery whenever the content is
yours anyway.

## Figures
**Charts are computed, illustrations are generated - never the other way around.** A data chart
(bars, lines, anything with an axis) is built with matplotlib inside your build script,
`savefig(dpi=150)`, embedded with `add_picture`/`platypus.Image` - every plotted number is a real
number, and you state the key figures in prose too, or nothing can verify them. An *illustrative*
image (a cover, a concept visual, imagery no dataset produces) comes from the `generate_image` tool:
generate to `/work/images/*.png`, embed it, caption it, and reference it in the text. If
`generate_image` reports failure, keep a captioned placeholder and continue - an illustration is never
worth the document.

## What the verifier will do
It opens your file with python-docx, extracts every paragraph and table cell, and checks each
substantial number against the sources in `/task/inputs/`. A figure in no source fails the
document; an empty body fails it faster. **Structure is real, not painted**: a screen reader, a table of
contents, and the verifier all read the structure you declared. And publish means done: **one save, one publish, never
touch the file after** - a re-save changes the bytes and harvest fails on the hash mismatch.

## Your bundled tools (beside this file)
- `scripts/check_docx.py` - pre-flight before publish:
  `python3 /skills/docx/scripts/check_docx.py /work/report.docx`. Prints the heading outline
  and body stats, and flags empty bodies, fake lists (typed glyphs), and heading-free walls of
  text. Read-only; it never changes your bytes.
- `references/python-docx.md` - deeper recipes (sections, headers with fields, table styling,
  landscape) when the ask goes past the basics.

## Gotchas
*Append-only. Every task failure with a fix becomes a line here (§11).*
- A promised "table of contents" is a Word field that only refreshes inside Word - build an
  explicit outline list instead of promising a field the reader must regenerate.
- `cell.width` must be set on every cell of the column, not once - Word ignores lone column widths.
- Never re-open and re-save after publishing: `.docx` bytes carry timestamps; the hash check at
  harvest fails on any post-publish save.
