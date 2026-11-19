# ADR-029 — Universal size buckets: five sizes, every artifact kind, and the no-push law

**Status:** accepted · 2026-08-13
**Context:** size was a binary door — `scale: auto|standard|long`, documents-only (`long` +
any other deliverable was a 422), with lane constants tuned for big documents. Three live
drills proved the middle broken: 12-page asks hit the flat 800-word section floor and burned
architect ask-gates ("expand to 16 pages or coarsen?") — the system pushing size upward *by
constant*. The user's directives: five explicit buckets for EVERY artifact kind; the system
never pushes size — explicit signals only; each bucket a target band + a hard ceiling; the
resolved size clearly marked everywhere; auto stays (nobody has to choose).

## The vocabulary

**`brief · standard · extended · long · book`** — one word set across documents, decks,
spreadsheets, collections and code. All five fit the existing `tasks.scale` String(8) column,
so this feature ships with **zero migrations**; the legacy stored value `"standard"` simply IS
the standard bucket and `"long"` keeps meaning long. `"auto"` remains the sentinel the
resolver replaces at first planning.

## The table (single source of truth: `schemas/sizing.py`, helpers `ai/policy/sizing.py`)

| kind / bucket | brief | standard (default) | extended | long | book |
|---|---|---|---|---|---|
| **document** (pages) | 1–2, cap 4 | 3–8, cap 12 | 10–25, cap 30 · **sectioned** | 30–60, cap 80 · sectioned | 70–120, cap 160 · sectioned |
| **deck** (slides) | 5–8, cap 10 | 10–16, cap 20 | 18–30, cap 36 · **sectioned** slide-groups + speaker notes | 32–50, cap 60 | 55–80, cap 100 |
| **spreadsheet** (sheets) | 1, cap 2 | 2–4, cap 5 | 4–8, cap 10 · **sectioned**, 1 section = 1 sheet spec | 8–12, cap 14 | 12–20, cap 24 |
| **collection** (rows; always one-shot) | 5–12, cap 20 | 10–30, cap 45 | 30–80, cap 120 | 80–150, cap 220 | 150–300, cap 400 |
| **code** | advisory scope words only — never enforced counts | ← | ← | ← | ← |

QA tolerance: brief ±50 % · standard ±40 % · extended ±30 % · long/book ±25 %. Sectioned
cells carry section bounds; the **section floor scales** — `max(400, total_words //
sections_hi)` — replacing the flat 800 (the gap-zone bug; the schema keeps 400–3,000 as the
absolute shape, `expand()` holds the bucket's arithmetic with a legible merge-don't-ask
message). Fleet allows brief+standard only ("fleet answers are single-breath"). Collections
allow long/book with honest judge-enforced bands — the organizer is one typed call and the
ceiling is stated here, not hidden behind a 422.

## Auto mode and the no-push law

`auto` (the default) resolves deterministically at first planning: a literal number in the
goal — "15 pages", "25 slides", "6 sheets", "40 rows" — picks the smallest bucket whose cap
covers it (the pages/words regexes inherited `longform.detect`'s input-consumption discount:
"summarize this 60-page PDF" scales nothing); otherwise **standard, always**. Adjectives
("comprehensive", "exhaustive") never move the bucket — pinned by unit test and live proof.
Numbers clamp DOWN to the bucket cap, never up. The resolved bucket is **stamped** on the
task row and shown everywhere: plan titles ("Design the document — extended · ~15 pages"),
the architect's acceptance, CLI `tasks show`, the web header chip, `TaskSummaryOut/DetailOut`
(the read-back the dial never had). The architect's law is **fit, don't ask**: merge sections
to fit the bucket; asking is for genuinely impossible asks, with options at-or-under the size.

## What the lanes do with it

One predicate family (`sizing.sectioned/fleet_allows/size_marker/size_clause`) replaced five
scattered `== "long"` string tests. Documents extended+ run the ADR-025/028 sectioned lane
(now entered at 10pp, the gap-zone fix). **Decks and spreadsheets extended+ run the SAME
lane** — sections stay markdown; only three things are kind-aware: the outline/section/
harmonize goal texts (slide-groups with `Notes:` paragraphs; sheet specs with pipe tables +
`Derivation:` lines), the assembly agent (typesetter/presenter/modeler, per the `KIND_AGENTS`
mirror) with two new assemble-skill walkers (`md_to_pptx.py`: dividers, one-idea slides,
speaker notes, figure placement; `md_to_xlsx.py`: one sheet per section, typed cells, bold
frozen headers — DATA ONLY, the modeler patches real formulas over every stated derivation),
and the QA precheck's extraction (python-pptx slides+notes; openpyxl sheets+rows — both
already in the frame image). Consensus outline, harmonize wave, typed joins, panel QA:
untouched, format-agnostic by construction. One-shot kinds carry their band as acceptance via
`size_clause` — which absorbed the collection-only `depth_clause`, ending the 10–30 triple
duplication (prompt + skill + policy).

