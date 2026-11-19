# ADR-034 — The formatting bar: one house look, and a gate that LOOKS at the page

**Status:** accepted · 2026-08-20

## Context

The user's report, verbatim in spirit: documents and decks shipped with formatting they would
not hand a customer — page length, margins, tables, image placement; a deck "so horrible I
couldn't look at the images". The bar named was Perplexity-Computer-grade output.

What the tree actually did (verified before designing):

- `md_to_pptx.py` built on python-pptx's **blank default template** (4:3), three layouts, and
  pasted every figure at `Inches(1.0, 2.2, width=8.0)` **over the bullet placeholder** — text
  and picture overlapped by construction. Tables collapsed to `" — "`-joined bullets capped at
  eight. No fonts, sizes, or density caps.
- `md_to_docx.py` / `md_to_pdf.py` used a bare `Document()` / reportlab's sample stylesheet,
  `Table Grid`, a **page break after every section**, no margins/footer/page numbers, and a
  customer-visible caption reading `Figure (fig-s03-1): …` — the internal id kept on the face
  so QA's text grep could find it.
- **Nothing ever looked at a rendered page.** The QA gate (ADR-026) and the receipt suites
  read extracted text; `_deliverable_view` tells the judge it cannot see a binary; the
  LibreOffice rendition (ADR-023) was display-only. ADR-026 recorded the visual pass as
  deferred.
- The deck/docx SKILL.md files carried decent design law (16:9, type scale, margins), but the
  long-form assembly lane bypassed them through the walkers, and the one-shot lane
  (typesetter/presenter writing `/work/build.py` freehand) had no shared engine to lean on.

Seams that shaped the design: skills mount only when pinned and cap at 20 files / 256 KiB, so a
shared engine cannot live in a skill; `frame.read_file` already returns PNGs as `BinaryContent`
(an agent in the frame can see pixels today); the QA arm fires on `StepSpec.qa` regardless of
lane, independently of `verify`; `ModelClass.VISION` exists on the catalog rows.

## Decision

1. **One house visual identity, baked into the frame image.** `plane_house` lives at
   `/opt/plane` (on `PYTHONPATH`, root-owned, read-only — guidance the system gives the agent,
   never its workshop): `tokens` (palette, type scale, margins, 16:9, density caps; a `Theme`
   value on every builder is the later per-Space override seam — seam only, nothing wires it),
   `pptx_theme.HouseDeck` (title / section / bullets / image-beside / image-full / two-column /
   native table / big-number / closing; pictures aspect-fit into a region no text box shares;
   footer + slide numbers; bullets SPLIT onto balanced `(cont.)` slides past the caps, never
   shrink below 14pt), `docx_theme` (styles set once, cover, a real `PAGE` field in the footer,
   tables with widths on every cell + shaded repeating header, `Figure N.` captions; no page
   break per section), `pdf_theme` (the reportlab twin, kept as the FALLBACK engine),
   `render` (deliverable → PDF via LibreOffice → `pdftoppm` page PNGs; read-only on the
   deliverable, the one-save law holds), `geometry` (overlap, off-canvas, overflow estimate,
   density, empty placeholders, orphan pages, page count vs band, ids on the face).
   The image tag now hashes the **whole** build context, not only the Dockerfile — an edit to
   the house is a different image for the same reason a Dockerfile edit is.
2. **The walkers and the freehand builders use the same engine.** `md_to_pptx/docx/pdf` were
   rewritten on it; `md_to_pdf --engine soffice` (default) builds the house `.docx` and converts
   it — one engine, the PDF is the Word file's twin (Calibri/Carlito, real page numbers,
   repeated table headers) — and falls back to reportlab when the conversion fails, printing
   which engine won. The docx/deck/pdf/assemble/page skills and the typesetter/presenter
   prompts teach the house builders and the render-then-look step.
3. **Figure ids leave the face.** They live in speaker notes + picture alt text (pptx) and alt
   text + core keywords (docx). The precheck's `qa:figures` searches face text PLUS those
   homes (`hidden_ids`); a new `qa:layout:face-ids` FAILS when a `fig-…` token is visible.
   Walkers and precheck shipped in the same change — else every run would trip the false
   REVISE `md_to_docx.py` memorialised.
