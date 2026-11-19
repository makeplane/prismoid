# ADR-045 — Bots: user-authored agents as data, and One, the front door that routes to them

**Status:** accepted · 2026-08-21 · W1–W3 landed and proven live; W4–W7 in the same arc
**Numbering:** 035–044 stay reserved for the software-factory blueprint
(an unpublished design study of 2026-08-20); the repo already carries a 002–005 gap,
so a hole is precedented and honest.

**Context:** every extension point was already data — a model is a catalog row (§9), a skill is
a registry row (§11), a connector is a user's row (ADR-020) — except the agent, which HANDOFF
§11's own custom-agent doctrine spelled as *a file plus an import in `all.py`*: a deploy. Users
want to create bots, teach them, and have one orchestrator route across them and the built-ins.
The thesis, one sentence: **everything a user composes becomes data; everything that must be
safe stays code.**

## 1 · A Bot compiles to an `AgentDef`; it never enters `REGISTRY`

`REGISTRY` stays the process-global dict of built-ins. A bot crosses into the engine as a frozen
value: row → `ResolvedBot` → `TaskDeps.bots` → `ai/agents/custom.compile_bot` → `AgentDef`. The
compile lives beside `base.py` because `AgentDef` is the top of the engine contract; `deps` sits
below it and may not hold one. `build()` grew one line — `definition_for(name, deps)` asks the
registry FIRST, so **a built-in always wins a name collision** (a user who could shadow `judge`
could author a friendly grader), and `deps.bots` is populated only on the step path, so the
light agents can never be served by a bot: enforced by construction, not by discipline, exactly
as `connectors` is. The compiler derives `verifier_suite` from `produces` (a bot cannot pick its
own grader — ADR-015's rule), refuses `judge`/`image-gen` capabilities (ADR-024's lanes), and
holds a compiled bot to every invariant `test_agent_registry.py` holds the built-ins to.
**Amended 2026-11-14:** a bot is an overlay on the agent it runs on (`ai/bots/overlay.py`): it adds
its author's voice and narrows that agent's hands, never widens them. In its conversation it runs on
the answerer's hands; in a task step, on what the step seats hold (their grants rows), so a grant no
base holds mounts nothing and a bot asks only where its base may. A reserved or built-in name is
refused with a name the author can take instead; the boundary is the structured `not_for`, in any
language (verbatim prose still carries a "Not for…").

## 2 · The lifecycle is the skill lifecycle, plus scope

`bots_registry` mirrors `skills_registry` — draft → test → publish, versions immutable,
publishing needs green evals **as an HTTP 409**, drafts resolve only for their author. Two
deltas, each with its reason. **Scope:** `space_id NULL` is personal to `owner_actor_id`; set,
the bot is shared under the §6 matrix. Two PARTIAL unique indexes carry it, because Postgres
treats NULLs as distinct and one plain constraint would neither keep a personal name unique per
owner nor a space name unique per space. **Instructions inline**, not MinIO: a skill body is a
bundle copied into a frame every step; a bot is ≤5,000 tokens of judgment read once, and the
object store would buy only dedup (Rule 13). The sha256 column stays — it is what makes an
unchanged push a no-op and `bots diff` cheap.

## 3 · The publish gate is a selection eval

Cases are `(goal, expect)` where expect is the bot's own name or `none`; the `selector` (the
router's twin, one level up — CHEAP_CLASSIFIER, `plannable=False`) scores them against the
roster the planner would actually see, with the draft's own description swapped in. Honest about
what it measures: production routing is One's job, but a real planner call per case is the
multi-minute real-money trade ADR-015 M5 refused for skills, so the gate is a cheap proxy for
the one thing an author controls — **is the description sharp enough to win its own goals and
refuse everyone else's?** A `none` case passes when the selector picks nothing OR a built-in;
precision is the half that is easy to lose. End-to-end proof is the nightly `bot-golden` lane,
advisory like `skill-golden`; the nightly `bot-selection` re-run is the roster-drift alarm.
Model calls run in the CLI, never the API (Rule 5) — there is no test route.
**Amended 2026-11-15:** the `selector` is gone (its name resolves to `lead`). Each case runs the real
Lead, built through `build()`, decision-only (`ai/agents/decision.py`, shared with the offline decision
runner): the goal as a person's first turn with questions off, the roster its author's tasks plan
from (`planning_brief.roster_section`) with the draft swapped in, stopped at its first ending or first
non-pure call before anything acts. A case passes when the plan seats the bot; a `none` case is a
near-miss and passes unless a step seats it. A reply, an offer, or a read or act before any plan seats
nobody. The nightly `bot-selection` suite re-runs each bot through this same gate as its owner.

## 4 · One is the planner's seat, made legible — not a new dispatch path

One is not a new agent file and not a bot: it proposes into the code-owned DAG, and models stay
out of routing (ADR-028 §5 is untouched). Three additions: (a) `_plan()` composes
`roster(plannable=True)` with the task's compiled bots — auto-included from the Space plus the
creator's own published bots (a roster you must attach is not a roster); `tasks.bot_pins` is the
override that narrows or pins a draft; (b) the brief renders bots as a fenced section labelled
author-written data — the structural defence is that `validate()` checks the graph regardless,
so untrusted text can at worst buy a bad *choice*; (c) `ProposedStep.why` → `StepSpec.why` →
`PlanStepView.why` carries the routing reason on the durable record, riding inside the existing
`task.planned` frame — zero new wire event variants for the feature's whole surface (the one new
variant, `step.prompt`, is §6's and additive). `_pin_bots` stamps each bot step with the version
that planned it: publishing v2 mid-task never changes what a running task is doing, the same
guarantee `skill_pins` makes one level down. The fallback floor stays built-in-only — a degraded
path landing on someone's bot would make the planner's worst day a surprising one. Follow-ups
supersede pins by name; forks inherit the parent's versions (ADR-018's dials).
**Amended 2026-11-15:** a bot step's skill pins pass the same door as a built-in's: a pin runs only
where the compiled `AgentDef` holds every tool the skill declares (`allowed-tools`, ADR-015's
amendment), so a skill can never widen what a bot's overlay narrowed.

