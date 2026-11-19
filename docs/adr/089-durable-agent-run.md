# ADR-089 · The durable agent run: a step attempt on its call log

**Status:** accepted · 2026-11-14
**Amends** ADR-064 (a cancel is the only token, and it is terminal) and ADR-069 (the call log sits beside the seam).

## Context

A worker that died inside a step re-ran the whole attempt: `rung.open_fresh` counted it a fresh
attempt, with a new sandbox and every model call bought again. DBOS replays `run_step`'s body, but
the agent's calls are not DBOS steps, so nothing below the body was durable.

## Decision

1. **Scope: step attempts only.** `operations/execution` opens a `CallLogScope` per attempt
   (`CallLogScope.open(PgCallLog(workspace), step, fresh=…)`) and `AgentRunner.run(log=scope)` enters
   it. `PrismoidDurability` (`ai/capabilities/durability.py`) is mounted on every built agent and is inert
   without a scope; every other run is a plain run. A law test holds both doors.
2. **Units.** pydantic-ai's durable-backend builder with Prismoid's backend (`PrismoidBackend`, JSON codec):

   | Unit | Key | Rows |
   |---|---|---|
   | model request | `<agent>__model.request[_stream]#<n>` | result + content fingerprint |
   | tool call | `<op>:<tool>@<tool_call_id>[!approved]` | intent before the body, result after (every tool) |
   | MCP discovery, capability operation | `<op>#<n>` | result |
   | argument validation, Lane-2 events | none | never logged; events suppressed while replaying |
   | run segment | `segment#<n>` (n >= 1) | intent = its input (key, line, the segment it continues) before it runs; result once a run of it returned |

   A row is durable when `record` returns: nothing is batched or written behind.
3. **A crash re-enters its generation.** A step recovered `running` re-enters the attempt it died in:
   no `attempt` bump, no infra bump, no budget gate, the latest generation replayed. Only a fresh
   ladder rung opens the next generation. A crash no longer costs a ladder attempt.
4. **The fence.** A replayed request whose fingerprint differs from the recorded one raises
   `ReplayDiverged`; a generation logged under another `LOG_VERSION` is never replayed. Both start the
   next generation, its prompt followed by a note naming every call that started and never reported an
   outcome, with its args. Every new generation writes its opening row (`segment#0`, holding the note),
   so it is the latest from the moment it opens and a later re-entry hears the note.
5. **One run, a chain of segments.** Every model run of a step attempt goes through `AgentRunner` on
   the scope: the prompt is segment 1, and `Continue` adds one more user line inside the trailing
   request of the conversation so far - a guarded step's next attempt (`attempt:<n>`), with a fresh
   request ceiling. Every run replays its
   chain from the logged inputs, so a re-entered attempt runs the prompt it logged, never a rebuilt
   one. A guarded run that dies says how its next attempt takes it up: cut inside a call (a provider,
   a transport, a tool), it re-enters the same generation; stopped at a logged point (its request
   ceiling, its output retries, a `failed` ending), the same generation continues with one line
   saying why; refused, or unguarded, the next attempt opens a fresh generation. How the next attempt
   takes it up, why it stopped and the steers taken since ride the step's record (`owed_run`, kept
   through every park and pace like the owed cut) until that attempt's `step.prompt` commits; it is the
   only thing a retry inherits (no carried conversation, no seat rotation: the model's failover is
   the run's own sticky fallback). A re-entered run keeps the epoch it started on: a steer accepted
   since reaches it at its first live safe point.
6. **Pinned names.** Unit names embed the agent, toolset id and tool, so every function toolset has
   a stable id and `tests/unit/test_operation_name_pins.py` pins the names derived from the roster.
   A rename or removal appends a pin section and bumps `LOG_VERSION`, which fences in-flight steps
   once; an added name strands no logged call, so it joins the current section with no bump.
7. **Two departures from the builder's defaults.** Runtime toolsets are refused inside a scope
   (they would run unlogged). The spec accepts a `CancellationToken`, which the builder advises only
   on a deterministic loop: Prismoid's cancel is terminal, a cancelled attempt is never replayed, and
   pause and steer never use the token. A step's run polls its task once a second
   (`ai/capabilities/cancellation.py`, no NOTIFY channel) and fires the token on a cancel: the
   stream stops within about a second rather than at the next safe point, its `message.end` is
   still sent, and the cut model request leaves no result row.

## Consequences

- **The one window:** a death after a provider answered and before its row committed buys that one
  request again (drill D1b).
- A park saves no conversation and no call ids, only its `ParkRecord` (`schemas/park.py`: kind,
  gate, the epoch the run was told, the browser's `once` widens, who paused) beside the wake's
  mapping of the gate's resolution. Its resume replays the generation to the ending it stopped on
  (no model call), reads the park's calls again off that turn (`classify.deferral_of`) and logs their
  answers as the next segment (`gate:<id>`), so a crash after the resume replays the same answers. A
  park saved before the call log is decoded by `V1Park.of`, the one reader of its keys, and seeds a
  fresh generation with its saved history and its answers, until those parks are gone.
- A tool that started and never reported is answered `OUTCOME UNKNOWN` at once by the tool record
  and never run again, unless the catalog marks it `READ` or `PURE`: those run again
  (`capabilities/tool_record.py`).
- A new generation (a fence, a fresh attempt) never repeats a write: a `WORLD` or `RECORD` call an earlier
  generation of the step finished under the same `call_digest` (name and args, hashed) is answered
  `ALREADY DONE` with its recorded result and not made again; reads and the frame's own verbs run again,
  and a connector's tool counts as acting whatever its server's hints say (`tests/integration/test_write_once.py`).
- Proof: `tests/unit/test_durability_capability.py` (in process) and
  `tests/drills/durability/test_call_log_drills.py` (SIGKILL per attempt, and under DBOS: a kill at request 3
  recovers with 5 provider calls and the workflow's journal matches an uncrashed run's), and
  `tests/drills/durability/test_call_log_parks.py` (a park, its re-entry, its resume and a kill after it buy
  nothing twice).
- Cost: one insert per model request and two per tool call, against a budget of 5 ms p50 and 20 ms
  p95 per unit that the overhead drill enforces.
