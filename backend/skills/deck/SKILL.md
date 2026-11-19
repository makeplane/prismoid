---
name: deck
description: >-
  Load when the user wants a slide deck or presentation - a sequence of slides with a narrative arc, each making one point. Not for: written prose reports, spreadsheets, or code.
capability: fast-executor
allowed-tools: write_file shell publish generate_image
produces: deck
---
You build decks where each slide makes one point and the sequence tells a story.

## The rules of a deck that lands
- **One idea per slide.** If a slide needs two headlines, it is two slides.
- **The title is the takeaway**, not the topic: "Revenue grew 23% on services strength", not "Revenue".
- **The arc carries the argument** - situation, complication, resolution. A reader flipping only the titles should get the whole story.
- Numbers on a slide are still numbers: every figure traces to a source, and a chart's underlying data must exist.


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
from plane_house.pptx_theme import HouseDeck
deck = HouseDeck("Q3 review", subtitle="Board pack")      # 16:9, title slide, footer + slide numbers
deck.section("Revenue", 1)
deck.bullets("Services carried the quarter", ["+23% services", "Flat hardware"], notes="…")   # splits past 6 bullets / 70 words
deck.image_right("Churn fell", ["1.1% monthly"], "/work/images/fig-s02-1.png", "Monthly churn", fig_id="fig-s02-1")
deck.image_full("/work/images/fig-s02-2.png", "Pipeline by stage", heading="Pipeline", fig_id="fig-s02-2")
deck.table("Pipeline by region", [["Region", "Q2", "Q3"], ["EMEA", "1.2", "1.4"]])   # native, shaded, continues past 10 rows
deck.big_number("$4.2M", "Q3 revenue", "up 23% on services strength")
deck.two_column("Before / after", ["…"], ["…"], left_title="Before", right_title="After")
deck.closing("Questions")
deck.save("/work/deck.pptx")                              # ONE save
```
The layout menu IS the variety: rotate image-beside-text, full-bleed, table, big-number and
two-column so no two neighbouring slides share a shape. Never shrink below 14pt to make text
fit - split the slide (the builders do it for you past the caps). Native charts
(`CategoryChartData`) still go on a slide from `deck.bullets(...)[0]` via `slide.shapes.add_chart`
inside the content region (`deck.content_box`).

## Building with python-pptx (pinned in the sandbox)
Write ONE Python script to `/work`, run it with the shell (`python3 /work/build.py`), save the
`.pptx` ONCE, then publish it as type `deck`.

- **Set the canvas first.** The default template is 4:3. Go widescreen explicitly:
  `prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)`. Position everything in
  `Inches()` / `Pt()`.
- **Nothing clamps a shape to the slide.** `left + width` past 13.333in is written happily and
  renders off-canvas - the single most common python-pptx failure. Keep every shape inside the
  frame and run the bundled checker before publishing.
- **Set text on runs, not on the frame.** `text_frame.text = "..."` collapses the frame to one
  unstyled run, wiping every size/bold/color you set. Build paragraphs
  (`tf.paragraphs[0]`, `tf.add_paragraph()`) and runs (`p.add_run()`), then style the run:
  `run.font.size = Pt(18)`, `run.font.bold = True`, `run.font.color.rgb = RGBColor(...)`.
- **Use real placeholders for titles** (`slide.shapes.title.text = ...` on a layout that has
  one) - the outline, the checker, and the verifier all read them. Bullets: paragraphs with
  `p.level = 0/1` in a content placeholder; **never type `•` into the text**.
- **Charts are native.** `CategoryChartData()` + `slide.shapes.add_chart(...)` for anything
  PowerPoint can chart - the numbers stay data. A matplotlib PNG (`add_picture`) is the
  fallback for chart forms PowerPoint lacks; if you use one, the figures must ALSO appear as
  text somewhere on the slide, or nothing can verify them.
- **Three things python-pptx will not do**: duplicate an existing slide, read or insert
  SVG/EMF art, and restyle text assigned via `.text` (see above). Build fresh, use PNG.
- Speaker notes: `slide.notes_slide.notes_text_frame.text = "..."`.

## The verifier and the slides
**A verifier will open your deck and check every number on a slide against the sources you were
given.** A figure that is in no source and is not one step of arithmetic on source figures is a
number you invented, and it fails the deck. A figure you calculate is one step, and its working
goes in that slide's speaker notes: `($41.6M - $28.4M) / $28.4M = 46.5%`, naming the two source
figures. Apply a percentage to a dollar base only when the source says that percentage is of that base.

- **A slide count in the ask is the whole deck.** "3 slides" means 3 in the file, the title and any
  closing included: drop the closing first, then fold the title into the first content slide.
- **Say where a number comes from** when it matters - in the speaker notes, not on the slide.
- **The slide face talks to the audience, not about your sources.** No "the brief", "the source",
  or "not stated" on a face; a gap you must flag goes in the notes.
- **Build what was asked, and no more.** "The top risk" is one risk: a second one stays off the deck.
- **A slide never argues with its own figures.** Keep a claim, quoted or not, only if the numbers on
  that slide agree with it, and keep titles under about 40 characters so they hold one line.

## Imagery
**Charts are computed, illustrations are generated.** A data chart is native `CategoryChartData`
first, a matplotlib PNG (`add_picture`) as the fallback - and its figures must ALSO appear as text on
the slide, or nothing can verify them. An *illustrative* image (a title visual, a concept image no
dataset produces) comes from the `generate_image` tool: generate to `/work/images/*.png`,
`add_picture` it, and let the slide's text carry the point without it. If `generate_image` reports
failure, ship the slide without the image - never fail a deck over decoration.

## Design - the short law (references/design.md carries the full method)
- **Palette from the topic**: one dominant color, one accent, neutrals - never default blue,
  never all colors equal. Dark title + closing slides bracketing light content reads composed.
- **Type scale**: titles 36–44pt bold · section headers 20–24 · body 14–16 · captions 10–12.
  Left-align body text; center only titles.
- **Space**: ≥0.5in margins, one consistent gap (0.3–0.5in) between blocks, and room to
  breathe - a slide is not a page.
- **Every slide carries something visual** - a chart, a big-number callout (60–72pt), a
  simple shape diagram - never a wall of bullets.
- **The tells to avoid**: decorative accent bars and stripes, the same layout repeated on
  every slide, text overflowing its box, one styled slide among plain ones.

## Your bundled tools (beside this file)
- `scripts/check_deck.py` - pre-flight before publish:
  `python3 /skills/deck/scripts/check_deck.py /work/deck.pptx` (add `--layout` to run the house
  render + geometry pass and leave page PNGs in `/work/pages`). Prints every slide's text (read
  it - wrong order and missing content hide in code you just wrote) and flags off-slide
  shapes, empty titles, and empty slides. Read-only; it never changes your bytes.
- `references/design.md` - palettes, layout patterns, and the avoid-list in full.

## Gotchas
- Do not put a paragraph on a slide. If it needs prose, it wants the report skill, not this one.
- A figure on a slide is held to the same grounding as one in a memo - the verifier checks it.
- Build the model first if the deck depends on numbers; a deck citing figures nobody computed is a deck of guesses.
- A shape positioned past the slide edge is written, not clamped - it silently renders off-canvas; the checker names each one.
- `text_frame.text = "..."` after styling runs erases the styling - set text run-by-run.
- Never re-open and re-save the `.pptx` after publishing: the bytes change and harvest fails on the hash mismatch.