4. **The visual pass — ADR-026's deferred item, lifted.** The precheck renders the deliverable
   (reusing the harvest's own rendition when it exists — one conversion, not two), runs the
   geometry checks as `qa:layout:*` rows (a FAIL is a LAYOUT revise floor, like every check),
   and leaves the sampled pages (first three, last two, every flagged page, cap 12, 80 dpi) as
   PNGs. The composer hands them to the panel as `BinaryContent` after the text — **only to
   seats whose catalog row serves VISION**; a blind seat reads the same words. `qa.md` gained
   rubric item 8 and the `layout` kind. The check budget rose to 330 s — above one LibreOffice
   conversion; a render that cannot happen costs the rows, never the gate.
5. **Standard-lane file deliverables get the gate.** `one_step_plan(qa=)` + a pure planner
   stamp (`_gate_visual_steps`): every typesetter/presenter step carries `qa=True`, whichever
   path planned it; `verify` stays — the receipt still signs after. This is the smallest
   honest amendment to ADR-019's answer-first law: prose answers are untouched; a FILE whose
   look is the product is looked at before "done", at ~30–90 s and cents.
6. **Dependency:** `poppler-utils` (apt, frame image only) — `pdftoppm` is one small static
   binary set, the tool ADR-026 named. PyMuPDF rejected (a second PDF engine, 40 MB, AGPL).
   HANDOFF §3 carries the row.

## What was seen, not assumed

The first rendered pages found two defects no text check could: the docx `Normal` style's
EXACT 14pt line spacing clipped every inline picture to a 14pt bar (now a multiple), and the
eight-bullet split went 6+2 (now balanced 4+4). Both are exactly the class of defect this ADR
exists to catch — found by the mechanism it adds, before it shipped.

## Live findings, same day (2026-08-20) — and what they changed

The first standard-lane docx run on the new image (task 646e97fe, $0.40) exercised the gate end
to end and found four things the fixtures could not:

1. **A soft timeout is a hang.** One QA seat's provider call stalled; `asyncio.wait_for(300)`
   fired on time and then WAITED for a cancellation the stream never honoured — the gate held
   the thread for 52 minutes. `operations/qa._hard_timeout` now abandons the seat at the
   deadline (cancel, stop awaiting); pinned by a test whose coroutine ignores cancel.
2. **Reviewers describing pages write longer summaries** — past the `QAReport.summary` 500-char
   cap; three overruns cost the seat (`ModelIncapable`) and the document shipped PASS_UNREAD.
   Reproduced offline; the summary is now clipped before validation and the prompt states the
   budget. Findings keep their limits — they are what a rebuild executes.
3. **A closing checklist is not a truncation.** `qa:truncation` flagged the final bullet ("…
   check-in with your manager") as an unclosed sentence and billed a rebuild. The docx
   extraction now marks list paragraphs (`- item`) and a list item may end a document.
4. **"Use the house" was advice, and the typesetter ignored it** — both attempts were bare
   default-template files (no cover, no footer; attempt 2 drifted to a serif body). The
   builders now stamp `core_properties.category = plane_house/1` and `qa:layout:house` fails a
   file without it — a floor, not a style note. (The geometry check itself was RIGHT: attempt 1
   had one bullet orphaned on page 2, named as `qa:layout:orphan-pages`; the rebuild fixed it.)

Operational note: builtin skill text reaches an agent through the published row, not the file —
a SKILL.md edit needs the builtin re-seed, or the agent reads yesterday's skill.

## The tuning pass (2026-08-20, user-directed)

The user's observations after the first pages: rules should live in one file and be tuned
gradually; the font read as "Word's"; tables could be far better; margins too generous.

- **Font: Inter**, chosen by eye from Inter / Lato / Open Sans / Calibri rendered on the same
  page and slides. `fonts-inter` joins the image so PDFs embed it; a `.docx`/`.pptx` opened
  without Inter falls back to the reader's sans (accepted; PDF is the sharing format).
  Rendering the candidates exposed a bug: Word's heading styles carry a theme-font attribute
  (`w:asciiTheme`) that outranks the name — every heading had rendered in Calibri regardless.
  `_set_font` now strips it.
- **Tables: `TableStyle` tokens** shared by docx/pptx/pdf — no vertical rules, a light header
  band with one 1.5pt rule under it, hairlines between rows, padding on every cell, numeric
  columns (header included) right-aligned, zebra off.
- **Margins:** documents 0.8in top/bottom × 0.9in sides (a reading measure), cover placed by
  spacing rather than blank paragraphs; slides 0.5in safe area, a shorter title band
  (`title_top/title_h/content_top/footer_h` tokens).
- **One file:** the ~28 numbers that lived in the builders (cell sizes, footer size, spacing,
  row heights, "Letter = 8.5") moved into `Theme`; builders read tokens only.

## The review pass (2026-08-20, pre-commit)

A from-scratch read of the whole change, every finding proven in the frame before it was fixed:
the reportlab fallback's table header text was paper-white on the light header band (a
Paragraph's own colour outranks a `TableStyle` `TEXTCOLOR`); `image_right` kept only the first
density chunk and silently dropped the rest (now continues on `(cont.)` slides, split against the
half-width box); an all-empty column counted as numeric by a vacuous `all()`; the orphan-page rule
flagged any interior page under 40 words — including a figure page and the walker's own 11-word
Contents page — so pages carrying a picture or headed "Contents" are exempt; `_hard_timeout`
now retrieves the abandoned seat's eventual exception; `COPY house/` shipped `__pycache__` (a
`.dockerignore` now, hashed with the context); `is_number`/`bare_caption` lived three times and
live once in `plane_house.text`. The double page break the `--toc` walker path risked (cover
break + `page_break_before`) was probed: LibreOffice renders 4 pages, none blank.

## Verified

Live run 2 (task 6884a782, after the fixes): the sighted panel read the pages and revised a
bare-template draft (house floor + its own "no footer or page numbers" finding) in 14 s; the
rebuild on `house_document` passed in 6 s — cover, page numbers, house styles, a real table —
$0.45 end to end. The shipped file was rendered and looked at.

`just check` green · unit: `test_house_tokens` (density, sampler, overlap, image tag),
`test_visual_qa_plan`, `test_qa_policy` (+2), `test_assemble_walkers` (the id law flipped) ·
in-frame: `test_house_engine` — three walkers on real fixtures: 16:9, zero overlap/off-slide/
overflow rows, a `(cont.)` split, no id on any face + every id in notes/alt text, docx
Calibri/1in/PAGE field/`tblHeader`/`Figure 1.`, a real soffice PDF rasterized to PNGs, the
precheck's `qa:figures` + `qa:layout:*` rows, and planted defects NAMED.

## Deferred, named

Per-Space theme override (the `Theme` argument exists; no dial, no storage) · a headless-
Chromium render for `.html` pages (LibreOffice's HTML import is approximate and says so) ·
`IMAGE` as a verifier suite (ADR-021's recorded gap, unchanged) · the golden-eval layout axis
beyond the in-frame proof · backfilling renditions for pre-ADR-023 artifacts.
