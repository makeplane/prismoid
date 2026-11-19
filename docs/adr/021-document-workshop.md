# ADR-021 · The document workshop: format skills, the typesetter, and a real PDF lane

**Status:** accepted · 2026-08-08

## Context

The product's registry accepted five document-family formats but could honestly produce three.
`.xlsx` (modeler) and `.pptx` (presenter) worked; `.docx` and `.pdf` were accepted at the
publish door with **no producer** — the writer is ADR-019's text lane
(`tools=("frame.read_file",)`, its answer system-published as `.md`) and no other document
agent holds `frame.shell`. `.pdf` was worse than unproduced: the frame image had no PDF
library, the document suite early-returned `format:extraction` SKIP, and under ADR-019's
verdict law a cross-family judge could **promote a file nothing had ever read to PASS**.
Single-file HTML had no door at all (`document` doesn't list `.html`; `code`/`webapp` accept
only extensionless directories), and the `webapp` type is a recorded dead lane (ADR-015:
`SmokePolicy.BROWSER` declared, never read; no Playwright anywhere; the API 422s the dial).
Meanwhile the kind skills (`model`, `deck`) were one page of judgment with zero library
mechanics — the exact gap Anthropic's document skills fill for claude.ai.

The reference corpus was studied directly (github.com/anthropics/skills). Its four document
skills (docx/pdf/pptx/xlsx) are **source-available, proprietary — no copying, no derivative
works** ("shared … as a reference"); its creative/meta skills are Apache 2.0. Its technical
stack also does not transfer: docx-js and pptxgenjs (Node), pandoc, markitdown, and a
LibreOffice recalc/render loop — none of which exist in this frame, whose premise is a sealed
pinned toolchain. What transfers is the **harness pattern**: tight SKILL.md judgment, heavy
detail in `references/`, deterministic pre-flight checks in `scripts/`, and a
verify-before-publish loop.

## Decision

1. **The frame's toolchain grows a PDF lane**: `reportlab==4.5.1` (authoring; 4.5.x
   deliberately, not the weeks-old 5.0.0) and `pypdf==6.15.0` (read-back) join the pinned
   image. A format we can write but not re-read would ship un-verifiable artifacts, so both
   arrive together. HANDOFF §3 carries the row ("frame image only", the sandbar precedent);
   the shell tool's docstring and the selftest/integration import probes name them.
2. **A new agent, the `typesetter`** (one file + one import, the registry pattern):
   FAST_EXECUTOR, `produces=DOCUMENT`, tools incl. `frame.shell`, document suite. It builds
   `.docx` with python-docx and `.pdf` with reportlab — the writer's text lane stays exactly
   as ADR-019 shaped it, and the two roles cut cleanly on the description boundary
   ("prose answers" vs "formatted document files"). Known boundary, accepted: a **dialed**
   document (deliverable set) fast-paths to the writer and ships `.md` even if the goal says
   "as a PDF" — the dial's promise is determinism, we do not goal-sniff. The full planner
   (Auto, and every replan) sees the typesetter in its roster and routes format asks to it.
3. **Five skills carry the mechanics** — all **original text** (the license above forbids
   derivation, and the toolchain differs anyway): `model` and `deck` enriched in place
   (openpyxl / python-pptx mechanics + design judgment), `docx`, `pdf`, `page` new. Every one
   bundles a deterministic pre-flight checker (`scripts/check_*.py`) that mirrors what the
   house verifier will do — hardcoded totals and short `=SUM` ranges (model), off-slide
   shapes (deck), fake lists and empty bodies (docx), extractable text (pdf), the
   self-contained law (page) — plus a `references/` file for depth. The checkers open files
   read-only: the one-save-then-publish hash law holds.
4. **HTML ships as a page-in-a-`code`-directory**, not a registry change: the `page` skill
   teaches ONE self-contained `index.html` in a directory published as type `code`, carrying
   its own `test.sh` + a copy of `check_page.py` (the verify frame mounts no skills — a code
   artifact carries its own tests, which is the code lane's law, and the judge gets the untar
   view instead of a byte-count sentence). The `webapp` lane stays dead for v1; resurrecting
   it (registry `.html`, a viewer, real browser smoke) is a recorded follow-up, not a rider.
