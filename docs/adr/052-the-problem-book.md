# ADR-052 — The problem book: RangeGolden and the eval tiers

**Status:** proposed · 2026-08-27 · built the same day (the book, three suites, the
doctrine gate, the cadence steps); the owner's direction on the record: *"a rock-solid
framework around testing — problems and kinds of problems — edge cases and outliers
covered — an important document going forward — and see if some evals should kick in NOW
or not."*
**Grounding:** an unpublished research note of 2026-08-27 (the harness/
benchmark/judge/statistics landscape + this repo's own eval seam map + a fork red-team of
the design — twelve findings, all folded). Siblings: ADR-049 (whose triage-golden lane
this ADR lands and whose journaled `route`/`route_source` is the flywheel's substrate),
ADR-050 (whose failure matrix maps into the book as armed rows), ADR-051 (whose
lane-registration law the book pre-pays and whose W1 registry is the enforcement hook).

**Amended 2026-11-15 (ADR-092):** a book row on a Lead turn grades the route log's `ending` (`step.state.ending`: reply, plan, ask, propose or fail), never which rule fired.

**Amended 2026-11-15 (the books become datasets):** the range, computer-routing, computer, navigate, machine and planner books are retired with their walkers (`triage`, `range`, `answers`, `planner`, `computer`, `computer-routing`, `computer-live`, the transforms sweep). Every row lives on as a dataset row (`evals/datasets/outcomes.jsonl`, or `web.jsonl` for the fixture-site rows) whose `source` and `source_id` point at the book line it came from; `evals/run.py` keeps no book suite (`--dry-run` lists the dataset suites), `just range` runs `--suite trivial50`, and growth is a new dataset row, never a book row. `chat_book` stays until the talk door goes.

## Context

The answer path now spans a range — a breath to a book — and every prior workstream left
behind its own fragment of proof: 21 triage goldens, a live range drill, selection evals
behind a 409, the golden hundred, chaos drills, failure matrices in three ADRs. What no
place holds is **the problems themselves**: the kinds of asks the product claims to
handle, the edge cases and outliers that break naive routers, the extreme prompts that
must keep working as Code, Design, and Bioscience arrive — one battery, with one law about
which checks may gate and which may only alarm.

The field's evidence (research doc §0) is unusually aligned with the house's own habits:
case data separated from grading logic; the verifier carried per row; deterministic tiers
gating and graded tiers alarming; suites seeded from real failures and grown from traffic;
judges given an honest way out; flaky cases quarantined with an owner, never silently
dropped. The golden hundred already practices half of this. This ADR makes the whole of it
law and gives the answer path its book.

## 1 · The constitution

> **Every problem kind the product claims to handle has a row; every row names its
> verifier tier; only deterministic tiers gate; graded tiers alarm on regression until
> their judge is calibrated; and the book grows from the record, never from imagination
> alone.**

Five clauses, each with its mechanism:

1. **One battery, many consumers.** `evals/range_book.py` — the golden-hundred format
   (literal `RangeCase` rows + a model-free doctrine test) — is read by the per-commit
   unit gate, the `triage` suite, the `range` suite, the `answers` suite, and the
   transforms sweep. There is no second case store and no second harness
   (`pydantic-evals`, declared and imported nowhere, is DECLINED and its §3 row flagged
   for removal).
2. **The coverage law, not a row count.** The book must hold: every `kind × plain`
   crossing · every triage-relevant ADR failure-matrix row (049 in full; 050's as armed
   scatter rows; 048's fires-never-express clause) · every found-live routing bug (each
   becomes a permanent regression row) · the owner's five, verbatim. **No row without
   provenance** (owner | audit | adr | drill | found-live | research | traffic). If the
   law yields 74 rows, the book is 74.
3. **The tier law.** `grading ∈ policy | plan | live | judged`, the verifier hierarchy
   made explicit: `policy` rows execute pure functions (`express_tier`, contract
   validation) and **gate `just check` at zero model cost**; `plan` rows execute the full
   `draft_plan` branch chain against a DB row with the planner stubbed — deterministic and
   model-free, but integration-tier, because the branches stamp the row (the red-team's
   split); `live` rows ride the real API and gate `just range`'s budgets; `judged` rows
   add a rubric judge and **may never gate anything** until the judge is calibrated
   against ~100 human-labeled traces (the named trigger). Regression on judged lanes
   alarms through `tools/evals_diff.py` — the number is an alarm, the diff is a lead.
4. **The adversarial oracle.** Strategy-transforms (homoglyph, zero-width, base64-wrap,
   mixed-language — authored once in `evals/transforms.py`, applied mechanically) assert
   route monotonicity: a transformed variant routes to its canonical route **or toward
   the anchor** — never to a LIGHTER route than its canonical (that is the steering
   attack, COLM-2025), and never to an error. Pure, so the sweep runs per-commit, free.
5. **The flywheel.** Every turn already journals `route · route_source · route_why`
   (ADR-049 §10); outcomes are on the record. Growth = weekly error analysis over real
   traces (open → axial coding, the Hamel discipline), silver labels Adaptive-RAG-style
   (which lane would have sufficed, judged by the outcome), new rows with
   `provenance=traffic`. The book is grown by looking, never by inventing.

## 2 · The case row (the anatomy, evidence-backed)

`RangeCase`: `id` · `version` · `kind` (chat · closed_qa · lookup · rewrite ·
summarize_extract · generate_small · deliverable · research · code · standing · operate) ·
`weight` (trivial → heavy) · `stress` (plain · boundary · ambiguity · contradiction ·
adversarial · degraded · thread · garble · abstention) · `provenance` + note ·
`prompt` verbatim · `dials` (create-time kwargs) · **`facts` XOR `turns`** (thread
context: policy rows carry `TurnFacts` overrides; live rows carry scripted prior turns
executed via real `follow_up` — the MultiChallenge shape) · `expected_route` +
`allowed_routes` · `must[]` / `never[]` (evidence asserts) · `budget_s` / `budget_usd` ·
`grading` · `requires` (e.g. `search` — skipped with an honest note where the engine chain
is empty; absent beats broken) · `trials`/`metric` (pass@1 now; `pass^k` armed) ·
`status` (`active` | `armed:<trigger>` | `quarantined:<owner>:<review-by>`).

Two scope guards, both red-team findings: `operate`-kind rows assert **route and answer
expectations only** — workflow mechanics stay in the integration suite (the house's own
test-vs-eval line: "what is measured here is the gate, not the selector"); and armed rows
are pinned for existence + named trigger NOW, while the cannot-register-without-rows
enforcement hook lands with ADR-051 W1 (the doctrine test says so honestly).

## 3 · The suites (three new lanes in the ONE dispatcher)

- **`--suite triage`** — ADR-049's named triage-golden lane, book-driven, no model:
  policy rows through pure functions; plan rows through `draft_plan` against an
  in-transaction task row, planner stubbed, **rolled back per case** (nothing lands in the
  dev DB). Threshold **1.0** — a deterministic miss is a real bug. The shape is
  `golden.py`'s battery + `bot_selection.py`'s in-process-DB precedent (NOT `routing.py`,
  which runs a live model).
- **`--suite range`** — the live tier, ported from `tools/range.sh` into `evals/range.py`
  (the shell stays as porcelain): live rows through the real API, hop-timed from the
  record (`task.created → task.dequeued → task.planned(route) → step.dequeued →
  task.delivered`), per-row budget gates, the route triple printed. `--quick` runs the
  express rows.
- **`--suite answers`** — the graded tier, ADVISORY: judged rows through the API;
  **narration is admissible evidence** (`evidence = delivered summary + record` when a row
  expects no artifact — the gap that made express unscoreable); deterministic asserts
  first (`must`/`never`), then a rubric judge (binary + `unverified` out, never Likert)
  built cross-family via the catalog filter — and **honest about the host**: with one
  enabled family the judge column reports `unverified`, the smoke suite's
  unservable-class precedent. The judge call runs in the CLI (Rule 5) with the
  `operations/bots.choose` null-task metering (Rule 8 — its spend lands on the ledger).

## 4 · The gates and the cadence

| Tier | Where it gates/alarm | Cadence | Cost |
|---|---|---|---|
| policy rows + transforms oracle + book doctrine | **gates** `just check` (unit) | per commit | $0 |
| plan rows | **gates** the integration suite + `--suite triage` | per merge / nightly | $0 |
| live rows | **gates** `just range` budgets | nightly + on demand | cents |
| judged rows | **alarms** via `evals_diff` | nightly, advisory | cents |
| orphan suites (bot-selection · skill-golden · bot-golden) | advisory (selection: drift alarm) | nightly (newly wired) | cents |

`evals.yml` gains the five steps; the graded/live steps no-op gracefully without a funded
key secret in CI and run on-demand locally until one exists. The transforms sweep needs no
cron — it is pure and rides `just check` (better than the planned weekly: free and
earlier).

## 5 · The verdict — what kicks in NOW, what is armed (the owner's question)

**NOW:** the policy gate (free; misses are real bugs) · the plan-tier walk · the live
range rows (cents) · the judged answers suite as an advisory seed (regression-alarmed) ·
router-steering and garble rows (free, at the door) · the abstention branch · quarantine
discipline. **ARMED, each with its named trigger:** judged lanes gating anything → judge
calibrated vs ~100 human labels · the cross-family judge itself → a second judging family
funded · pass^k per row → the first external user · heavy encoding batteries against the
live triager → ADR-049 W2 (a model joins the door) · capability-frontier suites → the
first model/scaffold swap decision · traffic-flywheel growth → real traffic ·
scatter/code/design/bio rows → their lanes landing (ADR-051's law; its W1 registry is the
hook). One line: **evals already kicked in — the book makes them a system; what stays off
is exactly what small n or an uncalibrated judge would turn into theater.**

## Rejected, each with its reason

- **A second harness / DSL** — the dispatcher, thresholds, reports, and the regression
  diff exist; vocabulary is the expensive part of a harness.
- **A row-count target** — padding; the coverage law is the honest size.
- **Likert rubrics** — middle values hide uncertainty; binary + `unverified`.
- **Judge-gated publishes for answers** — uncalibrated judges are the skeptics' whole
  case; the 409 stays a selection-eval gate only.
- **User-simulator multi-turn rows in regression** — simulator variance demands k trials;
  scripted transcripts regress the same property at deterministic cost.
- **Per-PR graded gates** — cheap, fast, statistically significant: pick two; the graded
  tier is nightly and advisory.

## The failure matrix (this ADR's own rows → the book, recursively)

| Failure | What happens | Book row |
|---|---|---|
| A new lane lands without rows | ADR-051 W1's registry refuses registration; until then the doctrine test pins armed-row existence | `armed:*` rows |
| A flaky judged row starts blocking | It cannot — judged never gates; flake → `quarantined:<owner>:<date>`, doctrine test enforces the fields | quarantine clause |
| The book drifts from the ADR matrices | The doctrine test resolves every mapped matrix slug to a live row id | `MATRIX` mapping |
| A transform finds a lighter-route steer | The per-commit oracle fails the build — the steering attack caught free | transforms sweep |
| The judge grades its own family | The catalog filter raises; the column reports `unverified` | answers suite |
| CI has no search engine / no funded key | `requires:` skips with an honest note; graded steps no-op without the secret | `requires` field |

## Workstreams

- **W1 — the book + the free gates** (the battery, transforms + oracle, doctrine test,
  triage-test migration, `--suite triage`). Exit: `just check` green with the book
  enrolled; `--suite triage` 100% with zero model calls.
- **W2 — the live + graded lanes** (`--suite range` port, `--suite answers` +
  narration-evidence + the honest judge, `range.sh` porcelain, `evals.yml` steps). Exit:
  range GREEN live; answers seed run with a report; the orphan suites wired.
- **W3 — the flywheel** (trigger: traffic): the weekly error-analysis ritual, silver
  labels, `provenance=traffic` rows; the calibration set toward the judge-gating trigger.
- **W4 — the arming** (triggers as named in §5): pass^k, the W2-triager adversarial lane,
  frontier suites, the ADR-051 W1 registry hook.
