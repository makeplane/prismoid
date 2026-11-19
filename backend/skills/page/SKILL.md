---
description: >-
  Load when the user wants a web page - a landing page, microsite, dashboard mock, or one-pager delivered as a single self-contained static HTML file (vanilla-JS interactivity inline is fine). Not for: multi-service web apps, programs, or scripts (code skill), prose documents, decks, or spreadsheets.
capability: fast-executor
allowed-tools: write_file shell publish
produces: document
format: html
---
You build one self-contained HTML page that opens anywhere - double-clicked from a folder, no
server, no network - and still looks deliberately designed.


## The house look (ADR-034)
Use the house tokens as CSS variables so a page reads as the same studio as the documents:
`--ink:#1F2933 --paper:#FFFFFF --primary:#1F3A5F --accent:#C9A227 --rule:#D9DEE3 --muted:#6B7785
--surface:#F3F5F7`; body `Inter, "Segoe UI", sans-serif` at 16px/1.5, headings in
`--primary`. Before publishing, `python3 -m plane_house.render /work/page.html --pages /work/pages`
gives an APPROXIMATE print render (LibreOffice's HTML import: no scripts, partial CSS) - enough to
catch a missing section or an unreadable table, not a substitute for reading your own markup.

## The deliverable shape (this exact workflow)
The page ships as ONE **`.html` file** published as type **`document`** (ADR-033: a static
page is a document - the reader shows it in its sandboxed page view, and the document suite
reads its extracted text):

1. Write **`/work/page.html`** - the whole page, one file (name it after the subject if you
   prefer, e.g. `/work/launch.html`; the extension is what matters).
2. Prove it before publishing: run
   `python3 /skills/page/scripts/check_page.py /work/page.html` with the shell; fix what it
   names.
3. `publish` the **file** - `path="/work/page.html"`, `type="document"`.

The old directory-plus-`test.sh` webapp shape is retired (2026-08-17, ADR-033): the page
carries no test because the document suite and your own step-2 check are the verification;
a directory publish now means the code skill, not this one.

## The self-contained law
Everything inline: `<style>` in the head, `<script>` before `</body>`, images as inline SVG or
small `data:` URIs. **Zero external requests** - no CDN scripts, no font services, no remote
images. The build sandbox has no network, the verify sandbox has none, and the reader may open
the file offline: a page that phones home is broken three times. Fonts come from system stacks
(`font-family: Georgia, 'Times New Roman', serif` or
`system-ui, -apple-system, 'Segoe UI', sans-serif`). Interactivity is vanilla JS - no
frameworks. Links out (`<a href="https://…">`) are content and fine; *loading* anything remote
is not.

## The page itself
- **Real structure**: `<!doctype html>`, `<html lang="en">`, one `<title>` that names the
  thing, `<meta name="viewport" content="width=device-width, initial-scale=1">`, then
  `header / main / section / footer`, exactly one `<h1>`.
- **Designed, not templated**: pick the palette and type from the *subject* (a tide-chart page
  and a tax-prep page should not look alike); one signature element the page is remembered by;
  everything else quiet. Depth in `references/design.md`.
- **Responsive by construction**: one content column with `max-width` + margin auto; grids and
  flex rows that wrap; nothing fixed-width past ~40rem.
- **The floor**: visible `:focus-visible` states, honest contrast (body text ≥ 4.5:1),
  `prefers-reduced-motion` respected if you animate, alt text on every meaningful image.
- Ground what the page claims: figures shown on the page come from `/task/inputs/`, same as
  any document.

## Your bundled tools (beside this file)
- `scripts/check_page.py` - your pre-publish gate (step 2). Stdlib-only; checks:
  parses as HTML with a doctype, `<title>` and one `<h1>` present, `lang` set, viewport meta
  present, **no external resource loads** (script/link/img/iframe/source/@import), size within
  budget. Run it against the file you are about to publish; fix everything it names first.
- `references/design.md` - the design method: subject-grounded palettes, type pairings from
  system stacks, layout patterns, and the avoid-list.

## Gotchas
*Append-only. Every task failure with a fix becomes a line here (§11).*
- Publish the FILE (`/work/page.html`) as `type="document"` - the pre-ADR-033 directory
  shape (`/work/site` as `webapp`, with `test.sh`) is retired; a directory publish is the
  code skill's shape, not this one's. *(Reshaped 2026-08-17 with the kind change - the two
  directory gotchas that lived here described the retired shape.)*
- A `<script src="https://cdn...">` sneaks in with any copied snippet - the checker names it;
  inline the code or cut the feature.
- The page's claims are still a document's claims: figures shown on the page must trace to
  `/task/inputs/` - the document suite reads the page's TEXT and grounds it like prose.
