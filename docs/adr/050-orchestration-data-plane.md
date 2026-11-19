# ADR-050 — The orchestration data plane: the plan is the protocol

**Status:** accepted · 2026-08-27 · W2 (the scatter shape) BUILT and proven live the same
day — see the amended W2 entry under Workstreams for what the build settled and what the
first live runs caught. W1/W3/W4 remain open.
**Grounding:** the unpublished research note behind ADR-049 (same
day — three research agents against primary sources + a full seam map of this repo); the
Computer X direction doc (2026-08-22) and the unpublished software-factory study of 2026-08-20,
§8.6 (the `delegate` blueprint this ADR generalizes). Sibling: ADR-049 (triage and the
express lane) decides *which* lane a turn enters; this ADR decides how work inside a lane
fans out and returns.

## Context

The owner's question, verbatim in spirit: *when a master agent orchestrates a subagent, how
does data start being exchanged? Research agents spinning up a bunch of subagents — how does
every single piece work, and how is it robust?*

The answer this ADR gives has two halves. First, **the exchange already has a constitution —
it has just never been written down as one.** Plane's orchestrator is not an agent talking to
agents; it is the plan: One proposes a DAG, code validates and dispatches it, each step runs
in isolation with a typed brief down and published artifacts up, and the join law decides
what "all the results are in" means. Every piece of that exists at a named file:line. Second,
**two pieces are genuinely missing**: a fan-out whose width is decided mid-flight (the
research-swarm shape — today only the long-form lane can expand a plan from inside a task),
and a way for one step to quarantine bulk context without minting a sibling step (the
`delegate` blueprint, ADR-035–044 §8.6, pulled forward as general machinery).

The landscape pass behind this is unusually unanimous. Four systems that never met —
Anthropic's research system (subagents "store their work in external systems, then pass
lightweight references back"), MetaGPT (structured artifacts as contracts between roles),
Google's A2A (Messages split from Artifacts at the protocol level), CrewAI (typed
`TaskOutput`) — independently arrived at the same rule: **conversation and deliverables
travel on different channels, and only deliverables are the contract.** That is a
publish → manifest → harvest data plane with summaries riding the control channel — the
architecture this repo has run since M1. The field converged on where Plane already stands;
this ADR's job is to make the convergence law, and to add the two missing shapes without
breaking it.

## 1 · The exchange, named — what crosses each boundary (the constitution)

One section that turns the implicit contract into doctrine. Each law names its existing
enforcement; nothing in this section is new code.

**DOWN — the brief.** A step agent receives, and only receives:

- **The contract**: `Contract{goal, acceptance[], inputs[], output, allow_domains, budget,
  notes}` on `StepSpec` — Anthropic's finding that a good task brief needs "an objective, an
  output format, guidance on tools and sources, and clear task boundaries" maps onto these
  fields one-to-one, and their failure mode ("vague briefs produced duplicated searches") is
  the reason `validate()` refuses uncheckable acceptance.
