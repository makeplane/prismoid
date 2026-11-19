# ADR-025 — The long-form production lane: outline → parallel sections → assembly

**Status:** accepted · 2026-08-08
**Context:** a 30/60/110-page document was impossible on every route. The dialed path
one-shots a writer into the 16k output ceiling (~5 real pages) and delivers the shortfall
green — no length concept existed anywhere. Auto had no assembly vocabulary, `MAX_STEPS=12`
with a silent one-step fallback, and the verifier's own timeouts graded correct big documents
FAIL. The user's bar: hours are acceptable, the output must be crazy good, and the machine
must fan out — sections in parallel, images in the same run, durable end to end.

## The lane

1. **The door**: `TaskCreateIn.scale ∈ {auto, standard, long}` + `target_pages` (4–160).
   `long` enters directly; `standard` NEVER enters (even "200 pages" in the goal); `auto`
   enters only when deterministic detection reads a lane-sized page/word count out of the goal
   itself — vibes stay the planner's business. Fleet and the lane cannot combine. Documents
   first (user decision): deliverable ∈ {None, document}; deck/sheet assembly is the named
   next lane on the same rails.
2. **Deterministic like the fleet**: phase 1 is ONE step — the `architect`
   (FRONTIER_REASONER, typed `OutlineResult`: sections with executable briefs + word targets +
   figure specs, the continuity bible, front/back matter; system-published as `outline.md`).
   Phase 2 is `ai/policy/longform.expand` — PURE CODE — inserting ≤24 `section_writer` steps
   (born READY; the outline mounts into each) and one `typesetter` assembly (PENDING on the
   sections, `qa=True`, `verify=False`, `tolerates_missing_inputs=True`) via the plan-edit
   machinery (`PlanEdit(kind=EXPAND)` carrying the outline verbatim; a fresh `TaskPlanned`
   snapshot; the run_task loop unchanged — its next frontier read sees the wave). An outline
   that cannot expand fails its own ATTEMPT with a legible gotcha and rides the architect's
   ladder — no silent one-step fallback exists in this lane, structurally.
3. **Sections are markdown always** — that is what makes "decks and sheets ride the same
   shape later" true. `section_writer`: the writer's text lane narrowed — prose IS the
   section, `NN <title>` naming so files mount in lexical = document order, **no ask_user at
   the registry** (twelve parallel askers flipping the task WAITING is the stampede §14
   exists to prevent; a stuck section states assumptions inline). A section's third strike
   parks it SKIPPED (the fleet-seat path): the loss costs the section, never the document.
4. **Assembly: content flows disk → disk, never through model context** — the 16k ceiling
   makes model-retyped assembly impossible at scale, so the typesetter drives the `assemble`
   skill's walkers (`md_to_docx` / `md_to_pdf` / `concat_md`: headings, lists, tables,
   figures, generated TOC, visible gap notes for missing sections). **Figures generate AT
   assembly** (the typesetter already holds `generate_image`): sections carry caption markers;
   assembly generates and embeds — no image bytes ever cross frames, and a dead image API
   costs placeholders in one place. The 80% floor: below it the assembly guard fails the task
   honestly (a hollow book with a confident cover is worse than an honest failure); above it,
   gaps ship visibly.
5. **Working vs deliverable**: `artifacts.role`, stamped at harvest FROM THE STEP SPEC
   (model-unforgeable). Working rows (outline, sections) cross steps through the one publish
   door and show on the record, but never reach the shelf, never mount into follow-ups/forks,
   and never enqueue verification. *Amended 2026-10-06:* one follow-up mount - a sectioned
   follow-up's architect is shown, read-only beside the delivered file, the section markdown
   the last delivered document was assembled from (per number the `-final` file, else the raw
   one: `mounts.prior_sections`), and a writer mounts the one it keeps (`kept_from`) or its
   brief names. A kept section is that file, published byte for byte by the system (never a
   model's copy, never a .pdf or .docx no model can read), with no seam pass (its seams were
   settled the turn it was written) and no figure bought: assembly mounts the figures the last
   delivered document shows (`mounts.prior_figures`), which its markers point at. Seam passes
   and assembly never mount the section files; a fork still inherits only the deliverable.
6. **Money**: the consent moment is the EXPANSION-TIME cost gate (user decision) — a
   budget-kind gate with the real outline's numbers, opening only above
   `COMPUTER_WORKER_LONGFORM_CONFIRM_USD` (default $2) and skipped when an explicit task
   budget already covers the high estimate; it parks at the boundary the loop already owns
   and never re-asks. The Bursar additionally PROJECTS the remaining wave
   (`bursar.project`) — at fan-out width, spend-so-far alone was a receipt, not a budget.
