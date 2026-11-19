# ADR-049 — Triage at the door: the express lane, per-turn lanes, and the answer budgets

**Status:** proposed · 2026-08-27 · **W0 + W1 BUILT AND PROVEN LIVE the same day** (see the
proof block before Workstreams; `just check` + the full 1603-test suite green; the five
cases on the flight recorder — "what's the time?" went 16.4 s → **1.0 s**). Revised same
day, three times, on the owner's record:
(1) the five cases ("the fundamental reason is not the MTTA… the system is not able to
answer the questions and it's going haywire" — the machine's weight must match the ask's
weight); (2) the bucket-landscape pass (how the field classifies big vs small problems);
(3) the surgical audit (unpublished — where the
15 seconds actually goes, and W0). Build authorized by the owner ("get an implementation…
created"); the open questions at the end are recorded as builder's calls, taken and
reversible.
**The owner's ask, recorded:** three things held at once — *no user query ever fails* ·
*time-to-answer very low with the answer still right* · *the right model for the right
job* — across **a RANGE of asks, trivial to book-scale, all working**; heavy lanes (Code,
Design, Bioscience to come) may never cost the simple question its speed.
**Grounding:** an unpublished research note of 2026-08-27 (the
landscape: RouteLLM/FrugalGPT/RouterBench/RouterArena; Bedrock/NVIDIA/Adaptive-RAG
classification mechanics; the GPT-5 router incident; Anthropic's measured cascade; the
mode-switching record) + the surgical audit. Siblings: ADR-050 (how work inside a lane
fans out and returns) · ADR-051 (what a lane IS — the recipe this ADR's door composes).

**Amended 2026-11-15 (ADR-092):** §10's `route_source` gains `lead` (the Lead decided the turn). The Lead replaces the door's lanes, the answerer and the planner at its cutover; a reply keeps the express shape.

## Context — the five cases

| The ask | Today (audited) | Under this ADR |
|---|---|---|
| "What's the time?" | **≈16.4 s p50** — planner on Opus 7 s + clarifier 2 s + frame 1.4 s + publish/harvest 1.9 s + three 1 s queue polls… and no clock to be right with | **express + ambient facts**: no planner, no clarifier, no frame, no publish; first token ≈1.4 s, delivered ≈2 s-class |
| "What's the weather like?" | goes haywire — plans a research task | **express-lookup**: one `web.search` (worker-process, frameless), delivered p50 ≤10 s |
| "Create a simple document" | minutes of machinery for a one-pager | **express-publish**: the prose system-published through the one door; brief md p50 ≤30 s |
| "This complicated document → the full deliverable" | forever, unbounded | heavy stays heavy **on purpose** — sectioned/standard as dialed, now bounded by §7's budgets |
| "A PPT that's so good" vs "just 1–2 slides" | one size regardless | **scale honored strictly per kind**: "1–2 slides" deterministically resolves brief and the contract enforces the count; extended keeps the whole lane |

The audit's four buckets for the trivial case: **55% model calls the ask never needed ·
21% sandbox/publish machinery the answer never used · 9% pure DBOS poll granularity · 15%
everything else including the actual answer (~11%)**. The machinery is superb and the
trivial turn drowns in it — while the answer path already holds four deterministic
short-circuits (`operations/planning.py:250-406`: DM, fleet, sectioned, "the dial IS the
plan"), two live CHEAP_CLASSIFIER routing seats (`router`, `selector`), per-turn dials on
`task.continued`, and a nullable `TaskDeps.frame`. The fast path has all of its rails and
none of its law.

The field's evidence says build exactly that law and nothing fancier. RouterArena's
measured verdict: every router falls short of oracle "primarily because they are
inefficient at recognizing when smaller, cheaper models are sufficient" — the easy case is
the whole game, and it is what deterministic detectors do best. Production routers that
work are lane-first (Claude Code `opusplan`, OpenRouter auto, vLLM semantic router) with
learned machinery inside a lane, never as the door; Bedrock's router emits a quality-gap
*measurement* cut by one operator dial with an anchor when unsure; and the one full-scale
experiment in silent routing — GPT-5's launch week — fixed the constitutional constraint:
route by default, disclose which model ran, let the user pin, never reroute silently.
Plane's own doctrine said it first, one axis over: every fire and non-fire journals a row
wearing its reason. OpenAI's population measurement (Asking 49% · Doing 40% · Expressing
11%) says the express lane serves roughly half of real turns.

**The doctrine sentence:** *the machine's weight matches the ask's weight; no query ends
silent; the budgets are the gauge that proves it.*

## 1 · Triage is a code stage, per turn, before everything

A named stage at the top of `run_task` — before intake on turn 1, before planning on every
turn — that resolves **this turn's lane**. Code first, model last, bounded:

1. **Dials win, absolutely.** An explicit `mode=fleet`, `direct_bot`, user-sourced
   `deliverable`, `scale ≥ extended`, or `clarify=always` (turn 1) IS the routing
   decision — the four existing short-circuits become triage's first clause. Operator
   intent outranks every heuristic (the ADR-024 pin law, one level up).
2. **Deterministic detectors next** (`ai/policy/triage.py`, pure functions over the turn's
   text + dials + thread state; p95 **<5 ms**). Hard negatives (never express): any input
   file this turn (files need frames) · code fences · attached bots/skills/connectors ·
   `origin_routine_id` set (standing work keeps the standard record — builder's call, see
   Open Questions) · a thread whose previous turn escalated (§4 hysteresis). Hard
   positives: acknowledgements and social closers · short single-question shapes (including
   over the thread's own material) · arithmetic/conversions · **lookup shapes** (weather /
   price / score / news / "current X" / a bare URL question — the express-lookup tier, §3)
   · the explicit intent phrases ("quick:", "just tell me") — the GPT-5 "think hard"
   convention, both directions. **Explicit size tokens are extracted here by rule**
   ("2 slides", "500 words", "brief", "comprehensive") — the size law, §2b.
3. **The classifier seat, only when the middle is genuinely ambiguous**: `triager` — a new
   light agent, CHEAP_CLASSIFIER, tools=(), `plannable=False`, the `router`/`selector`
   twin. **It emits measurements, never a lane** (the Bedrock/NVIDIA rule — models out of
   routing, made stricter): a typed verdict `{task_type, size_class, retrieval_need,
   why}` — `task_type` from the InstructGPT-lineage vocabulary (open_qa · closed_qa ·
   chat · rewrite · summarize · extract · classify · generate · code · brainstorm ·
   other), `size_class ∈ brief|standard|extended` (never an exact count — LIFEBench's
   lesson), `retrieval_need ∈ none|lookup|research` (Adaptive-RAG's A/B/C). **Code cuts
   the scores** against conf thresholds in `ai/policy/triage.py`; under-threshold and
   `other` anchor to **standard** — abstention is a first-class outcome and the anchor is
   never express. Fed the goal head + a dial summary, never the whole thread. Hard wall
   **2.5 s** (p95 <1.5 s): timeout, error, refusal ⇒ standard. Never more than one model
   call to route a turn; the trivially-clear majority pays zero.
4. **Triage never asks questions.** Clarification stays the clarifier's job, governed by
   the `clarify` dial, turn 1 only, unchanged. Triage routes; it does not interview.
5. **The security clause.** Triage is an unauthenticated control plane and its input is
   attacker-influenceable data (the COLM-2025 confounder-gadget class: short prepended
   token sequences reliably steer routers). Therefore: the turn's text reaches detectors
   and `triager` clamped, as data; triage selects among lanes the dials allow and **can
   never widen budgets, grants, or autonomy** — the worst a steered triage buys is a
   politer lane, never spend beyond the Bursar's caps, never a tool the lane doesn't
   grant.

Stage order on turn 1: triage → (express ⇒ the clarifier is skipped by construction — a
turn adjudged trivial has nothing material to ask; `clarify=always` already forced
standard in clause 1) → intake as today → plan. On turn ≥2: triage → plan, reading this
turn's dials from `task.continued` exactly as re-resolution does now.

*Amended 2026-11-12:* under `clarify=auto` the intake round is also skipped, in code
(`planning_turns.nothing_to_clarify`, its reason in the log), for an imperative at a
named site (the computer asks through `ask_user` and its approval gates when it matters), a
transaction (the agent doing it asks for what it lacks when it gets there) and a short question
(it states its assumptions in the answer). `clarify=always` still interviews every one of them.

## 2 · The lane taxonomy and the resolution law

The lanes, as of this ADR: **express** (new, §3 — with its lookup and publish tiers) ·
**standard** · **fleet** (dial) · **direct** (dial) · **sectioned** (dial or deterministic
size) — with **scatter** joining via ADR-050 and **code** via the blueprint (ADR-035–044),
each behind the same door, each an ADR-051 recipe entry. The resolution law generalizes
`kinds.resolve_turn`'s dial > language > stamp:

> **dial > deterministic > classifier > planner-anchor** — and the source is stamped.

Nothing a later clause decides may override an earlier one. The planner remains the final
authority for everything standard — triage decides *whether the planner runs*, never what
it plans (ADR-028 §5 untouched: the `triager` proposes measurements the code disposes of).

### 2b · The size law

Nobody in the studied field infers exact deliverable size from prose, because models
cannot self-govern length (LIFEBench: adherence "deteriorates sharply beyond a threshold");
Gamma externalizes every size decision as a parameter with a default; Beautiful.ai routes
size through an editable outline. Plane's law, matching:

1. **Explicit counts, extracted by rule, outrank everything** ("2 slides" → brief deck,
   count on the contract; "60 pages" → the long-form door, as today).
2. **Absent a count, a size-CLASS** (brief | standard | extended) — from the detectors or
   the `triager`'s head — resolves through the existing ADR-029 bucket machinery.
3. **Big shapes confirm at the gate that already exists**: the expansion-time cost gate
   (ADR-025 §6) is the outline checkpoint the field converged on. Nothing new.
4. **The small end has teeth**: a brief deck/workbook is ONE minimal step — no outline
   machinery, no QA, no figure pregeneration — and the resolved count rides the step's
   contract acceptance, so **over-production against the resolved size is a verify
   finding**. "Sometimes I need one or two slides" gets one or two slides, fast.

## 3 · The express lane — a plan shape, not a second execution path

For the turn that is a breath, an answer that is one. The routines constitution applies in
the other direction: no second execution path. Express is **a code-made one-step plan**
(the DM-lane precedent, `planning.py:256-288`) whose one step is frameless:

- **The `answerer`**: a new step agent — FAST_EXECUTOR, frameless (`TaskDeps.frame` is
  already nullable; `deps.connectors`/`bots` already refuse the frameless path),
  `can_ask_user=False` (a quick answer that interviews you is not quick — escalate
  instead), `verify=False`, `qa=False`. It receives the thread transcript, the turn, and
  **the ambient facts** — current datetime, timezone, locale, injected as VALUES in the
  brief, not tools — so "what's the time?" is instant *and correct*. Its **narration IS
  the answer**, streaming on the message channel (first-delta law unchanged); the delivery
  summary is that narration; **its typed output also carries the turn's title**, so the
  namer's call is folded away on express (turn 1's title costs zero extra calls — the
  audit's namer lever, taken).
- **Three tiers, one agent, grants as the difference:**
  - **express** — `tools=()`: chat, arithmetic, thread questions, ambient facts.
  - **express-lookup** — `web.search` + `web.fetch` granted: both are worker-process
    warden tools (ADR-046/047) that bypass frames BY DESIGN, so the lane stays frameless.
    "What's the weather like?" is one search and one breath, not a research plan. The
    detectors route lookup shapes here directly; the warden, the query-free journal law,
    and the search ledger apply unchanged.
  - **express-publish** — a brief/auto-sized TEXT deliverable rides the express spine and
    the prose is **system-published through the one door** (the ADR-019
    `publishes_output` lane, minus the frame). The careful piece, named honestly: harvest
    reads `frame.manifest()` today, so the one-door walk needs a host-side variant —
    content-addressing, provenance, and repairs unchanged; if that variant fights, the
    recorded fallback is one frame with everything else still cut (~+2 s), and the
    frameless door becomes its own small follow-up.
- **The escape hatch is typed, upward, once.** The answerer's output is
  `{answer, title} | {escalate: reason}` — the AutoMix insight (the small model verifies
  *itself*) without a trained scorer. An escalate replans the same turn as standard (a
  plan edit on the record, the reason in `lane_why`); one hop maximum; standard never
  demotes to express mid-turn — the next turn re-triages.
- **What express keeps**: the ledger (every call meters) · the flight recorder
  (`task.planned` with the express shape, `step.state`, narration, `task.delivered`) ·
  the Bursar (a task at its cap parks like any) · the Retry button. **What it skips**: the
  clarifier (by construction) · the planner · the frame · publish/harvest (except the
  publish tier's door walk) · verification (nothing published ⇒ nothing to verify —
  consistent with today: narrations are never verified; the publish tier's artifact gets
  the normal ADR-019 receipt) · the archivist (builder's call: a "thanks!" is not a
  memory) · the namer as a separate call (folded, above).
- `deliver` gains the tolerance for a zero-artifact turn (W1) — and the notify=findings
  vocabulary stays honest by construction (no deliverable artifact ⇒ a quiet run).

## 4 · Per-turn resolution, stickiness, and the cache pin

The field's stickiness rule (Claude Code persists safe modes, Plan/Bypass decay per
conversation; GPT-5 routes per message; Gemini answers follow-ups from gathered context
first) maps onto machinery Plane already has:

- **Safe context is thread-DNA; weight is per-turn.** `mode` and `space_id` stay
  inherited, never chosen (`FollowUpIn`'s law). Lane weight resolves per turn from that
  turn's message and dials, so a trivial follow-up inside a heavy thread pays a trivial
  price *with the heavy thread's context* — the transcript is the already-gathered
  research. Deterministic-trivial turns may express in any thread; anything more honors
  the thread's mode. Express never sticks — it decays every turn by construction.
- **Hysteresis**: a thread whose last turn escalated floors at standard for the next turn
  unless the detector match is hard-positive. Route flip-flop was the GPT-5 users' top
  named complaint; one turn of memory kills the oscillation.
- **The cache pin**: within a thread, when the chain re-rank puts two candidates within a
  price whisker, the previous turn's answering model wins — a mid-thread model switch
  forfeits the cached transcript prefix (hits cost 0.1×; ADR-018 §3 built the transcript
  append-only for exactly this; Copilot routes "along natural cache boundaries" for the
  same reason). Operations feeds the prior ref (`UsageEvent.for_step` — "the one place
  that knows for certain"); the engine stays persistence-free.

## 5 · The ladder that climbs — never-fail, unified and finished

The chain exists at every altitude; this section names the whole ladder once and adds the
one missing rung. From the bottom:

1. **Transport**: retries on the SAME model honoring Retry-After, ≤3 (ADR-024) — only then
   may the chain change authors.
2. **The chain**: `FallbackModel` over ≤4 candidates per class, provider-throttled, winner
   bills (exists).
3. **The attempt ladder — now climbs.** Today attempt 2 re-buys the identical chain
   (ADR-019's addendum recorded thirteen identical truncations across three rungs). New:
   **attempt ≥2 re-resolves with the max-quality ordering and the prior attempt's
   answering model demoted to tail** — retry-with-upgrade, the one escalation primitive
   with published measurements (Anthropic: re-running only the failures one tier up held
   the pass rate at ~half the cost, counting the failed cheap attempts). Operations passes
   the attempt number and the prior ref into `build()`; targeted-retry briefs, gotcha
   recording, and `MAX_ATTEMPTS=3` are untouched.
4. **Express's own ladder**: attempt 1 on the fast rung under an express wall
   (`COMPUTER_WORKER_EXPRESS_TIMEOUT_S`, default 45 s — the 180 s class ceiling is another
   lane's patience) → attempt 2 climbed, same wall → **escalate to a standard replan, not
   to task failure**: the answer arrives late rather than never, the record shows the hop.
5. **Escalation with diagnosis** (exists) → **honest failure + Retry** (exists). The floor
   stands: no turn ends silent, hung, or unactionable.
6. **Named deferrals at this altitude**: per-provider circuit-breaker cooldowns (LiteLLM's
   shape — transport retry + chain + throttle absorb the common case today); bounded TTFT
   hedging for express p99 (fire the second rung only past the observed p95 first token —
   The Tail at Scale's ~5%-overhead shape) once live percentiles exist.

## 6 · The machinery floor — W0, the surgeon's cuts

The audit's arithmetic: without these, the express budgets are unreachable — three
independent 1.0 s sleep-then-poll loops alone cost ~1.5 s p50. W0 ships first and speeds
**every** lane:

1. `polling_interval_sec=0.05` on `task_queue` and `step_queue` (`jobs/queues.py`) and on
   the two workflow-result joins (`run_task.py:175`, `effects.py:387`) — ~1.4 s off every
   task's p50, ~2.9 s off p95, for three keyword arguments.
2. **Two new durable events** — `task.dequeued` (first line of `run_task`) and
   `step.dequeued` (first line of `execute_step`) — so hop 1, hop 2, and the join are
   measurable from the record forever (`task.created→task.dequeued` IS hop 1, etc.).
   Additive `_Durable` variants; the API-restart-before-worker deploy note applies.
3. Loop hygiene: `enqueue_async` in `ClientDispatcher` (the API's enqueue currently blocks
   the event loop on a sync DBOSClient); `@lru_cache` the docker client + `to_thread`
   `ensure_ready()`; async `frame.denials()` (a blocking httpx call, 5 s worst, on every
   step today).
4. Process-cached, version-stamped `catalog()`/`order_for()` — four DB round-trips per
   step attempt for data that changes on `computer models add`; the cache invalidates on
   the admin mutations.
5. The namer leaves the delivery path (detached on standard; folded into the answerer on
   express, §3).
6. `tools/smoke.sh` gains its first timing assertions — the audit found every §15 temporal
   budget asserted nowhere.

Floor after W0, express, model stripped: **≈0.5 s of machinery** against ~1.6 s of actual
answer — the remaining lever is the model, which is the part the user asked for.

## 7 · The answer budgets — MTTA becomes §15 rows, with gates

The owner's MTTA lands as budgets beside the stage budgets, per lane, p50/p95 — and this
time with gates: the in-process rows join `tests/perf/`, the end-to-end rows join `just
range` (W1) on the nightly cadence. The felt floor is unchanged and now named: first event
<1 s · first delta after step start <2 s · no-dead-air ≤10 s. External anchor: Bedrock's
entire routing layer runs ~85 ms P90; our code path budget is <5 ms because the classifier
only runs on the ambiguous slice.

| Row | Budget |
|---|---|
| Triage, code path | **<5 ms** p95 |
| Triage, classifier (ambiguous slice only) | **<1.5 s** p95 · hard wall **2.5 s** ⇒ standard |
| Queue hops (task, step) + result join, each | **<100 ms** p95 (W0; measured by the dequeued events) |
| Express: submit → first answer token | **≤2.5 s** p50 · **≤5 s** p95 |
| Express: submit → delivered | **≤10 s** p50 · **≤20 s** p95 (post-W0 arithmetic: ≈2.2 s p50 — the budget keeps honest headroom) |
| Express-lookup: submit → delivered | **≤10 s** p50 · **≤20 s** p95 |
| Express-publish (brief document, md) | **≤30 s** p50 · **≤60 s** p95 (docx adds only its rendition) |
| Brief deck (1–2 slides, one minimal step) | **≤3 min** p50 (measured live: ~3 min — the ADR-034 QA gate is most of the clock and it is the deck's quality law; the 90 s aspiration is named for the QA-tuning follow-up) |
| Express: answerer wall clock | **45 s** per attempt, then the ladder |
| Standard: submit → `task.planned` | **<5 s** (existing row, unchanged) |
| Standard: submit → first substantive delta | **≤12 s** p95 |
| Standard, single-step undialed: submit → delivered | **≤60 s** p50 · **≤120 s** p95 |
| The dialed path: planner skip | instant (existing) |
| The escalation hop (express → standard) | adds one replan; the standard rows then apply; the hop journaled |
| Long-form / consensus / standing lanes | their existing §15 rows, untouched |

## 8 · The lane hones — priming as law

Once a turn is in a lane, the lane gathers what it needs — lazily, with receipts (the
Devin-Knowledge / CLAUDE.md-rules pattern, already Plane's shape): **express** mounts the
transcript + ambient facts, nothing else · **standard** primes as today (context pack,
skill router's one pick, roster + compiled bots, space instructions — `context.assembled`
is the Accessed-Knowledge receipt, already durable) · **sectioned** primes the bible into
every seat · **scatter** primes per-seat fenced briefs (ADR-050) · **code** (when the
blueprint lands) is the deepest priming and the reason this is law: rules files, the repo
map, the warm workspace keyed by lockfile hash — **lane state lives on rows**, so
re-entering a lane is warm by construction. Honing = state on rows + receipts on the
record, never resident memory in a process.

## 9 · What the user holds

The levers stay outcome-shaped and they win: `quality`, `scale`, `deliverable`, `fleet`
(the model picker, inverted — the honest one), `direct`, `clarify`, budget. Intent phrases
steer without dials in both directions ("quick:" / "think hard"). Expensive lanes keep
their approval artifacts (the expansion cost gate; `hold`) — suggest-then-confirm, the
Cursor/Devin shape. Express never sticks. And nothing routes silently: the lane and its
reason on `task.planned`, the answering model on `usage.recorded`, both replayable
forever.

## 10 · The routed record — wear the reason, grade the door

- **`task.planned` gains three fields**: `route`, `route_source ∈ dial | detector |
  classifier | planner`, `route_why` (≤200) — *route*, not *lane*, because the event
  envelope's `lane` is the stream discriminator (record|live), found at build time. The
  `StepSpec.why` precedent — zero new wire event kinds beyond W0's two dequeued marks;
  express's code-made plan emits `task.planned` like every lane, so every turn of every
  task wears its route. Together with `usage.recorded`'s model, that is the full
  disclosure the GPT-5 reversal converged on. (The web's quiet route chip is the named
  W5.)
- **The `triager` bills like everyone**: a normal task-scoped usage row.
- **The golden flywheel** (label by outcome, not intuition — Adaptive-RAG's method,
  Not Diamond's train-on-your-evals): `triage-golden` seeds with the five cases + silver
  labels derived from replayed traffic (which lane would have sufficed, judged by the
  outcome), graded nightly beside `bot-selection`. Watched metrics: **express recall on
  trivial turns** (the field's measured defect), **big-job recall** (the rare class —
  with ~80% of traffic in a few buckets, rare-class recall matters more than accuracy),
  and **the live hatch rate** (a rising escape rate is the drift alarm). **Named v2 with
  its trigger**: once the journal holds enough outcome-labeled turns, train a small
  multi-head encoder (the NVIDIA DeBERTa / vLLM ModernBERT pattern — task × complexity ×
  size heads, ~10 ms) to replace the LLM `triager` on the hot path; the LLM then labels
  offline only.

## Rejected (each with its reason; the research doc holds the receipts)

- **A trained preference router as the door** — training data Plane lacks, transfer
  penalty per new model, opaque; the measured field still loses to easy-case recognition.
  The named revisit is §10's encoder, *behind* the deterministic door, trained on our own
  outcomes.
- **Always-on parallel hedging** — ~110% of strong-only cost; the bounded TTFT hedge is
  the named deferral.
- **FrugalGPT scorer cascades** — a per-task trained scorer that becomes the correctness
  oracle; the answerer's typed self-escalation is the untrained substitute.
- **An LLM judge at the door** — 50–100+ ms and dollars taxed on the trivial majority;
  judges stay on the receipt (ADR-019).
- **Inferring exact deliverable size from prose** — LIFEBench; the size law (§2b) extracts
  counts by rule, classifies a class, confirms big shapes at the gate.
- **Silent rerouting of any kind** — both 2025 trust incidents ended in public reversals;
  constitutionally excluded by §10.
- **A raw model picker** — disclosure-not-selection; fleet is the comparison surface; the
  catalog stays operator surface.
- **A separate planning destination** — Copilot Workspace was sunset into the agent within
  a year; Plane's plans stay in-thread.
- **Express as a `TaskMode`** — mode is thread-DNA; express must decay per turn, so it is
  a plan shape, never a mode.

## The failure matrix (contract rows → tests and drills)

| Failure | What happens | Why |
|---|---|---|
| `triager` times out / errors / refuses | Standard lane, `lane_source=classifier`, `lane_why` names the fallback | The planner's judgment is the default; a broken router degrades to today, never below |
| Express misroute — the question was hard | Typed escalate → standard replan, same turn, one hop, journaled | §3's hatch; the hatch rate is the drift alarm |
| Standard misroute — the question was trivial | The cost is politeness, not correctness; `triage-golden` watches express recall | Over-escalation is the field's measured norm — watched, not feared |
| Express chain dead (fast rung down) | Attempt 2 climbs the rung; then a standard replan; never a silent hang | §5's ladder; 45 s wall per attempt |
| Every provider down | The chain spans families where the catalog does; honest failure + diagnosis + Retry; breaker cooldowns deferred, named | The DBOS spine holds the turn durable throughout |
| Thread flip-flops express↔standard | Hysteresis floors post-escalation turns at standard | §4 |
| A confounder-gadget goal aimed at the router | Worst case: a politer lane. Never wider budget, grants, or autonomy; detectors clamp what the `triager` reads | §1.5 — the control-plane clause |
| A short prompt hiding a big job ("write me a book") | The size law: "book" is a size-class signal → the long-form door, cost gate confirms | §2b; length was never the signal |
| A huge prompt hiding a trivial job (pasted logs + one question) | Extraction/QA task shape → express; the thread carries the pasted material | §1.2; task type, not length |
| Routine fire lands on triage | Never express (builder's call) — standing work keeps the standard record | §1.2; Open Questions |
| `clarify=always` on turn 1 | Never express — the dial wins, the interview happens | §1.1 |
| Budget nearly exhausted | Express parks on the budget gate like any lane | §3 |
| Deploy mid-express-turn | A seconds-long step workflow on the existing queues; DBOS recovery replays | Nothing new to orphan |

## Consequences

- **Zero new dependencies. Zero migrations.** Detectors are `ai/policy/triage.py` +
  `schemas` values; the `answerer` and `triager` are two agent files + registry imports;
  the lane fields ride the `task.planned` payload; W0's `task.dequeued`/`step.dequeued`
  are additive `_Durable` variants (API restart before worker); conf gains
  `COMPUTER_WORKER_TRIAGE_*` / `…_EXPRESS_*`.
- **CLAUDE.md gains** (on acceptance): *"Triage (ADR-049): every turn resolves its lane in
  code before anything runs — dial > deterministic > classifier > planner-anchor, the
  decision on `task.planned` wearing its source and reason; the classifier emits
  measurements, code cuts, abstention anchors standard; express is a frameless one-step
  plan shape (ambient facts; lookup via the worker-process web tools; narration is the
  answer; escalates upward once; never sticks; picks machinery only — never budgets or
  grants); attempt ≥2 climbs the rung; the answer budgets are §15 rows with gates."*
- **HANDOFF**: §15 gains §7's rows; §1's vocabulary gains Express and Triage lines; §8.1's
  state machine is untouched (express rides `planning → running → delivered`).

## The proof (2026-08-27, live on the flight recorder — W0 + W1's exit)

The five cases, run on the real stack the day this ADR was written (dev box; the text lane
was served by `xai:grok-4.20-non-reasoning` — added as **pure data** mid-proof via
`computer models add … --enable` because the dev Anthropic key had run dry and the Gemini
tier was exhausted: §9's comfort guarantee, demonstrated during its own drill):

| Case | Before (audited) | After (measured, `just range` from the record) |
|---|---|---|
| "What's the time?" | ~16.4 s p50 | **1.0 s** delivered — hop 1 60 ms · triage+mint 40 ms · hop 2 80 ms · answer 0.8 s; **and correct**: "It's 5:27 AM in Los Angeles" from the ambient facts + the user's timezone |
| "What's the weather like…?" | a research plan | **5.5 s** delivered, sourced — `web.search` (searxng) + three anchored fetches, every hop journaled, route=`express · detector · lookup ask` |
| "a one-page note…" | minutes | **13.8 s** through the full standard path (planner 4.8 s + step incl. frame/publish/harvest) — under the express-publish target before that tier exists |
| standard single-step | — | **12.5 s** (the control row: heavy machinery, W0-fast) |
| "just 2 slides…" | one size regardless | `scale=brief · target_pages=2 · deliverable=deck` resolved from the goal's own words; a real 2-slide pptx in ~3 min through the presenter + ADR-034 QA gate |
| A recovered orphan with every provider dead | — | attempts → climb → escalation → honest failure with the diagnosis — the never-fail ladder exercised by accident, exactly to spec |

`RANGE: GREEN` · `just check` exit 0 · full suite **1603 passed, 0 failed (2:21)** · the
namer runs post-delivery on express (title +0.7 s after the answer, off the path) · the
express turn's whole ledger: `task.created → task.dequeued (60 ms) → task.planned
(route=express·detector) → step.dequeued (80 ms) → step.prompt → usage.recorded →
step.narration → task.delivered`, zero artifacts, zero frames.

## Workstreams

- **W0 — the surgeon's cuts** (§6). **LANDED 2026-08-27**: queue/join cadence conf
  (`queue_poll_s=0.05`), `task.dequeued`/`step.dequeued` on the record (+ the web façade,
  drift-gated), `enqueue` async end to end (Protocol + both dispatchers + sweeps + call
  sites), docker client cached + `ensure_ready` off-loop + `denials()` async, the
  catalog/routing TTL cache with writer invalidation, the smoke's first timing assertion.
  Measured: hop 1 + hop 2 + join each **<100 ms** (were ~500 ms mean each).
- **W1 — detectors + the express spine. LANDED 2026-08-27**: `ai/policy/triage.py` (the
  deterministic door; 20 unit cases incl. the five and the boundary traps); the `answerer`
  (ambient facts; chat + lookup tiers; frameless); `_attempt_express` (the deliberate
  sibling of `_attempt` — no frame choreography, same ladder); zero-artifact delivery;
  `task.planned` route fields on every branch; the namer post-delivery on express; the
  archivist skipped on express; conf + `.env.example`; `just range` + the smoke timing;
  4 integration tests (mint, frameless run end-to-end, lookup tier, Doing-reaches-planner).
  **Express-publish shipped as its recorded fallback**: the brief-document case measures
  13.8 s through the standard path, already under the 30 s budget — the frameless one-door
  walk stays the named follow-up, taken only if the budget ever tightens past what the
  standard path holds. **The exit proof above.**
- **W2 — the `triager` + goldens.** The agent + prompt (silence-biased: when unsure,
  standard); the 2.5 s wall + fallback; the golden lane in the eval harness. Exit: the
  golden set routes ≥ the bar; a forced-timeout drill lands every turn standard with the
  fallback journaled.
- **W3 — the ladder climbs + the hatch.** Attempt ≥2 re-resolve (max ordering +
  prior-ref demotion, operations-fed); the answerer's typed escalate → replan; hysteresis.
  Exit: a poisoned-fast-model drill shows attempt 2 on the climbed rung with both models
  on the ledger; a live express escalation replans standard with the hop on the record.
- **W4 — the cache pin + measured routing.** Prior-ref preference within the whisker;
  express percentiles on the Logfire dashboard queries; `duration_ms` on usage stays the
  ADR-024 named deferral.
- **W5 — the web chrome.** The quiet lane chip + the escalation hop line, per DESIGN.md +
  a sibling screen, verified in a real browser.

## Open questions — recorded as builder's calls, taken 2026-08-27, each reversible

1. **Nouns**: `express` / `answerer` / `triager` (the clarifier/namer school); "triage"
   stays a plain word, no proper noun.
2. **The express rung**: FAST_EXECUTOR (haiku-class serves it today; CHEAP_CLASSIFIER
   would be noticeably dumber in the one lane whose product is the first impression).
3. **Routine fires**: excluded from express v1 — standing work keeps the standard record
   and the notify=findings vocabulary stays coherent.
4. **The archivist on express turns**: skipped — corrections that matter arrive on
   substantive turns.
5. **The §7 numbers**: set from the audit's arithmetic with headroom; every row is a gate,
   tighten on evidence.

## Amendment 2026-11-15 - the cutover (ADR-092)

The deterministic door no longer routes a new turn and `PRISMOID_WORKER_TURN_ANCHOR` no longer reaches one: the
Lead decides every turn no recipe answers. The express rung stays: the Lead runs on it.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The talk door (`ai/policy/triage`), the answerer and the express rules (the offline retry after a wall stop, `NO_WEB`) are deleted; the express shape stays as the Lead's one frameless step, and `PRISMOID_WORKER_EXPRESS_TIMEOUT_S` stays as the Lead's wall. A stored answerer step runs as the Lead.
