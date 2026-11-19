# ADR-030 — Figure pre-generation: the outline is the index; the images fly in parallel

**Status:** accepted · 2026-08-13
**Context:** figures were generated INSIDE assembly — the last step on the critical path —
one at a time, between agent round-trips. The first live runs measured the cost: the deck's
assembly phases were ~20 of its ~28 minutes; and because every assembly retry starts in a
fresh sandbox, **every retry re-bought every image** — the deck generated its five figures
three times (15 calls, ~$0.75 for $0.25 of work), and those wasted rounds helped push its
first assembly over the 50-request budget cliff. The user asked for the obvious better
shape: index first, then parallel generation.

## The design

The index already exists: the architect's `OutlineResult` enumerates every `FigureSpec`
(stable `fig-sNN-N` id, what it depicts, caption hint) before any section is written, and no
image depends on section prose. So at the outline's completion — beside the verification
enqueue, after the commit like every enqueue — execution enqueues **`pregenerate_figures`**
(`figures-{task}-t{n}`, deterministic id, on the verification queue: post-answer side work
that never starves the step lanes).

The job (`operations/figures.py`): re-reads the outline from the EXPAND plan-edit payload
(the durable record, not a re-ask), composes each prompt **deterministically** (spec +
caption + the bible's first line — no model call in the job), fires every generation
**concurrently** through the same `ImageCandidate` chain/wire/throttle machinery the in-frame
tool uses (per-provider concurrency limiter; per-candidate `units_emitter` so Rule 8 holds,
attributed to the outline step), then opens ONE lightweight frame — the QA composer's
pattern, so images still enter through the only door: `frame.publish` → manifest →
`_harvest` — publishing each PNG as a **working artifact on the outline step**. The existing
`outline → assembly` edge (ADR-028) mounts them into **every** assembly attempt: retries
reuse them for free, forever.

Assembly's contract flips from "generate each figure" to "each figure is already mounted at
`/task/inputs/fig-*.png` — copy and embed"; `generate_image` stays granted as the named
FALLBACK for a straggler (the race where assembly starts before a figure lands) or a lost
job. The degrade ladder is unchanged and total: lost figure → placeholder caption; lost job
→ all placeholders; **never the task**.

## What it buys

Wall-clock: N × (10–30 s + 2 agent round-trips) leaves the END of the critical path and
hides behind the section + harmonize waves (images finish in ~one generation's time, all
concurrent). Money: each image is bought once per turn, not once per assembly attempt.
Request budget: assembly sheds 2–3 rounds per figure — directly relieving the
`UsageLimitExceeded` pressure that killed the first deck. Stale wakes are guarded (the job
no-ops on a turn mismatch, the stale-writer law), and a task whose outline names no figures
enqueues nothing.

## Verified

`just check` green (backend + web) · 840 unit green · registered in `jobs/all.py` +
dispatcher (SCREEN 3 stays honest) · LIVE PROOF (task d11e341d, 2026-08-13): a 15-page extended
document — ALL FIVE figures landed in one concurrent batch at 11:03:08, while the section
wave was still finishing (sections done 11:03:19–32) and before the seam passes closed;
assembly made **zero** generate_image calls (embedded from mounts) and its one retry reused
the mounted figures for free; delivered through QA first-pass at 11:11:31 — 12.5 minutes and
$4.47 end to end, versus the same-day pre-030 baseline of ~15 minutes, $6.30 and every
figure bought twice.

## Slides amendment (same day)

The deck lane's IMAGE LAW: every content slide gets its own `FigureSpec` (the per-section
figure cap rose 3→8 to fit a slide-group), each slide's writer places its marker inside its
slide block, and `md_to_pptx` embeds full-bleed. LIVE PROOF (task a26110c4): a 24-slide ask
delivered **25 slides with 25 pictures — an image on every slide** — 23 with speaker notes,
first-pass QA, $8.80. The concurrent batch landed 12/24 (OpenAI's images-per-minute quota
throttled the greedy fan; xai:grok-imagine 404s — stale model id, catalog residual) and the
assembly fallback covered the rest exactly as designed; the job now runs a SECOND SWEEP —
one bounded retry pass for only the failed figures after a 30 s breath — so future batches
clear the rate window themselves.

## Post-review amendments (2026-08-13, same day)

The review's CRITICAL: the job bound `units_emitter` per candidate and never CALLED it —
every pre-generated image was an unmetered provider call, invisible to the ledger, the
Bursar, and the $12 kill-cap (the live proofs' dollar figures undercounted accordingly).
The fan-out is now a pure core (`generate_batch`) that emits per successful wire return,
with fake-wire unit pins holding Rule 8 on it. With it, the money guards the design implied:
the enqueue is HELD when the expansion cost checkpoint will park the task (the gate's accept
releases it; decline never buys a pixel) · the job spends only while its task is RUNNING on
its own turn — re-checked before the second sweep and before publish · re-entry is
idempotent (a figure already on the outline step is never re-bought) · every wire call wears
`IMAGE_TIMEOUT_S`. Correctness with it: figures publish to the turn's NEWEST done outline
step (ordinal-first pinned a follow-up's images to turn 1's step, which no current edge
mounts — the silent full fallback regression) · `FigureSpec.id` is pattern-locked (it
becomes a frame path and a marker grep) · the estimate prices the IMAGE LAW (one figure per
content slide, not /8) and the gates bill at the LIVE price book, so the consent threshold
fires on real numbers · the stale `grok-imagine` seed id is corrected to `grok-2-image` ·
the web folds working-role images at the reducer: shelf and record yes, transcript cards
and reader-hijack no.