## Defects swept in passing (each found in exploration, fixed here)

1. `qa:length` silently VANISHED when `target_pages` was NULL — now `qa:size` (per-kind
   units) always emits a row, "skipped — no size target" when unenforced.
2. An unsupported deliverable extension was a BLOCKER check → forced billed rebuild loop —
   now `exit(3)` = INFRA → PASS_UNREAD with the note visible ("we could not read it" is the
   runner's failure, never the document's).
3. QA's tolerance was a hardcoded 0.3 — now the bucket's own.
4. Stale "intake question" docstrings (the consent moment moved to the expansion gate in
   ADR-025) corrected.
5. `TaskDetailOut` never echoed `scale`/`target_pages` — the dial was write-only.
6. The CLI could not express size at all — `computer do --size … --units/--pages …` (create
   and follow-up), `tasks show` prints the marker.

## Verified

`just check` green per workstream (backend lint/types/audit + web tsc/vitest) · 840 unit
(58-test sizing suite: 5×5 table invariants, resolution law, no-push pins, clamp-down-only,
input-ref discount, per-kind regexes, floor property incl. the 15pp gap case; `qa:size` skip
row; renamed band tests) · contract + web types regenerated · assemble walkers proven on
disk (6-slide pptx with speaker notes reopened via python-pptx; 2-sheet xlsx with typed
cells reopened via openpyxl) · LIVE PROOFS (2026-08-13, all four): the no-push pin
("comprehensive, exhaustive, in-depth one-page memo" → `standard`, adjectives ignored) · the
extended DOCUMENT — "15-page report" auto-resolved `extended · ~15 pages`, sectioned lane with
ZERO size-arithmetic gates (the scaled floor's whole point), delivered 6,112 words with FOUR
real generated figures (the image lane's first live document), QA revise→pass, $6.30 · the
extended WORKBOOK — "6 sheets" auto-resolved `extended · ~6 sheets`, six sheet-spec sections →
harmonize → md_to_xlsx, delivered exactly the six asked sheets, $3.81 (residual filed: only
one real formula — the walker's data-only/modeler-patches handoff needs a formulas-vs-
Derivations QA check) · the extended DECK — "25-slide" auto-resolved `extended · ~25 slides`,
sectioned lane through md_to_pptx: EXACTLY 25 slides, speaker notes on all 25, five embedded
generated pictures; its turn-1 failure minted a permanent engine fix (UsageLimitExceeded →
attempt-death with a teaching gotcha, never a task-death) and its QA rebuild was cancelled at
the operator's $12/task spend cap — the deck ships as attempt 1 with QA's findings on the
record, the cap working exactly as ordered.

## Residuals, named

Deck/sheet estimate arithmetic uses conservative words-per-unit constants (130/600) —
correct from live telemetry when a few runs exist. Collections above extended lean on the
judge to hold the band (single-call ceiling stated; a sectioned collection lane is a named
non-goal until evidence demands it). Code buckets are advisory language only. `target_pages`
keeps its wire name while meaning "units of the kind" (documented here; a rename is
housekeeping, not semantics).

## Post-review amendments (2026-08-13, same day)

A three-lens review (backend / frontend / AI engineering) audited this landing; every
finding is fixed in the same tree. The law gained its missing half: **the turn-resolution
law** (`sizing.resolve_turn`) — this turn's dial > a fresh literal number in the new message
> the stamp, because the stamp is the resolver's own past answer and must never clamp the
user's "now expand this to 40 pages" down to the old bucket (`TaskContinued` now carries the
turn's dials so planning can tell a choice from a stamp). Fleet AND council obey the
single-breath law at the door and at planning (the clamp the docstrings promised now
exists, stamped honestly). The read-back is server-composed end to end: `size_marker` +
`deliverable` echo on the task Outs and the CLI/web chip print the marker verbatim
(`normalize`/`spec_for`/`size_marker` moved to `schemas/sizing.py` — api may not import ai).
Detection learned 4-digit asks, tabular source nouns, and unit-of-source prepositions;
collections carry an explicit row ask in their acceptance; the deck ask budgets its
structural slides (title + dividers) into the total QA measures. Walker seams closed: the
pptx walker keeps fig ids in extractable text on BOTH paths (the docx law), the xlsx walker
writes a mangled tab's full title as a bold A1 row, and `_cell` keeps percent/leading-zero
semantics. QA honesty: `has_notes_slide` before access, a missing parser is exit-3 INFRA,
and the unread note never claims a cleanliness nobody measured. All of it pinned in the
unit suite; the assemble skill's evals gained deck/workbook routing cases.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** Size resolves from the dials only (`schemas/sizing.resolve`: an explicit bucket, else an explicit count, else standard); the goal-number detectors are deleted, and the table's helpers (`bucket_for_units`, `runs_longform`, `fleet_allows`, `section_bounds`, `size_clause`) live in `schemas/sizing.py`.
