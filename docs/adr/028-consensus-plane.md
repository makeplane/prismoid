# ADR-028 — The consensus plane: typed joins, the harmonize wave, the master consensus, and the panel

**Status:** accepted · 2026-08-09
**Context:** after the chaos drill (runbook `chaos-worker-kill.md`) proved the workflow plane
durable, the user asked for the next octave: *multi-model orchestration with a master
consensus, output that never reads as patchwork, and a formal answer to "state machine, DAG,
or something stronger?" — extremely scalable and performant, no architectural compromise.*
A three-sweep research pass (with sources; unpublished) grounded every decision below. The engine's substrate — code-owned DAG + per-step
FSM + DBOS durable workflows + append-only record — is exactly the architecture the field
converged on; this ADR does not replace it. It closes it, and adds three plan shapes on top.

## 1 · The join law is typed, pure, and total (`ai/policy/joins.py`)

The DAG's one hardcoded join ("all upstream done", softened by `tolerates_missing_inputs`)
becomes a declared, per-node semantics — `StepSpec.join`:

- **`all`** — every producer DONE; `tolerates_missing_inputs` keeps the fleet chair's exact
  pre-028 contract (settled + ≥1 done).
- **`quorum`** — settled + ≥1 done promotes; `join_quorum` (the assembly's 0.8, now stamped
  ON the spec by expansion) is enforced by the EXECUTOR's `assembly_ready_guard`, which can
  write a diagnosis. Promotion never holds the floor: a settled wave below quorum has nothing
  left to complete, so a promotion-layer refusal would strand the graph in silent WAIT
  forever — the executor's refusal is an honest, legible task failure. This distinction was
  found by writing the law's truth table, not by an outage.
- **`consensus`** — settled + ≥1 done: the merge judges whatever proposals survived; a lost
  proposal costs a voice, never the merge.
- **`primary_input`** outranks all three: a step that exists FOR one upstream (a harmonize
  pass for its own section) is SKIPPED at promotion the moment its primary settles without
  DONE, and WAITs while the primary runs — neighbours are context, never a reason to run.

`decide()` is pure; `tests/unit/test_joins.py` walks the whole truth table including a
3,072-combination totality sweep. `_promote_dependents` only carries out the verdict, under
the same task row lock as ever.

**The entry triage is now total.** The drill's twin bug was a missing `(state, event)` edge;
statechart discipline says every persistable state defines its re-entry. Added: `RETRYING`
(killed between rungs → finish the walk, infra rung), **`VERIFYING` → resume-at-review** (the
produce is durable — artifacts are re-read via `Artifact.for_step` and fed straight to the
review arm; a kill during QA no longer re-buys a paid-for assembly, and no longer kills the
task on an undefined edge), `ESCALATED` (faithful replay of the recorded failure). `PENDING`
stays loud — dispatch only enqueues READY, so being there is a promotion bug.

## 2 · The harmonize wave (anti-patchwork, the AutoSurvey pattern)

The strongest production lesson in the literature is verbatim ours-in-waiting: parallel
section-writing produces disjoint reports, and neighbour TITLES alone don't fix it — writers
need neighbour TEXT. One-shot rewriting caps at the output window, so at 30–110 pages the
scalable answer is the harmonize wave: after the section wave, one `harmonizer` pass per
section (lane_role `"harmonize"`, FAST_EXECUTOR, itself fully parallel) reads its own section
plus its neighbours' actual text plus the bible, and returns the section's final form under a
hard law: **delete and bridge, never regenerate** — whole-document revision measurably
corrupts 16–27% of untouched content, so the pass exists to remove patchwork and is forbidden
from creating its own. One model family owns the pass for a document: the final voice is
singular by construction.

Graph shape: sections keep their direct edges to assembly (a lost seam pass costs the POLISH
— the walkers fall back to the raw file, never a gap where finished prose exists); each pass
gets its ≤3-section window + the bible, and feeds assembly; the outline now also edges into
assembly (the bible reaches the build and the QA read — previously a latent gap). The pass
publishes `NN-… (final)` → `nn-…-final.md`; `load_sections`/`concat_md` prefer `-final` per
number IN CODE (and note `-final.md` sorts *before* `.md` — last-wins would have chosen
wrong). Third-strike harmonize failures SKIP like sections and fleet seats: the loss costs
the part. Single-section documents skip the wave entirely (no seams).

