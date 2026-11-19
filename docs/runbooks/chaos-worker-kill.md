# Chaos drill — kill the worker mid-run (2026-08-09)

**Status:** executed · four SIGKILLs across three turns of one long-form task · two real defects
found and fixed live · drill halted by the operator after the fixes; the final turn ran clean as
the performance benchmark.
**Scope:** the ADR-027 standing exercise — *kill the worker mid-wave and watch the run deliver
without hands* — executed against the long-form lane (ADR-025) on the dev host, application
version pinned `dev`.

This runbook is both the record of what happened and the procedure for running it again.

---

## 1 · The procedure (repeatable)

1. Create a long-form task (`scale=long`, 10–15 pages, `clarify=never` so no intake gate parks
   it) with a `client_token` naming the drill.
2. Watch the plan: outline step → `plan.edited` EXPAND → N section steps + assembly.
3. `kill -9` the worker at the chosen phase. The honest phases, in rising order of interest:
   mid-outline (single inline child), mid-wave (parallel queue children), mid-assembly
   (disk-to-disk build), during QA.
4. Restart the worker (`prismoid run worker`). Watch for the two adoption lines:
   `Application version: dev` and `Recovering N workflows from application version dev`.
5. Pass = the task reaches `delivered` with **zero manual intervention** after the restart,
   done work not re-run (attempt counters tell the truth), and the ledger shows no
   post-failure spend.
6. Kill triggers must distinguish their exit reasons: *fire only when the task is `running`
   AND ≥2 sections are `running`*; a `failed`/`delivered` escape must say so and not kill
   (the first trigger in this drill printed "WAVE CONFIRMED" on a failed-exit — drill tooling
   bug, corrected the same hour).

## 2 · The subject

Task `76e4ba32-c1d3-48e9-b298-4900be1fa4cb` — *"The Box That Rewired the World"*, a 12-page
document on the standardized shipping container. `scale=long`, `clarify=never`,
`client_token=chaos-drill-2026-08-09`. Plan shape: 1 outline (architect) → 6 sections
(parallel wave) → 1 assembly (docx via the `assemble` skill) with QA.

## 3 · Timeline of record (all times UTC)

### Turn 1 — kill #1 (mid-outline) and finding #1

| At | What |
|---|---|
| 09:08:27 | Task created; turn 1 planning → outline step running |
| 09:10:01 | **KILL #1** — SIGKILL pid 53345, architect mid-model-call |
| 09:10:40 | Restart. `Recovering 2 workflows from application version dev` — **adoption proved**, ~39 s outage |
| 09:10:4x–09:11:4x | Recovered outline attempt: the architect probed `/task/inputs/` on a task with **no attachments** — `read_file` raised `ModelRetry` per miss, pydantic-ai's `retries=3` exhausted **in seconds**, three model attempts burned back-to-back ("Tool 'read_file' exceeded max retries count of 3") |
| 09:11:47 | Attempt 4 = ESCALATED. `infra_attempts` accounting **validated**: attempt 4 = 3 model + 1 infra — the caps counted exactly as ADR-027 §4 promises |
| 09:11:48 | **KILL #2** — SIGKILL pid 65435 (operator-ordered; landed post-escalation) |
| 09:13:55 | Restart on fixed code. `Recovering 1 workflows` → turn 1 **fails honestly** with the escalation reason |

**Finding #1 (fixed before the restart):** `frame_tools.read_file` turned `FileNotFoundError`
into `ModelRetry` — and a retry-counted error on a *sourceless* task is a run-killer, not a
nudge. A missing file is an **answer**: the tool now returns an instructive result string
naming the missing path, listing the actually-mounted files (or stating there are none), and
telling the model to proceed from its own knowledge. Cost of the old behavior: three model
attempts ≈ $0.04 and the whole turn. Cost of the new one: zero.

### Turn 2 — kill #3 (mid-outline) and finding #2, the twin resurrection

