# ADR-023 · Full-fidelity renditions: LibreOffice in the frame, the document as the view

**Status:** accepted · 2026-08-08

## Context

ADR-022's extracted previews show the content, not the document — and the user named the gap:
people expect to SEE the Word file, the deck, the workbook, full length, as they would in
Office. The industry answer (and the reference document skills' own visual-QA pipeline) is
server-side conversion by LibreOffice headless; client-side parser bundles were rejected again
for the same reasons as ADR-022.

## Decision

- **The frame image carries LibreOffice** (writer/calc/impress, no Java) + the
  metric-compatible font trio (Liberation, Carlito, Caladea) — ~600 MB, the honest cost of
  fidelity.
- The preview runner grew a rendition leg: at harvest, in the still-live work frame,
  `soffice --headless` converts **docx/pptx → PDF** and **xlsx → styled HTML**, under a
  per-run minted profile that FORCES recalculation (`OOXMLRecalcMode=0` — an openpyxl file
  has no cached values; without this every formula renders as a hole; proven by the in-frame
  test asserting the `=SUM` total lands in the HTML).
- `artifacts.rendition_sha256 + rendition_media` (migration `f1a3c5e7b9d2`), threaded through
  harvest beside previews; `GET /v1/artifacts/{id}/rendition` returns a presigned link.
- Web: `RenditionView` wins over the extracted preview when present — PDF media in the
  browser's own viewer (`<object>`); HTML media in a cage STRICTER than the page's
  (`sandbox=""` — nothing granted, no scripts at all — plus the injected no-network CSP and
  no-referrer), both pinned by render tests. The extracted preview stays the instant fallback
  whenever LibreOffice could not render.
- Best-effort throughout: a rendition that cannot be made is absent, never a failed harvest.
  LibreOffice's leniency is recorded honestly: bytes it can interpret may render even when the
  strict parser refused a preview.

## Proof

In-frame suite: docx and pptx renditions are genuine `%PDF` streams; the xlsx rendition is a
styled `<table>` with the recalculated total. Live: a real Word-document task delivered in
~40 s with `has_rendition` true at delivery and `GET /rendition` serving a 36,873-byte
LibreOffice PDF over the wire. Gates: `just check` green · backend suite green · web 130
(incl. the sheet-cage pins) · migration applied.

Recorded: pre-existing artifacts carry no renditions (no backfill job yet); a live-proof
lesson — never run the integration suite against the dev DB while proof tasks are in flight
(it churns the same tables; one delivered task's rows were deleted mid-verify by exactly that).