5. **PDF verification became real**: the document suite gained `extract_pdf` (lazy pypdf,
   mirroring the docx branch). A grounded PDF passes on its extracted text, a fabricated
   figure fails, and a **textless PDF fails extraction** rather than shipping unexamined —
   SKIP would have let a rasterized fabrication through the judge-promotion door.
6. **Deck verdicts hardened**: `slides:present` → `format:slides`. Having slides is
   structure, not substance; the old substantive name let a zero-source deck ride "it has
   slides" to a deterministic PASS. A sourceless deck is now honestly UNVERIFIED and the
   judge decides on criteria (the promotion path ADR-019's addendum defined).

## Seams fixed en route

- `POST /v1/skills` silently dropped `files` (only PUT forwarded them; the CLI happened to
  always PUT). Fixed + endpoint-level regression test.
- `computer dev frame-selftest` hardcoded `DockerFrame` — the one instrument that answers
  "is the frame a sandbox" was blind to the production driver. It now builds through
  `new_frame()`, prints the driver, and tolerates both drivers' denial shapes.
- `just sandbar-dev` lacked `SANDBAR_EGRESS=on`, so the local daemon refused every frame
  whose task carried an allowlist — which is every real task.
- `parse_dir` now skips `__pycache__` droppings: a locally byte-compiled skill script used to
  make the next `just migrate` refuse the whole skill under the path law.

## The gates, and how builtins update

Seeded builtins are born published + green (`seed_builtins` → v1 PUBLISHED); the 409
green-evals gate guards the user-push path only. The discipline for builtins is therefore
**convention, enforced at authoring time**: every changed skill must pass 100% of its own
routing cases (`computer skills test <name>`) and the aggregate `just evals --suite routing`
must clear 95% before the sources merge. This round: all seven touched skills 3/3 against the
full 16-skill menu; aggregate **47/48 = 98% PASS** — the one miss is a pre-existing
`compare`↔`organize` boundary ("which of these tools should we choose and why"), untouched by
this workstream and left for its own pass.

Edited builtin **sources** reach an existing dev DB only via `computer skills seed --force`
(drops `origin=builtin` rows, re-seeds v1). Two caveats, accepted: run it worker-quiescent (an
in-flight task pinned `model@1` would silently see the new body), and a user-pushed `model@2+`
keeps shadowing the reseeded v1 as head — both dev-only shapes.

## Doc drift recorded (HANDOFF corrected)

§3's dev/test line claimed "`playwright` lives inside the frame image only" and §10 promised
"pandoc / node for web apps" and "Playwright smoke" — none of these were ever in the shipped
image or backend. §10's toolchain line now names what the image actually pins;
`SmokePolicy.BROWSER` remains declared-never-read (ADR-015's honest inventory).

## Consequences

- Every format in the user's vocabulary — docs, spreadsheets, decks, PDFs, HTML — now has a
  producer, a skill teaching its mechanics, a pre-flight checker inside the frame, and a
  verifier that actually reads the bytes back.
- The frame image grew ~6 MB (two pure-Python wheels); the docker driver re-tags by
  Dockerfile hash automatically, and the sandbar path needs the recorded manual
  `docker build -t computer-frame:latest backend/computer/ai/frames/image`.
- 48 routing cases now guard a 16-skill menu; `prompts/router.md` carries one added hint line
  for the file-format skills.

## Amendment 2026-11-15 - the typesetter is the maker

The typesetter is retired (ADR-091): a `.docx`, `.pdf` or `.html` document is the maker's, built with the
`docx`, `pdf` or `page` format skill, which now carries the typesetter's craft.

**Amended 2026-11-15 (ADR-092):** `prompts/router.md` is deleted with the router. The routing cases are now scored on the Lead, decision-only (`operations/skill_selection.py`): a case passes when the Lead's plan step names the skill or its reply reads it.