- **Materials by reference, never by value**: upstream artifacts mount read-only at
  `/task/inputs/` (`operations/execution/mounts.py`); the manifest is the door
  (`execution/__init__.py:1166` — "a file that arrived in /artifacts without going through
  publish has no entry").
- **The conversation as instruction, never evidence**: `thread.md` (ADR-018, ~16k budget,
  rendered to the planning boundary so retries read byte-identical history) plus
  `answers.json` — the context pack's fenced-lane law applies to everything here.
- **The grants**: tools via the warden, skills via pins, connectors/bots only on the step
  path (`deps.py:105-117` — light agents can never mount one, by construction).
- **The budget slice**: `TaskDeps.budget`; the Bursar re-checks at every frontier boundary.

**UP — the return.** A step hands back, and only hands back:

- **Artifacts through the one door** (`frame.publish` → manifest → harvest, repairs on
  provenance) — the blackboard. Working artifacts (`artifact_role="working"`) cross steps
  without reaching the shelf (ADR-025 §5).
- **Typed values where the shape is the contract**: `OutlineResult`, `Collection`,
  `RepoSurvey` (blueprint) — schema-validated at harvest, the field's "structured outputs as
  the inter-agent ABI" (OpenAI measured 100% schema conformance enforced vs <40% prompted;
  Plane's equivalent enforcement is pydantic-ai typed outputs + the harvest validators).
- **The narration as the summary**: a ≤one-screen account for the record, exactly Anthropic's
  "condensed summaries — often 1,000–2,000 tokens" riding the control channel while the bulk
  stays external.
- **The receipts**: `step.prompt` (what it was told), `tool.called` (what it did),
  `usage.recorded` (what it cost) — the trace tree the OTel GenAI world is still
  standardizing is already Plane's flight recorder.

**SIDEWAYS — nothing.** Siblings exchange only through published artifacts of settled steps,
under the join law (ADR-028: `all | quorum | consensus | primary_input`, pure and total).
There is no shared mutable state, no message pool, no group chat. Where the field uses
reducers to survive concurrent writes (LangGraph), Plane removes the concurrency instead:
two steps never write one artifact — the lineage boundary cut (ADR-018 §5) exists precisely
so two fleet seats publishing one name in one turn cannot read each other as "prior".

**The doctrine sentence this section adds:** *briefs go down, artifacts come back, siblings
never speak — everything else is a receipt.*

## 2 · The scatter shape — fan-out whose width is decided mid-flight

The long-form lane already owns the only mid-flight expansion in the product: the architect
publishes a typed `OutlineResult`, and `ai/policy/longform.expand` — PURE CODE — inserts the
section wave via `PlanEdit(kind=EXPAND)` (ADR-025 §2). That is the exact shape Anthropic's
orchestrator-worker system uses for research ("the lead agent develops a strategy and spawns
subagents to explore different aspects simultaneously") — except theirs is a model deciding
in its own context, and Plane's is a model proposing into code that disposes (ADR-028 §5,
untouched). Generalize it:

1. **The map step.** A research-shaped task whose triage class says *deep* (ADR-049) plans a
   first step whose contract demands a typed **`ScatterSpec`**: subtopics, each with its own
   brief — `goal`, `expected` (what a finding looks like), `not_here` (the scope fence,
   ADR-028's outline vocabulary reused verbatim — what this seat must NOT cover because a
   sibling owns it), and a per-seat budget share. The map step is the researcher itself with
   a typed output lane (the organizer precedent) — no new agent.
2. **`ai/policy/swarm.expand` — pure code** validates the spec (disjoint fences, width,
   budget arithmetic) and inserts ≤W `researcher` steps (born READY, parallel by
   construction — no shared edges, one frontier wave, dispatched as concurrent child
   workflows exactly as fleet seats are, `jobs/effects.py:357`) plus one **synthesis** step
   (`writer`, `join="quorum"`, `tolerates_missing_inputs=True`) that mounts whatever
   findings survived. An unexpandable spec fails its own attempt and rides the ladder — no
   silent one-seat fallback, the ADR-025 rule.
3. **Effort scaling is data, not vibes.** Anthropic embeds the scaling rule in a prompt
   ("simple fact-finding: 1 agent, 3–10 tool calls; direct comparisons: 2–4; complex
   research: 10+ with divided responsibilities") and calls overinvestment in simple queries
   "a common failure mode of early versions." Plane makes the same table config:
   `SwarmSettings` (`COMPUTER_WORKER_SWARM_*`): `max_width` (default 8, hard-clamped by
   `MAX_STEPS`), `min_width_for_scatter` (default 3 — below it, one researcher step was
   always the right plan and the map step is refused at validate), per-seat budget floor.
   The planner's brief teaches the classes; the clamp is code.
4. **Dedup at dispatch, not at merge.** The field's measured lesson (Anthropic: task
   boundaries are the dedup mechanism; Cognition: merging unfenced parallel work is where
   conflicts live). The fences are validated disjoint before expansion; the synthesis step's
   contract orders it to report *disagreement between findings as a finding* — the fleet
   chair's honesty rule, one lane over.
5. **The money.** Anthropic's number is the warning: multi-agent burns ~15× chat tokens, so
   "the value of the task must pay for the performance." The scatter shape is therefore
   entered only from triage class *deep* or an explicit dial, the expansion-time cost gate
   rides the long-form precedent (a budget-kind gate above the confirm threshold, skipped
   when the task budget already covers the high estimate), and the Bursar's
   `bursar.project` projects the whole wave before it dispatches.

> **Amended 2026-08-27 (WANDR, arXiv:2608.14747 — Perplexity's wide-and-deep research
> benchmark, read on the owner's pointer):** two W2 design inputs adopted from its
> measurement design, zero architecture change. (1) **The seat's typed return is the
> citation-backed record atom** — `{entity, url, excerpts[], claim}` — so scatter output
> is independently verifiable by construction (their judge re-fetches every cited page;
> our ADR-019 receipts already grade quotes verbatim — same religion, now the same shape),
> and a WANDR slice grades our lane for free. (2) **The map's coverage contract may speak
> the qualification-key grammar** (`key(r) → … → url(r)`, per-parent minimum counts) —
> WANDR's decomposition shows the field loses 47–89% of records in DISCOVERY, before
> enrichment ever runs, which is precisely the stage the map step + fences spend their
> machinery on; the count grammar makes that stage's obligation explicit and gradeable.
> A 10–20-task WANDR slice (github.com/perplexityai/wandr) is W2's external exit
> yardstick, beside the book's own rows (`research-frontier-wandr-slice`, armed).

Write-heavy work is deliberately NOT scattered. The field split cleanly — parallelize reads,
serialize writes (Anthropic concedes coding "involves fewer truly parallelizable tasks";
Cognition's Flappy Bird is what unfenced parallel writers produce) — and Plane's lanes
already embody the split: research scatters, sections write in parallel only under an
outline's fences + the harmonize wave, and the code lane's blueprint fans out only on
"disjoint owned paths named by a reviewed TechSpec, with an `integrate` step as the only
writer" (§8.1.6). This ADR changes nothing there.

## 3 · `delegate` — the in-step quarantine, pulled forward from the blueprint

The blueprint (§8.6) gives the code lane a `delegate(role, brief, tools_subset?)` tool. The
need is general, not code-specific: a researcher reading forty fetched pages pays every page
into its own context forever (the n² law — "context rot" — is why Anthropic's subagents
return 1–2k-token summaries from tens of thousands of tokens of work). Adopt it as general
machinery:

- **One tool, warden-granted**: `delegate(role, brief, expected)` spawns a child agent
  through `build()` — the one constructor — with a **narrowed** grant (⊆ the parent's tools,
  minus `frame.publish`, minus `ask_user`, minus `delegate` itself: **depth 1 is the law**,
  the ADR-045 bot-recursion decline reaffirmed at tool level), fresh context (the brief is
  all it knows — the Claude Code subagent contract), non-interactive, and returns exactly
  one message, clipped at a conf ceiling with an honest truncation marker.
- **Roles are the two blueprint built-ins**: `explorer` (read-only: read/list/grep-class
  tools) and `worker` (the parent's frame-write set). A delegate is never a bot and never
  plannable — it does not exist in the roster; it is the parent's context, quarantined.
- **The parent stays the author.** A delegate publishes nothing and its narration reaches no
  record; its return value lands in the parent's context as a tool result, journaled
  `tool.called delegate` with the role + brief-summary label (keys-only law), metered to the
  parent's step (`deps.emit_usage` — Rule 8 holds with zero new wiring), bounded by the
  step's own budget and `CLASS_TIMEOUT_S`.
- **The decision rule, one line:** *if the child's work deserves its own receipt, it is a
  step; if it only saves the parent's context, it is a delegate.* Steps are durable,
  verified, steerable, and individually billed on the record; delegates are context
  economics. A delegate that starts feeling like a step (wants to publish, wants to ask,
  wants siblings) is the plan telling you where it belongs.

## 4 · Robustness — every practice mapped to its machinery

The field's robustness recipe, row by row, against what already stands (this table is the
"how is it robust" answer; rows marked **NEW** ship with this ADR's workstreams):

| Practice (source) | Plane machinery |
|---|---|
| Durable execution; a dead worker re-bills only the in-flight step (Temporal event history; DBOS checkpoints) | The spine since M1: `@DBOS.workflow` per task/step, deterministic ids (`task-{id}`, `step-{id}`), effects journaled, enqueue-after-commit + sweeper |
| Idempotent dispatch; racing enqueues collapse (DBOS dedupe) | The id grammar + `client_token`; proven again by ADR-048's three layers |
| Partial-result harvesting with an explicit completeness policy (EIP Scatter-Gather: wait-all / quorum / deadline) | The join law, typed pure total (`ai/policy/joins.py`): `all | quorum | consensus | primary_input`; a lost seat costs a voice, never the merge |
| Per-worker caps and kill switches (OpenAI guardrail tripwires; Claude Code `maxTurns`) | Per-step budget + `CLASS_TIMEOUT_S` + `MAX_ATTEMPTS=3` + `MAX_STEP_QUESTION_GATES=2` + the Bursar at every frontier; cancel is the one mid-step interrupt |
| Supervise-and-restart, cheap because replay is free (Erlang OTP + durable execution) | DBOS recovery + the entry triage made total (ADR-028 §1); `FIRST_EXCEPTION` sibling-cancel on waves |
| Disjoint scope at dispatch beats merge reconciliation (Anthropic) | **NEW** — `ScatterSpec.not_here` fences validated disjoint (§2); already law in the outline lane |
| Typed returns, validate-and-reask bounded (OpenAI structured outputs; Instructor) | pydantic-ai typed outputs + harvest validators + `collection_repair`'s conservative salvage |
| Summaries on the control channel, bulk by reference (Anthropic; MetaGPT; A2A) | narration + publish/manifest/mounts (§1); **NEW** — `delegate` extends it inside a step |
| Trace the tree, price the spans (OTel GenAI; Langfuse) | `step.prompt` + `tool.called` + `usage.recorded` per hop, `ORDER BY seq`; `UsageEvent.for_step` "the one place that knows for certain" |
| Effort scaled by declared class, never emergent (Anthropic) | **NEW** — `SwarmSettings` width table + triage class (§2, ADR-049) |
| End-state evals first, trajectory second (Anthropic; ADK) | The verify/QA receipts grade the artifact, not the path; trajectory evals stay deferred, named |
| Deploy around in-flight agents (Anthropic rainbow deploys) | Fires and steps are short; version-matched DBOS recovery; the ADR-048 amendment killed the one eternal-sleeper shape — nothing to orphan |

## 5 · Deliberately NOT adopted, each with its reason

- **Free-form agent-to-agent messaging** (A2A as internal bus, AutoGen group chat, MetaGPT's
  pool): models stay out of routing (ADR-028 §5); a message an agent routes is a decision no
  code validated. A2A remains interesting at the *product boundary* (Plane as a callable
  peer) — that is ADR-037's inbound-door territory, not the internal plane.
- **A compression/compaction agent** (Cognition's single-thread prescription): the thread
  transcript IS the compaction — deterministic, derived from the record, cache-friendly
  (ADR-018 §3) — and a model summarizing the record would drift from it. Cognition's
  underlying principle (actions carry implicit decisions) is adopted structurally instead:
  writes are never parallel without fences.
- **Handoffs** (OpenAI transfer-of-control): control transfer mid-run is replanning without
  the record; Plane's equivalent is a plan edit with history, and the DM lane already covers
  "route the whole task to a specialist" at the door.
- **Reducers over shared state** (LangGraph): admired, unnecessary — no shared mutable
  state exists to reduce. The lineage boundary cut is the same guarantee by removal.
- **Async orchestration v1**: Anthropic itself ships synchronous waves and names the
  trade ("bottleneck, but simpler coordination"); Plane's waves + parked gates + the
  frontier loop are that shape. Continuous promote-and-enqueue dispatch stays the named v2
  (ADR-025's deferral), taken up when a real DAG shows wave-barrier waste.
- **Bot→bot recursion**: ADR-045's decline stands; `delegate`'s depth-1 + no-self-grant is
  the bounded, tool-shaped answer to the same want.

## 6 · The failure matrix (contract rows → tests and drills)

| Failure | What happens | Why |
|---|---|---|
| A scatter seat dies (model dead, budget out, third strike) | Seat parks SKIPPED; synthesis runs on survivors under `quorum`; the gap is named in the deliverable | Join law + `tolerates_missing_inputs`; the fleet-seat path |
| Every seat dies | Synthesis's quorum guard fails the task honestly with the diagnosis | ADR-028 §1 — a settled wave below quorum never strands the graph |
| The map step proposes overlapping fences | `swarm.expand` refuses; the attempt fails legibly and rides the ladder | Validation is code; dedup at dispatch |
| A delegate hangs or floods | `CLASS_TIMEOUT_S` + the return clip; the parent reads a truncation-marked result and decides | Depth 1, one message, bounded |
| A delegate tries to publish/ask/delegate | The grant never contained the tool; warden refuses, journaled | Narrowed grants by construction |
| Worker killed mid-wave | DBOS resumes each step workflow; DONE steps never re-run; the wave re-joins | Durability spine; entry triage total |
| Fan-out flood (a hostile or confused map) | Width clamp + `MAX_STEPS` + per-seat budget floor + the expansion cost gate + the Bursar | Money is the backstop, ADR-048's rule one lane over |
| Two scatters race one budget | The Bursar re-checks at every frontier; the second wave parks on a budget gate | Existing machinery, no new lock |

## Consequences

- **Zero new dependencies. Zero migrations.** `ScatterSpec` is a schema value; `swarm.py` is
  policy; `delegate` is a tool + labels row; widths are conf. The one new event payload is
  nothing — scatter rides `plan.edited`/`task.planned` exactly as the long-form expansion
  does.
- `CLAUDE.md` gains (on acceptance): *"Orchestration: briefs down, artifacts up, siblings
  never speak; scatter width is config behind triage class; `delegate` is depth-1 context
  quarantine — never a publisher, never a step's substitute."*
- The doctrine sentence: **the plan is the protocol; everything an agent exchanges is on the
  record or does not exist.**

## Workstreams

- **W1 — the constitution on paper**: §1 into CLAUDE.md/HANDOFF §8 as amendment lines; the
  robustness table into `docs/runbooks/` as the orchestration contract; doctrine tests that
  pin the sideways-never law (no tool grants a step another step's live state). Exit: the
  doctrine test suite green.
- **W2 — the scatter shape** — **BUILT 2026-08-27**, and the build settled more than the
  sketch above:
  - *The shape as landed:* `schemas/swarm.py` (SeatBrief/ScatterResult/ResearchRecord/
    RecordSet — the record atom is WANDR's {entity, url, verbatim excerpts, answer});
    `ai/policy/swarm.py` (the deterministic door: enumerate-verb + count grammar at
    ≥ `swarm_min_count`, no model consulted; validate with legible refusals; expand;
    canonicalize/dedupe/tally; rescatter); dedicated `surveyor` (frontier, maps the
    territory into fenced SeatBriefs) and `gatherer` (fast executor, one seat, exhausted-
    honesty) agents; `operations/swarm.py` (expansion + the after-gather hook);
    conf trio `swarm_max_width=8 / swarm_max_rounds=2 / swarm_min_count=8`.
  - **THE ITERATION LAW (added over the sketch):** a scatter does not end at one wave.
    When the wave settles, the tally (pure code) canonicalizes + dedupes the published
    records against the map's target; an undercount RE-SCATTERS the gap — a smaller wave
    onto non-exhausted fences only, each seat carrying the exclusion list — bounded by
    `swarm_max_rounds`, all-fences-exhausted, and the Bursar. The re-scatter wave is
    written BEFORE the settling seat's completion promotes synthesis (race-free by
    construction: synthesis join="quorum" sees the new inputs). Every ending is one of
    three honest reasons: target-met · rounds-exhausted · ground-exhausted.
  - *Proof:* 54 unit laws + a 40-seed narrative SIMULATOR (scripted seat deaths, aliases,
    dupes, exhaustion; invariants asserted every round: coverage monotone, rounds capped,
    exclusions only grow, exhausted ground never re-plowed) + 2 integration walks (expand
    → undercount → round-2 wave with exclusions → met target → no third wave) + SIX
    ITERATED LIVE RUNS. Run 2 (target 10): route=scatter on the flight recorder, 4 fenced
    seats, 24 records, target-met round 1, synthesis document with 27 entities each
    carrying year + repo + seat provenance — $0.17 of a $2 cap. Run 4 (target 40): first
    live tally — `18/40 — ground-exhausted`, the honest close. **Run 6 (target 60): the
    whole iteration law on one record — 6 seats → `44/60 — continue` → `re-scatter: 1 gap
    seat for the 16 missing` (full exclusion list carried) → `Gather matplotlib-ecosystem
    · round 2` → `63/60 — target-met` → synthesis → delivered at $0.25.**
  - *Found live (three catches, none visible to the deterministic suites):* (1) the
    publish door's `dataset` kind accepts `.csv/.parquet/.jsonl` only — the map/records
    now publish as single-line `.jsonl`; (2) the count grammar needed a modifier window
    ("8 **open-source** vector databases"); (3) the after-gather hook originally ran
    BEFORE the settling seat's own DONE transition, so the wave never read as settled and
    the tally silently never ran — target-met and hook-never-ran are indistinguishable
    from the event stream, which is exactly why the undershoot run (target 40) exists.
    The hook now runs after DONE, before promotion, on both the done and skip paths.
    (4) seat identity was string surgery — a `{id}r{round}` suffix overflowed the 24-char
    id cap on the surveyor's own 23-char descriptive ids ("vega-altair-declarative") and
    the base-id parse split on the letter r, mangling every hyphenated id; the mint
    crashed, the best-effort contract delivered honestly, and the fix made identity a
    FIELD: `StepSpec.scatter_seat` carries the fence id verbatim every round (steps stay
    unique by row id; the round rides `scatter_round`) — no suffixes, no title parsing.
    Also live-taught: the gatherer's prompt now says the territory is the LIVE WEB — two
    seats had read the mounted map as their only source and declared honest exhaustion
    over an orientation file.
  - *Deferred out of W2:* a dedicated expansion-time consent gate — v1 rides the Bursar's
    **wave projection** (`swarm_seat_est_usd` × the gather seats about to dispatch, checked
    at every boundary exactly like the sectioned lane's) plus the round cap. (The earlier
    wording here claimed "per-seat budgets"; that control never existed in the code — the
    2026-08-29 review caught it, and the projection arm replaced the claim, same commit.)
    Also deferred: the planner-brief scatter section (the door is deterministic; the
    planner never sees scatter asks).
  - *The 2026-08-29 review (15 confirmed findings, fixed same day, pre-merge):* the tally
    now rides a SAVEPOINT — best-effort enforced, a failed tally can no longer poison the
    session (fail the task one seat short) or commit a half-recorded wave; harvests are
    EDGE-SCOPED to their own map step (a follow-up turn's scatter is structurally disjoint)
    and read the NEWEST readable artifacts (a retried surveyor's accepted map, not its
    rejected first attempt); re-scatter waves carry the map edge (the mount contract holds
    in every round) and round-suffixed record filenames (`records-<seat>-rN.jsonl` — the
    synthesis mount is last-write-wins per path, so round 2 was silently erasing round 1);
    seat identity and exhaustion key off the SPEC's stamp, never the model's echo; the door
    grammar no longer reads years, dollar amounts, or time spans as counts, and the door
    sits behind the shared TurnFacts blockers (pinned bots/connectors/dials → planner) plus
    a prose-kind guard (an auto-detected spreadsheet keeps the planner path, ADR-032);
    `canonicalize` is Unicode-aware (深圳腾讯 survives, Müller/Möller stay distinct — an
    ASCII fold was dropping non-Latin records from tally AND exclusions); the skill-router
    model call is not paid on this deterministic lane, while user pins ride every wave step.
  - *Amended 2026-11-15:* the `surveyor` and `gatherer` agents are gone; the map and every
    seat are the `researcher` in its lane modes (ADR-091), their craft the built-in `scatter`
    skill, and a record names its page by source id (`ResearchRecord.source`), the URL filled
    by the system from the read. The scatter shape, the tally and the iteration law are
    unchanged.
- **W3 — `delegate`**: the tool + warden classification + labels + meter attribution; the
  explorer/worker role pair; depth/one-message/no-publish enforced in `build()` wiring; unit
  + doctrine tests (grant narrowing, self-grant refusal). Exit proof: a researcher step
  delegates a 30-page read and its own context receipt (`step.prompt` + ledger) shows the
  summary came back without the pages.
- **W4 — the measured loop**: swarm rows in the eval harness (end-state graded); a perf row
  (scatter expansion arithmetic <1 ms, the size-resolution precedent); STATE/docs.

## Open questions for the owner

1. **The noun** — `scatter` for the shape and `delegate` for the tool are proposed here;
   both have blueprint/industry precedent. Confirm or rename (the fleet/routine naming
   sessions suggest you will have a view).
2. **Default `max_width`** — 8 proposed (Anthropic's "10+" is their frontier-budget posture;
   Plane's per-seat budgets argue for entering smaller and raising on evidence).
3. **`delegate` in v1 grants** — researcher + coder proposed; writer deliberately excluded
   (its work is single-context by nature). Widen or hold?
4. **Ship order** — W2 (scatter) before W3 (delegate) as cut, or reversed if the code lane
   lands first and wants `delegate` earlier.
