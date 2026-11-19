# python-docx recipes for documents that read well

Read this when the ask goes past headings, lists, and one table - never guess an API.

## Page setup and sections

```python
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches, Pt

doc = Document()                      # the default template is US Letter, 1in margins
section = doc.sections[0]
section.left_margin = section.right_margin = Inches(1)

# Landscape: swap the dimensions AND set the flag - the flag alone rotates nothing.
section.orientation = WD_ORIENT.LANDSCAPE
section.page_width, section.page_height = section.page_height, section.page_width
```

A new section (different orientation or columns mid-document) is
`doc.add_section(WD_SECTION.NEW_PAGE)` - sections carry page furniture, so set the new
section's margins too.

## The document-wide voice

```python
normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(11)
normal.paragraph_format.space_after = Pt(8)
```

Set the voice once on `Normal`; style runs only for emphasis. Alignment:
`from docx.enum.text import WD_ALIGN_PARAGRAPH` →
`p.alignment = WD_ALIGN_PARAGRAPH.CENTER` (title pages only - body stays left).

## Headers, footers, title page

```python
header = section.header
header.paragraphs[0].text = "Acme Corp - Q3 Review"
section.different_first_page_header_footer = True   # a title page carries no header
```

Automatic page numbers are a Word *field* (`PAGE`) that python-docx has no API for - inserting
one means hand-building XML (`w:fldSimple`). Prefer honest static footers; add the field XML
only when the user asked for page numbers explicitly:

```python
from docx.oxml.ns import qn
fld = footer.paragraphs[0].add_run()._r
simple = fld.makeelement(qn("w:fldSimple"), {qn("w:instr"): "PAGE"})
fld.addnext(simple)
```

## Tables that hold their shape

```python
t = doc.add_table(rows=1, cols=3)
t.style = "Table Grid"
t.autofit = False
widths = (Inches(2.5), Inches(2.0), Inches(1.5))
for row in t.rows:
    for cell, w in zip(row.cells, widths):
        cell.width = w

for cell, label in zip(t.rows[0].cells, ("Item", "Owner", "Due")):
    cell.paragraphs[0].add_run(label).bold = True

row = t.add_row()          # data rows appended one by one
row.cells[0].text = "Migration dry-run"
```

Shading a header row is XML (`w:shd`); use it only when asked:

```python
from docx.oxml import OxmlElement
shd = OxmlElement("w:shd"); shd.set(qn("w:fill"), "F2F2F2")
cell._tc.get_or_add_tcPr().append(shd)
```

## Quotes, captions, code

- Block quote: `doc.add_paragraph(text, style="Quote")`.
- Caption under an image: a small italic paragraph - python-docx has no caption-field API.
- Code: a paragraph styled with a monospace run (`run.font.name = "Courier New"`); there is no
  built-in code style in the default template.

## Reading and light edits

```python
doc = Document("existing.docx")
for p in doc.paragraphs: ...        # body paragraphs only - tables are separate
for t in doc.tables: ...
```

Text you can see is often split across several runs (spell-check seams), so run-level
find-replace misses phrases. For surgical edits unzip and edit `word/document.xml` as text
(stdlib `zipfile`), then re-zip the tree - and never reformat the XML.