Pre-draft, the same research ranks outline DETAIL as the largest measured coherence lever
(+22.5 pp): `SectionOutline.not_here` (the scope fence — what a section must NOT cover
because a sibling owns it) and `OutlineResult.voice_exemplar` (style by example, not
adjectives) join the schema, travel in `outline.md`, and are demanded by the architect's
contract.

## 3 · The master consensus (`quality=max`)

The user's instinct — "different models per section, with a master consensus" — split
against the evidence: mixed-model per-section drafting LOSES to the single best writer
(Self-MoA; frontier error correlation is rising), while consensus pays at the **master**:
outlines are small, near-discrete, cheaply arbitrated — and an outline error fans into every
section, making it the most expensive error in the lane.

So: at `quality=max`, planning reads the catalog (data, the fleet's pattern — `ai/policy`
stays pure) and seats up to three architects from DISTINCT families, each pinned
(`model_ref`), each `lane_role="outline_draft"`, proposing INDEPENDENTLY in parallel. Their
typed outlines publish as `proposal-<ref>.md` (never `outline.md` — that name is the merge's
to claim, and only the merge's outline may expand the plan). The merge step
(`join="consensus"`) mounts the surviving proposals and produces THE outline, recording every
material divergence and its ruling in the outline's summary — divergence is a surfaced
signal, never a silent average. Fewer than two families ⇒ the classic single-architect shape,
by construction. Prose-level model mixing is deliberately NOT done.

## 4 · The panel (QA grows two more chairs)

Judges measurably favor their own family's prose, and this lane's producer is always one
family — so a same-family QA gate is a documented bias, now retired. The QA read fans to
**up to three `qa` readers from distinct families** (chain order picks each family's best
LONG_CONTEXT candidate), run CONCURRENTLY — wall-clock of one read — and a pure merge law
(`qa_policy.merge_reports`, PoLL-shaped) reconciles: any BLOCKER from any judge → revise
(a blocker is span-anchored and checkable; a pass is an absence); else strict majority; an
even split revises only on MAJOR+ findings (minor-only dissent ships). Findings union,
deduped by (location, kind), blockers first. A lost judge costs a voice; all lost → the
existing PASS_UNREAD path unchanged. One family in the catalog = a panel of one — honest
about the host.

The finding vocabulary adopts the published long-form coherence taxonomy (BooookScore's
eight): `duplication`, `discontinuity`, `salience`, `inconsistency` join the kinds, and the
prompt now requires SPAN-anchored locations ("section 04, at 'The reefer fleet grew…'") so
every finding is an executable edit instruction, plus an anti-sycophancy line for panel
independence.

## 5 · What was deliberately NOT built

Per-section multi-model consensus and layered MoA (superseded by Self-MoA); multi-round
debate anywhere; whole-document rewrite as harmonization; LLM-decided graphs; a Petri-net or
actor rewrite of the engine (its headline win — models out of routing — we have by
construction). The deferred register gains: per-section-type routing once per-type evals
exist; best-of-N with cross-family selection for bookend sections; DOC-style leaf-level
outline depth as a dial.

## Cost & performance honesty

Consensus outline: +1–2 outline generations (~$0.3–1.7 each) + one merge — bounded, parallel.
Harmonize wave: ~2× section tokens — the CHEAPEST phase ($0.11 of $4.76 in the benchmark
run) — fully parallel, +1 wave of wall-clock. Panel: PoLL's economics — small judges at 7–8×
lower unit cost buy three-for-one vs one big judge; concurrent, so wall-clock unchanged. The
graph grows from N+2 to 2N+4 steps (+2 proposals at max): well inside queue width, and every
new step type is workload the queues already pipeline.

## Verified

`just check` green · 725 unit (join truth table + totality sweep; panel merge law; expansion
shape) · `just test` 1,048 (two pre-existing stale 2-tuple stubs in the reliability guard
updated to the ADR-025 signature — exposed, not caused, by this work) · assemble walkers
prove `-final` preference on disk.

**Live proof campaign — task `3ff14473`, five turns, 2026-08-09/10.** The consensus shape
materialized exactly (2 family-pinned `outline_draft` proposals + consensus merge), the merge
surfaced a REAL cross-family epistemic divergence as a decidable gate (Gemini proposed
precise ungrounded figures; Anthropic proposed established-knowledge hedging — ruled by the
human), and the clean final lap delivered: outline → 4 parallel sections → 4 harmonize
passes (first-attempt) → assembly consuming `NN-…-final.md` → panel QA with **reviewer
identity on the durable record** (`qa.reviewed … reviewers: claude-opus-4-8 (anthropic)`) →
delivered. Final docx: 4,471 words ≈ 11 pp of a 12 pp ask, **zero duplicated headings**
(turn 2's build, pre-fix, had four).

The campaign found and fixed, live, five instruction/infra defects — each now wearing its
comment in code: (1) the merge's divergence record was ordered into `summary`'s 500-char
clamp — 12 straight schema rejections; → `OutlineResult.divergence_note`, sized for the job;
(2) the span-quoting instruction exceeded `QAFinding.location`'s 120-char clamp — the whole
panel starved; → 200 + a stated budget in the prompt (the same instruction-schema disease,
both directions, one session); (3) harmonize files published under their prose H1s —
invisible to the walkers' `-final` glob; assembly ingested raw AND final (doubled headings,
40 % overshoot); → declared-title law extended to the harmonize lane; (4) `_slug_title`'s
60-char clip ate the `-final` suffix off long titles — caught by arithmetic pre-ship; → the
suffix is structural, never slug-derived; (5) a streaming 429 escaped as raw
`httpx.HTTPStatusError`, killed a pinned seat's whole task, AND broke DBOS's exception replay
(`TypeError` on reconstruction — required-kwarg constructors don't rehydrate); → httpx
translated to `ModelIncapable` at `agents_run`'s boundary + exception hygiene at the
`run_step` journal boundary (nothing crosses unless it reconstructs from a message).

Residuals, named: Google's structured-output converter 400s on `QAReport` (seat retired on
this host until investigated; the panel degrades honestly); FIRST_EXCEPTION sibling-cancel
can strand a sibling that had just parked on a gate (cancelled workflow + open gate +
WAITING task — the cancel must also expire the sibling's open gates); one turn showed a
`failed` status frame later superseded by a park (event-order wrinkle, display-grade); the
stream replay BLANKED on this five-turn record — diagnosed: schema-vintage skew (the worker,
restarted with `location ≤ 200`, wrote a qa.reviewed frame the older API process could not
validate) and the replay parse is all-or-nothing, so ONE poison frame blanked the whole
flight recorder ("No transcript · 0 calls" against a $28 ledger; API restart healed it) —
two follow-ups filed: event-schema widenings ride an API restart (prod: deploy order beside
the version pin), and replay wants poison-frame resilience — skip WITH a visible
unreadable-frame placeholder and a warn, never a blank history, never a silent skip; QA
pass 2 of the final lap degraded to `pass_unread` (transient read failure — the law shipped
it with the note visible, as designed). Two more from the web's first read of the record: a
later turn's rebuild of a prior turn's deliverable emits `artifact.revised`, and the feed's
turn boundary meant the repoint found nothing — the final document was invisible in the turn
that made it (fixed: a revision with nothing to repoint becomes the turn's card,
task-view.ts + test); and turn 2's QA rebuild born a SECOND lineage for the same basename
(attempt 2's harvest missed attempt 1 as prior — dual-lineage residual, filed).

## Amended 2026-11-15: the harmonize wave is retired
The long-form lane writes its parts in order (ADR-025, amended 2026-11-15): each part reads the outline and
what the parts before it established, so there are no parallel seams to harmonize. The `harmonizer` agent and
the wave are removed; the `"harmonize"` lane role and `primary_input` stay readable for plans written before.
