# ADR-010 — Fleet mode: every model answers; nothing is forced to agree

*Status: accepted · July 2026 · supersedes the council shape of the first build's milestone M6.1 and §1's Council row*

**Naming (owner's call, 2026-07-26):** the mode is **`fleet`** — planes fly in fleets, and a
fleet is many independent craft with no requirement that they agree on the route. "Council"
described the v1 design this ADR removes (deliberation toward one verdict) and collided with
the unrelated `ai/council/` package (the deterministic "code disposes" half of the engine,
which keeps its name). `council` remains a legacy wire value: accepted at the boundary, stored
as `fleet`; historical rows and their `council_report`/`council_opinion` artifacts parse
forever. The flag is `computer:fleet`; the chair's table publishes as `fleet_report`.

## Context

Council v1 convened three `counselor` opinions (one per model family) and a `chair` that
synthesised them into ONE report — a recommendation "on balance". The owner's actual intent for
a council is different: **the model-picker, inverted.** Standard mode routes one model to the
goal; a council should run the *same task through every enabled model*, each producing its own
artifact exactly as standard mode would, so the answers can be compared side by side — X's
answer, Y's, Z's. A forced synthesis hides exactly the thing a council exists to reveal.

## Decision

- **The plan shape** (`ai/council/planner.council_plan`, still a deterministic template): one
  `writer` step per eligible model — pinned via the new `StepSpec.model_ref` — each publishing
  a plain `document`, plus one `chair` step gated by every answer via `produces_for`.
- **Pins are data.** `model_ref` is a catalog ref chosen from the *enabled catalog at plan
  time* (like a skill pin), honouring Rule 7's intent: no model strings in code. The gateway's
  `resolve(pin=…)` serves exactly that ref — no class filter, no fallback chain, because a seat
  another model quietly answered for is not that model's answer. A pinned ref that is no longer
  enabled fails the seat with a `PolicyDenied` that says so.
- **Eligibility is data too**: every enabled catalog row serving an executor class
  (`frontier-reasoner`, `fast-executor`, `long-context`, `code`). Judge-only and vision-only
  rows grade and read; they don't get a seat. Width caps at `MAX_STEPS − 1`.
- **Parallel by construction.** The answer steps share no edges, so they form one frontier
  wave; the M3.3 fan-out (shipped on) dispatches them as concurrent child workflows on the
  step queue — N answers cost roughly one answer's wall clock.
- **The chair is a clerk now**, on the cheap tier (`fast-executor`): one table row per answer
  (model · position · strongest point · caveat), agreement reported **only where it verbatim
  exists**, disagreement left standing. No merged recommendation, no vote, no tie-break —
  "No common ground." is a valid section.
- **The honesty guarantee survives**: the chair's table is verified by the council suite's
  quote-match (M6.2) against the mounted answers, so a manufactured consensus still fails
  verification mechanically. Each answer is verified by the document suite, exactly as a
  standard-mode artifact is.
- **Removed**: the `counselor` agent (file, prompt, roster entry). `ArtifactType.COUNCIL_OPINION`
  stays in the schema so historical rows parse; nothing produces it anymore.

## Consequences

- A council of N models yields **N + 1 artifacts**: N independently verified answers and one
  comparison table — the deliverable the user actually wanted from "council".
- Cost scales with the enabled catalog (every seat is a real run plus its verification);
  `--budget-usd` and the Bursar's gate remain the throttle. Disabling a model removes its seat
  — the roster is the catalog, as ever (§9).
- Old council tasks and their `council_opinion`/`council_report` artifacts remain readable and
  verifiable; the append-only record is untouched.

## Amendment 2026-11-15 - the writer lays the table; seats are bought by the budget

- The `chair` agent is deleted; its name resolves to `writer` (ADR-091). The table step is the writer with
  `lane_role="fleet_table"`, taking its table part (`base.Mode`), whose craft is the built-in `fleet-table`
  skill read from disk. It cannot ask, holds no connector and streams its draft; it still publishes a
  `fleet_report`, verified in line by the quote suite.
- Planning buys seats in catalog order while each seat's writer run, at its own row's prices, fits the task's
  budget left beside the table (the first seat always sits; the boundary's budget gate then asks). Every
  fleet step carries its envelope from that one estimate.
