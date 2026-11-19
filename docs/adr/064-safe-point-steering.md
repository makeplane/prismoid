# ADR-064: Safe-point steering with plan epochs

*Status: accepted, 2026-10-02 · Phase 1c*

**Amended 2026-11-15 (ADR-092):** the Lead is a step (the turn's first), so these safe points apply to it; its re-run and re-plan rules land with its steering change.

## Context

A steer was recorded at once and read only when a step started, so a team steering a long step
waited for it to end; a pause flipped the row while the step ran on. The program spec
commits Phase 1 to steering at safe points, with an acknowledgement and a plan epoch as the
product and aborting an inference call as an optional optimisation. It amends CLAUDE.md's rule
"Steering merges at frontier boundaries only (cancel is the only mid-step interrupt); pause stops
dispatch at the boundary".

## Decision

- A steer is a durable, attributed command: a mutable `steers` row (a tenant child of `tasks`),
  idempotent per task on the client's `command_id`, beside the append-only `plan_edits{steer}` and
  `plan.edited{steer}` that already carried it. `task.steered` is removed (never emitted).
- Safe points: before each model request, before each tool dispatch, and when a step's run is
  about to end, plus every frontier boundary. A tool already running always finishes; cancel stays
  the only hard stop. The mechanism is one pydantic-ai capability (`ai/capabilities/steering.py`) fed by
  a `SteerInbox` on `TaskDeps`, so `ai/` stays persistence-free.
- A call the model chose before a steer arrived is held (`SkipToolExecution`), never run. A parked
  approval a steer overtakes is superseded. A question or connect gate stays open.
- A steer by anyone but the task's creator borrows her connectors for the rest of the run it
  reaches (1a's borrowed-turn rule, owner ruling 2026-10-01, which the mount reads only when a run
  starts): from the safe point that tells the run, every call to her connectors asks her first, and
  a run that cannot ask is refused the call (`TaskDeps.connectors_borrowed`, set by the capability).
- Every accepted steer, and every turn's start, bumps `tasks.plan_epoch` under the task row lock.
  A steer issued against an older epoch is a 409 `stale_steer` naming what its author missed.
  The author's own later steers are not something they missed: when every steer since their
  epoch is theirs and no turn started since, the steer is accepted at the current epoch.
- Unstarted steps re-plan at the next frontier boundary when the epoch moved since they were
  drafted, but only for a turn whose latest `task.planned` route is "standard" (the planner and
  dial lanes). Action, navigate, fleet/council, direct (DM, ADR-045), express, sectioned
  (long-form) and scatter turns are not re-planned; their unstarted steps take the steer through
  their contracts when they start (`applied{before_model}`). The re-plan (`planning.replan_remaining`)
  drafts first, so nothing is deleted before the model call; it re-checks under the task row lock
  and keeps the plan ("Kept the plan") when the planner fails, when the steer it drafted for was
  retracted meanwhile, or when the frontier moved. It wires EVERY done step of the turn that has a
  live artifact to every new root (not only the sinks), so skip-edge inputs keep mounting, and the
  planner is told what the turn already did. On a deterministic lane the plan's commit marks a
  steer `applied{replan}` though only the thread and the steps' contracts carry it to the work.
- Fences. A step that finished without an accepted steer of its turn goes again in place, its
  output superseded (fence 1, at step done); a turn about to deliver with an accepted, unapplied
  steer re-drafts each sink as a NEW step, with an edge from the old sink to its copy (fence 2;
  the answerer's express step named by the turn's hatch edit is never re-drafted, and the
  superseded sinks' narrations leave the delivered summary). Neither fence re-runs a step of an
  action or navigate turn, since a re-run repeats an external action: fence 1 lets the step
  finish; fence 2 rejects the turn's unapplied steers ("the turn ended before this applied") and
  delivers. Nor does either re-run a step whose superseded attempt acted outside the task, or may
  have: a connector call (a read too, and a failed one, which may have acted before it raised) or
  browser action (a snapshot too) on that attempt's journal, or `frame.shell` in a frame with a
  route out (any host its allowlist holds: the contract's domains, or a browser frame's allowed
  sites, which are never empty) - never a call a steer held, which never ran - lets fence 1's
  step finish, and a sink like it makes fence 2 reject
  the turn's steers rather than re-draft any sink. A task from before 1c (`turn_epoch` 0) rejects instead of
  re-drafting, and the sweeper delivers stranded tasks with `reopen=False` (rejects). A
  superseded artifact is never taken back: a re-run that republishes identical bytes gets a new
  artifact row (the content-addressed blob is reused) and one normal `artifact.produced`; the old
  row stays superseded, and its receipt is never signed.
- The frontier boundary runs before the budget check. After a budget gate resolves, the pre-park
  frontier dispatches and its steps take the steer at their first model request; the next
  boundary re-plans what remains. Resume lands in `waiting` when a question, connect or approval
  gate is still open. After the row lock, the steer and retract doors re-check authorization if
  the task moved (a move home refuses anyone but the creator).
- **The invariant:** before a turn delivers, every steer accepted in it is applied, rejected or
  retracted.
- The thread transcript (every planner's brief, the boundary re-plan, `/task/thread.md`) never
  reads a steer that did not apply as instruction: a retracted one is dropped, a rejected one reads
  `[not applied: <reason>] Bob steered: …`. Both settle inside the steer's own turn, so a finished
  turn renders the same in every later transcript and the prompt-cache prefix holds. A fork
  carries only applied instructions (2026-10-06): a parent's steer still open at the fork's bound
  belongs to the parent's run and reads `[not carried: the fork was made before this applied] Bob
  steered: …`, read at the bound, so whatever the parent does with it later never moves the fork.
- Pause parks a step that can defer (`can_ask_user`; a `CallDeferred` on any other agent is a
  `UserError`) at its next tool dispatch on a question-less PAUSE gate that never times out. The
  answerer, fan-out seats and bots that cannot ask pause at the frontier, as before. Each park
  holds a queue slot, so a step parks only while fewer than `COMPUTER_STEERING_MAX_PAUSE_PARKS`
  (default 4) PAUSE gates are open across the deployment; at the cap the step finishes and the
  frontier holds the pause.
- A paused task stays paused until a person resumes it: a park on any gate, or a gate's answer,
  leaves it `paused` (`Task.stays_paused`, in `execution.advance` and `gates.advance_status`), and
  the resume puts it where the parks left it. A failure still lands (paused → failed), and a
  delivery that finds the turn paused answers `held`, so the workflow holds for the resume. Every
  status decision reads the row under its lock (`Task.held`), never a copy read before it.
- Spend follows the epoch: a step's usage is labelled to whoever opened the epoch it has applied.

**Received and accepted land in one commit.** Every check that accepts a steer (authorization,
task state, the epoch compare-and-set) runs synchronously under the task row lock, and a failed
check is a 403, 409 or 422 with no row. The record shows a steer accepted from its first frame;
the visible gap is between accepted and applied. This deviates from the program spec's three
separate acknowledgements, on purpose.

Rejected: driving `agent.iter()` by hand (the run loop leaves `ai/`, the graph walk is duplicated);
ending the run at every steer and resuming from history (a dump and a round trip per steer).

## Consequences

- New effects in `run_task` change its step sequence: a `run_task` in flight at deploy relies on
  the worker-version fence.
- Each safe point costs a few point reads: the org bind, the task, its steers, and an actor per
  steer (the author's name).
- A pause park holds a DBOS queue slot until the DART port, like every park.
- Early cancel of an in-flight model call is not built: the week-one spike read NO-GO per call
  (an aggregate reading would pass; the owner may overrule), so `COMPUTER_STEERING_EARLY_CANCEL`
  does not exist and the `interrupt` safe point is unused.
- The CLI's `computer do --wait` never prompts on a pause gate; the gates API refuses a PAUSE gate
  (resume lifts it).
- A chat or channel reply over 2,000 characters for a running task is refused at the chat door
  (2026-10-06): the line is kept, wearing "too long to steer a running task …", nothing steers, and
  a channel answers `refused` (it was a 500 before 1c, then a 422 that rolled the line back).
- Superseded artifacts never appear on the rail shelf or a delivered card; the timeline keeps them
  greyed ("Superseded by Bob's steer", on a personal task "Superseded by a steer").

## Amendment 2026-10-15 - the RunGuard rides the safe points (agent loop, round 1)

The RunGuard watches the same safe points. Budget notices at 50% and 75% are persisted. At the
cap a BUDGET park is a park like PAUSE, with one difference: it has NO timeout (spec said the
timeout declines; the code never expires it). **Owner item**, with its twin: declining a
mid-run park leaves the task running, so a multi-wave task may open a second "Budget reached"
gate. Stalls notice, then finish: the same call 3 times in 8, or 5 consecutive acting verbs
(`browser_fill` counts) on an unchanged page. Finish-now exempts `done`, `browser_handoff`,
`ask_user` and `request_connector`, which are ways to finish. The request ceiling is 200
(`PRISMOID_WORKER_REQUEST_CEILING`). A continued retry hears the steers taken since: owed steers
accumulate on the step record's owed run across rungs that never ran, so no applied steer goes unseen. Open:
two narrow epoch windows (a held call's steer whose run dies before the next request).

## Amendment 2026-11-14 - the cancel token (ADR-089)

A step attempt runs on its call log (ADR-089), whose engine accepts a `CancellationToken`: only a
terminal cancel ever fires it, and pause and steer stay at the safe points, never the token.

## Amendment 2026-11-15 - the Lead is a step (ADR-092)

The Lead is the turn's first step and every safe point, epoch and spend rule above applies to it: a
steer reaches its run in flight like any step's (a call it chose before is held, the next request
carries the steer, an ending is redirected), and an ask's answer starts a fresh run told every steer
since. The frontier re-plan of a turn the Lead planned is the Lead's replan seat (`revise_plan` or
`keep_plan`), not the planner: a revision replaces only the steps not started, a keep leaves them
carrying the steers, both apply them `{replan}`, and any error keeps the plan. Fence 1 applies to the
Lead's step unchanged; recipe steps take a steer through their contracts.

**Amended 2026-11-15 (ADR-092, the planner deleted):** `planning.replan_remaining` is gone: the boundary re-plans only a turn the Lead planned (`turn/replan.at_boundary`); any other plan stands and its steps take the steer at start. An editor's replan (`POST /v1/tasks/{id}/replan`) is the Lead's replan seat told the editor's words as their steer (`turn/replan.by_editor`), attributed to them on the record, and a 409 when the task is settled, a step is mid-wave, nothing can be re-planned, or the frontier moved while the Lead drafted.
