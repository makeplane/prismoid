# ADR-022 · The reader renders every kind: in-frame preview sidecars, native PDF, the sandboxed page

**Status:** accepted · 2026-08-08

## Context

ADR-021 made every document format producible; the reader still said "download to open it" for
all of them. One binary preview existed — the spreadsheet sidecar (ADR-015 M4) — and it was
**structurally stale**: written only by the post-delivery verify job, while `ArtifactView`
fetches once on mount and never refetches on `verification.signed`, so the auto-opened reader
essentially never showed it. `.pdf` and single-file HTML had no render story at all, and the
judge-visible fact from ADR-021 stood for humans too: a delivered file nobody can see is a
claim, not a deliverable.

The reference products settle the *how*. Claude artifacts run untrusted HTML in a **sandboxed
iframe with a unique opaque origin** — `allow-scripts` and never `allow-same-origin`, behind a
restrictive CSP; that combination is the industry line for exactly this problem. Browsers ship
their own sandboxed PDF viewers (PDFium, pdf.js), so PDF embedding needs no parser of ours.
For office files the choice is client-side parser bundles (SheetJS-class, hundreds of KB of
untrusted-input parsing in the reader's browser) versus server-side extraction; this system
already owns the third and better option: **the frame** — the parsers are pinned there, the
blast radius is already contained there, and the sidecar mechanism already exists.

## Decision

1. **Previews are extracted where the artifact was made, when it is made.** A static preview
   runner (`ai/frames/preview.py`, the publish-runner hand-off: static bytes + args file +
   fixed exec line) runs in the STILL-LIVE work frame during harvest; `_harvest` threads
   `previews: dict[path, bytes]` into `operations.artifacts.harvest`, which stores each as its
   own CAS blob — so the artifact row, and therefore `artifact.produced`, is **born with
   `has_preview` true**. Strictly best-effort: an unparseable file yields no preview, never a
   failed harvest ("presentation, never attestation"). The verify-frame spreadsheet sidecar
   stays as the post-receipt refresh of the same slot; the runner's `=SUM` evaluator is its
   deliberate twin (change one, change both — both scripts must stay self-contained).
2. **One sidecar, five kinds.** `PreviewOut` grew one payload field per kind: `sheet` (the M4
   grid, computed values), `document` (styled blocks + tables extracted from `.docx`), `deck`
   (slide titles + lines), `code` (the archive's files with small-text bodies), `page` (a
   page-shaped tar's own `index.html`). The web's `sheet-preview.tsx` became
   `preview-view.tsx`: one fetch, one switch on `kind`, mounted exactly where the sheet case
   always was (the no-renderer branch above the DownloadCard floor).
3. **PDF renders natively.** New `RenderHint.PDF`: the registry maps it to `PdfView`, which
   embeds the presigned URL in `<object type="application/pdf">` — the browser's own sandboxed
   viewer does the rendering, the DownloadCard sits inside as the literal fallback content for
   browsers that refuse to embed. No PDF parser enters the bundle.
4. **The page runs in a cage.** `page` previews render in an iframe with
   `sandbox="allow-scripts"` and **nothing else** — never `allow-same-origin` (the untrusted
   page gets a unique opaque origin: no cookies, no storage, no parent DOM, no top
   navigation, no popups, no forms) — plus `referrerPolicy="no-referrer"` and a CSP `<meta>`
   injected at the head's front (`default-src 'none'`; inline style/script only; `data:`
   images) that enforces the page skill's self-contained law at the browser even for bytes
   that arrived by other roads. A "View source" disclosure shows the HTML as code. These are
   **pinned by render tests** (`preview-view.test.tsx`) — loosening the sandbox fails CI.
5. **Code shows as code.** The archive's files render as monospace source under per-file
   disclosures (the untar view humans deserve, matching what the judge already gets). The
   vendored shiki `CodeBlock` is the recorded upgrade path for highlighting; v1 stays quiet.

## What deliberately did NOT happen

- **No client-side office parsers** (SheetJS/mammoth/pdf.js bundles): the frame already has
  better parsers, ADR-015's "zero added to the web bundle" stands, and untrusted-zip parsing
  stays out of the reader's origin. Zero new dependencies, either tree.
- **No "Open in Google Docs / Office" button yet.** Researched: a real one requires the Drive
  API (`files.create` with MIME conversion) behind OAuth — there is no honest zero-auth path,
  and viewer-URL tricks (docs.google.com/viewer, view.officeapps.live.com) require publicly
  reachable URLs and would hand presigned links to a third party. The right home is a
  **connector** (ADR-020's OAuth machinery) — recorded as the named follow-up. Until then the
  download opens natively in Word/Excel/PowerPoint, which is the same one-click.
- **The revise() path still drops previews** (a human edit mints a row with no sidecar until
  the next verify) — recorded; today's revisable kind (collection) has a full renderer and
  never needs the sidecar.

## Proof

Five live tasks (one per format) after the change: xlsx/docx/pptx/page delivered with
`has_preview: true` **at delivery** and `GET /preview` serving the right kind over the real
wire (sheet grid with the `=SUM` total COMPUTED — 103.9 from 28.4+33.9+41.6; 17 docx blocks
led by the real `h1`; 3 slides; 10 KB of page HTML) — and the PDF delivered with
`render: "pdf"`. Gates: `just check` green (both trees) · backend 933 · web 124 (incl. the
iframe security pins) · build green.