| At | What |
|---|---|
| 09:14:13 | `tasks retry` → turn 2 |
| 09:15:21 | **KILL #3** — SIGKILL pid 69059, architect ~65 s into its call |
| 09:15:43 | Restart. `Recovering 2 workflows` — and here the plane grew a second head |
| 09:15:47–48 | The event record shows the outline's opening round **twice** — identical reads (`/task/context.md`, `/task/thread.md`, docx SKILL) and identical usage (3,387 in / 148 out) under distinct seqs, **both billed**. DBOS recovered the enqueued child *and* replayed the parent, whose inline re-invocation executed the same step a second time: **twins** |
| 09:18:54 | The real twin finishes: outline published, `plan.edited` EXPAND, 6 sections + assembly created, wave READY |
| ~09:18:5x | The zombie twin, still executing, **asks a question from beyond the grave** (gate `55ed1813` "One question before I continue") and parks — flipping the task `waiting`, so the READY wave never dispatched |
| 09:19:54 | Gate TTL auto-resolves → ADR-027's gate-hydration path resumes the zombie "with the user's answer" — the machinery worked; its caller was illegitimate |
| 09:19:55 | Zombie's resumed attempt concludes, tries `done → retrying` on the row the real twin already settled → `InvalidTransition` → propagates to the parent → **task FAILED** — a task whose outline had *succeeded* |
| 09:19:56 | Kill #4 fired by the drill trigger's escape-clause race — on an already-failed task (tooling bug, §1.6) |

**Finding #2 (fixed before turn 3):** post-recovery **twin execution** of one step. The loser
of the race can bill duplicate rounds, ask questions that park the task, and finally corrupt
the run with an `InvalidTransition` at the outcome boundary — *deeper* than any entry triage,
because the gate resolution smears `resume_state.answer` onto the **shared row**, which
bypassed both the RUNNING-only gate pre-triage and the state chain. The fix is a guard at the
**very top** of `execute_step`, outranking the gate and resume ceremonies: a step already
DONE/SKIPPED reconciles as a logged no-op, wipes the stale `resume_state`, and returns a
normal string to the parent. The row's state machine is the arbiter; the loser loses quietly.
Residual, accepted: the raced round's duplicate billing (~$0.02 here) before the first
settled checkpoint — the guard stops the damage, not the first duplicated call.

### Turn 3 — clean run (the benchmark)

| At | What |
|---|---|
| 19:30:31 | Worker restarted on twin-guarded code (nothing to recover — all settled) |
| 19:30:43 | `tasks retry` → turn 3, both fixes live |
| — | Operator halted the drill; no further kills. Turn 3 runs undisturbed as the clean benchmark (§5) |

### The standing scenario — kill mid-wave (executed on operator order, same day)

Fresh task `94424e5a` — *"The Cold Chain: How Refrigeration Remade What the World Eats"*,
12 pp, `scale=long`, `clarify=never`. One human touch before the wave: the architect asked a
real question (its 800-word section floor vs the 3,600-word budget — gate 20:15:35, answered
20:16:42 with the 5-section compromise). Everything after the restart was hands-free.

| At (UTC) | What |
|---|---|
| 20:19:21 | Outline done → EXPAND → **all 5 sections `running` at 20:19:22** — a true 5-wide wave |
| 20:19:24 | **MID-WAVE KILL** — SIGKILL pid 46542, five writers seconds into composition, five live frames orphaned |
| 20:19:42 | Restart. **`Recovering 6 workflows from application version dev`** — parent + all 5 children adopted; ~18 s outage |
| 20:19:43–44 | All five sections re-anchored `running` (attempt 2) — kill → wave-resumed in **19–20 s** |
| 20:20:10–17 | All five done (26–33 s of honest re-writing each) |
| 20:20:18–20:22:04 | Assembly #1 → QA #1 **revise** (short length + figures flagged) |
| 20:22:04–20:27:36 | Rebuild → QA #2 revise → `MAX_QA_ATTEMPTS` → **delivered with visible QA notes at 20:27:36** |

**PASS.** Delivered 8 m 12 s after the kill with zero post-restart intervention. The
`plan_steps` receipts prove the accounting to the row: every killed section
`attempt=2 / infra=1 / model=1` (the SIGKILL charged infrastructure, never the user's
ladder); assembly `attempt=2 / infra=0 / model=2` (its retry was the QA revise — correctly
model-flavored); the outline `attempt=1 / infra=0` (finished before the kill; not re-run).
No duplicate sections, no twin-guard activations (this recovery ran clean), no ghosts.
Run cost $4.06 · 76 calls. Deliverable honest-but-short: 2,853 words ≈ 6.8 pp of a 12 pp
ask, 0 figures (no funded image key on this host) — QA flagged both, twice, and the
delivery note says so; the width/figure gaps are content findings, not durability ones.

## 4 · What the drill proved

- **Version pin + adoption (ADR-027 §1):** every restart adopted in-flight work — recoveries
  of 2, 1, 2, 0 workflows; ghost count 0 throughout; outages 39 s / 26 s worker-restart to
  first recovered activity.
- **Recoveries are not retries (§4):** the escalation arithmetic held live — attempt 4 = 3
  model + 1 infra; no user-ladder rungs spent on SIGKILLs.
- **Honest failure:** a doomed turn fails with the real reason on the record; `tasks retry`
  starts the next turn cleanly.