## 5 · Teaching = distil a real run (W4)

`computer bots teach <name> --from-task <id>`: `digest_run` gathers what the record already
holds — the plan with each step's `why`, the whole conversation (every correction, gate answer
and steer is in it), the keys-only tool journal, the signed verdicts — into a `RunDigest` value;
`bot_lesson` from a run (FRONTIER_REASONER, no tools, `plannable=False`) proposes a `BotLesson`: full
revised instructions (never a patch — models write documents, `bots diff` renders the change),
optionally a sharpened description, ≤3 new cases mined from the run. The result is only ever a
DRAFT: the author diffs, tests, publishes — or discards. Teaching and testing are the same act.
This is the answer to teach-by-screen-recording, and better here: nothing to infer from pixels.

## 6 · The prompt snapshot (the one thing stolen from DeepSeek Harness)

DeepSeek Harness records every context injection; Plane's record had everything *but* the
assembled prompt, and "why did my bot do that" is unanswerable from narrations alone. One
durable `step.prompt` frame per attempt — agent, attempt, the text handed to `agents_run`,
clipped at 32k chars with an honest marker — written BEFORE the run so a wedged attempt still
shows what it was told, best-effort so the receipt can never cost the step. Mounted files are
named by the prompt, not duplicated: they are durable elsewhere. Excluded from the transcript's
allow-list by construction.

## 7 · Standards: adopted and declined, on the record

**Adopted — Agent Plugins v1.0.0** as the on-disk shape: v1 defines exactly two portable
component types (skills, MCP servers) and names agents as out of scope, so a bot lives in the
spec's own escape hatch — the reverse-domain namespace `so.plane.computer/bots/` — and the rest
of the bundle is portable to any conformant client. One `$schema` URL literal is allowlisted in
`tools/audit.sh`, commented: it is a version tag a client matches, never an endpoint we call.
**Declined — ACP** (both protocols: Agent Client Protocol is editor→agent, the inverse of MCP
and a separate product surface; Agent Communication Protocol is agent↔agent, and bots compose
through the plan DAG, which is durable, budgeted, verified and steerable). **Declined —
Cordis-style runtime hot-swap**: a swappable agent loop is the property ADR-028 refused, and
runtime mutation is hostile to ADR-027's recover-from-DB-state model. The reframe that survived:
*DeepSeek says "everything is a plugin"; Plane says "everything composable is a row."*

## 8 · Money, and what was deliberately NOT built

Metering is untouched — `build()` wires `deps.emit_usage`, so Rule 8 holds by construction;
per-bot spend is a join (`bots logs`), not a table. A bot cannot raise a budget. NOT built, each
with its reason: bot→bot recursion (wants depth caps, subtree budgets, cycle detection — its own
ADR); lessons-from-corrections as standing state (W4's natural sequel); conversational
authoring; proactive "make this a bot?" (needs a scheduler; `beat.py` is a pulse, not a clock);
seeded builtin bots (the built-in roster stays code); a marketplace (tenancy is ADR-014's
deferral — plugin export is the honest substitute); roster shortlisting via the router (the
named scale valve past `MAX_ROSTER_BOTS`).

## Verified

`just check` green throughout · unit + integration suites cover the compiler invariants, the
format door rule-by-rule, the §6 role matrix, the 409, author-only drafts, collision refusal,
pin reproducibility, roster fencing, and the digest. **Live proof campaign, 2026-08-21:** push →
4/4 selector calls green → publish; an over-claiming description went 3/4 RED and publish 409'd;
One planned a real MSA review onto `contract-reviewer` (not the writer) with its `why` on the
record, pinned `@5`; the bot delivered a 1,543-word clause-by-clause redline; the DOCUMENT suite
verified it exactly as a built-in's (41 numbers grounded, 22 quotes verbatim, one derived figure
→ honest fail). The campaign found two real defects, both now wearing tests: a text-lane bot
went looking for a publish tool it was never given (fix: `compile_bot` appends a system-authored
delivery law — the system states its own fact rather than hoping the author guessed it), and
`_publishes_text` asked the registry so a bot never system-published — "produced nothing" ×3
with the finished review in the run's output (fix: it asks `definition_for`, the question
`build()` asks). Also confirmed live: payload widenings ride an API restart before the worker
(ADR-028's deploy-order note), and a stale dev worker on two-day-old code fails every new-schema
task — kill long-lived workers before proving new columns.

**Amended 2026-11-15 (ADR-092, the planner and router deleted):** One is the Lead. The roster it plans from is `planning_brief.roster_section` (built-ins, then the task's bots, fenced as author data), a plan is checked in-run by the full rule set (`ai/capabilities/plan_rules.py`, which the commit holds it to again), and a skill's publish gate, like a bot's, runs the Lead decision-only (`operations/skill_selection.py`). `planner`, `clarifier` and `router` resolve to `lead` through `ALIASES`.