7. **The receipts are carefully removed from this lane** (user decision 2026-08-08; reversed, see the
   2026-11-15 amendment below):
   `StepSpec.verify=False` on assembly — the pre-delivery QA gate (ADR-026) is the lane's
   quality layer. Careful = lane-scoped and reversible (standard + fleet receipts
   byte-identical; the verify machinery untouched; a per-task re-enable dial is the named
   deferral), integrity preserved (harvest sha256 + the one-save law are content addressing,
   not verification), and the trade named: no post-delivery grounding receipt on longform
   deliverables. The planned verify-at-scale work (chunked judge, size-scaled suite timeouts,
   derivation work-budget) is therefore NOT built — deferred with the dial.
8. **Scale plumbing that shipped with the lane**: event replay keyset-paginated past the
   1000-row cliff (a correctness fix — late-joining clients silently lost long tasks' tails);
   `step_concurrency` 12 (24 GB of frames; hosts size via env); shell exec wall clock → conf
   (300 s — an assembly build is minutes of work, not a hang); LibreOffice rendition ceilings
   240 s / 60 MB (the rendition is THE DOCUMENT and the document got long).

## Honest edges, recorded
Pause is wave-granular (the running wave completes; mid-wave ejection needs attempt-scoped
child ids — v2). Steers fold into not-yet-run sections' contracts only; structural steers are
a follow-up turn. The writer/section text lane still has no durable draft snapshots
(reconnect mid-section shows nothing until it publishes — bounded by section size; the
`taps.draft_checkpoint` design is recorded in the plan). Continuous dispatch (promote-and-
enqueue) stays v2 — for this DAG the wave barrier is free (nothing can start before the
join). The web renders the lane through existing machinery (`lane_role` rides
`PlanStepView`); the grouped tree, composer dials, and `qa.reviewed` rendering are the named
web follow-up.

## Amended 2026-11-15: written in sequence, past one run only
The lane now runs only past one run's output: a deliverable whose target (the asked units, or the bucket's
midband) is over `ONE_RUN_MAX_WORDS` (6,000 words, `schemas/sizing.py`) is written here; below it one writer
or maker run writes it. No document is written past `LONGFORM_MAX_WORDS` (30,000 words): the book bucket's
ceiling is 100 pages, and an outline whose word targets sum past it is refused with its fix.
- **The outline** is the writer in its `outline` mode (the `longform` skill's craft, typed `OutlineResult`); the
  `architect`, `section_writer` and `harmonizer` agents are retired (`base.ALIASES` resolves them to the writer).
- **Parts in order, not in parallel**: the outline expands into one writer step per section (its `section` mode),
  chained by `depends_on` edges (join `quorum`, so a lost part never stalls the next), then the maker's assembly.
  Every part's goal is the same bytes (the document's brief with the whole outline), the cached head of its
  prompt behind a cache point (`agents.run.Cached`); its assignment and what each earlier part established (the
  parts' typed summaries, `SubmittedPart.summary`, read back from their narrations) follow it. No seam pass.
- **Citations**: parts keep their `[S#]` marks; assembly's mount links them under one numbering and mounts
  the list as `NN-sources.md` after the last part (`longform.render_parts_mounted`), so the document has one
  Sources list.
- The pure planning code (`ai/policy/longform.py`, `longform_wiring.py`) moved into `operations/longform.py`.

## Amended 2026-11-15: a long document carries a receipt
Point 7 is reversed: assembly keeps the default `verify=True`, so the delivered document gets the same
`verify_artifact` receipt as any deliverable, signed after delivery on the verification queue and never
blocking it (ADR-019); the QA gate still runs before delivery. The receipt reads within the existing bounds:
the suite's deterministic checks read the whole file in its frame, the judge reads the suite's capped
extract, and the claim reader reads the parts assembly was built from (they keep the task's own `[S#]`
ids, which assembly renumbers), packed whole into readings of its document bound, one call per reading.
The whole book is read once and no call holds more than one bound. The assembly floor counts only the
parts that feed that assembly, so a revision never counts an earlier turn's parts.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The lane is entered by a size dial or by a Lead's long-form recipe step; the recipe stamps the row's size as the dial does, and the assembly takes over every edge that waited on the outline step, so a step after the recipe waits on the whole document.