- **Gate-row-as-durable-truth (§2):** the hydration path did exactly what it was designed to
  do — including for a caller that shouldn't have existed, which is how finding #2 surfaced.
- **Two real defects** would never have been found by unit tests: both required a SIGKILL at
  a precise phase plus a specific task shape (no attachments; a recovery race). This is the
  argument for keeping the drill standing.

## 5 · Benchmarks — clean turn-3 run (no kills; the operator had ended the drill)

End-to-end **19 m 26 s** (`task.continued` 19:30:44 → `task.delivered` 19:50:10) for a
12-page ask; delivered 4,243 words ≈ 10.1 pp, 15 headings, with visible QA notes.

| Phase | Wall | Cost | Tokens (in / out) | §15 budget | Verdict |
|---|---|---|---|---|---|
| Plan (deterministic longform_plan) | 1 s | — | — | — | ✓ |
| Outline (architect, Opus, 7 calls) | 6 m 01 s | $1.69 | 134k / 29.6k | <90 s | ✗ over — see note 1 |
| Expansion (outline → wave READY) | <1 s | — | — | <2 s | ✓ |
| Section wave (4 writers, Haiku, fully parallel) | **30 s** | $0.11 | 36k / 7.3k | <5 min/section | ✓ (25–30 s each) |
| Assembly #1 (typesetter → docx) | 3 m 07 s | $0.85 | 647k / 19.4k | <8 min | ✓ |
| QA pass #1 (Opus, 4 calls) | 43 s | $0.48 | 55k / 3.5k | ≤300 s | ✓ — verdict **revise** (truncation blocker) |
| Assembly #2 (rebuild per QA findings) | 8 m 13 s | $1.11 | 859k / 22.4k | <8 min | ✗ marginal — note 2 |
| QA pass #2 | 50 s | $0.51 | 55k / 4.8k | ≤300 s | ✓ — **revise** again → `MAX_QA_ATTEMPTS` → delivered with visible notes |
| **Turn 3 total** | **19 m 26 s** | **$4.76** | 1.79 M / 87k | 30 pp <25 min | ✓ |

Whole task across all three turns (the chaos tax included): **112 calls · $6.65** — the two
killed/failed turns cost $1.90, of which the two fixed defects burned ≈ $0.06 directly
(3 wasted read_file-loop attempts; 1 duplicated twin round) and the rest is honest work that
the kills discarded (two outline generations).

Kill → recovery latencies measured across the drill: **39 s** (kill #1) and **26 s**
(kill #3), SIGKILL to first recovered activity, both with `Recovering N workflows from
application version dev` on the boot log.

Notes, honestly held:
1. **Outline over budget 4×**: turn 3's architect wrote a ~30k-token outline across 5
   generation rounds against a three-turn thread. The §15 90 s row was written for a fresh
   task's outline; a retry-turn's outline carries the whole failed-thread context. Both facts
   can be true — the budget row needs a per-turn qualifier, or the outline needs a leaner
   retry prompt. Filed as the drill's benchmark finding.
2. Assembly #2 includes a ~5 m 24 s silent span (19:42:03→19:47:27) inside the frame — the
   docx build + rendition, not model time. Within wall-clock bounds, but it is the longest
   dead-air window in the run.
3. **0 embedded figures** in the delivered docx (4 were planned): `generate_image` has no
   funded key on this host (OPENAI/XAI keys empty, Google free-tier) — the environmental gap
   already recorded in STATE; the degrade path held (missing figures cost captions, never the
   build). QA's truncation/figure flags and the ⚠️ delivery note surfaced it to the reader.
4. Cosmetic: each section's title renders twice (section file H1 + assembly walker re-adds
   it) — one-line fix in the `assemble` skill's walker, filed.

## 6 · Residuals

- Duplicate billing of the raced round between recovery and the first settled checkpoint
  (bounded, logged via the twin-guard warn).
- The zombie's gate lingers until TTL if the twin dies before asking — harmless (gates expire),
  but a settled-step sweep of open gates is a named candidate if it ever shows up in triage.
- ~~Mid-wave kill never landed~~ — **resolved same day**: the standing scenario was executed
  on operator order against task `94424e5a` (§3, last section) and PASSED — five writers
  killed mid-composition, all adopted on restart, delivered hands-free 8 m 12 s later with
  row-level `infra_attempts` receipts.
- Section writers undershoot: the cold-chain run delivered 2,853 words of a ~4,000-word plan
  (the container run overshot before ADR-027 §5 added the ceiling; now the floor needs the
  same teeth). QA catches it, but catching is not preventing — filed as the next content
  finding.
