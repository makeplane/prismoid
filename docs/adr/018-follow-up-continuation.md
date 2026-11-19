# ADR-018 · A follow-up continues the task; fork is the only branch

**Status:** accepted · 2026-08-06
**Replaces:** the follow-up-as-child-task model (§8.4 of the Phase-1 brief; "the thread is a
tree", DESIGN § States v3) — removal, replacement, and reason recorded here per the standing
rule.

## Context

Every follow-up minted a **new task**: a child row (`parent_task_id`), a new URL, a full page
remount, and an agent whose entire inheritance was the parent's published files plus a
**≤400-token** thread lane (title + tail of the delivered summary + filenames) reaching **one
hop** up. The web stitched the chain back together client-side, so the human saw a
conversation the model never did. Users experienced exactly that asymmetry: *"every follow-up
creates a new task and the context resets to zero."* Fork, meanwhile, was literally
`follow_up()` with four arguments pinned — the two gestures were one code path wearing two
names.

## Decision

1. **Follow-up = the same task's next turn.** A settled task (delivered / failed / cancelled)
   takes the message, re-enters `planning`, and runs again on the same row — same record, same
   stream, same URL. `tasks.turn` counts the conversation; the durable `task.continued` frame
   is both the user's turn in the record and the status signal. The settled statuses gain
   exactly one exit (`→ planning`); `TERMINAL` still means "settled" for every archive /
   delete / steer guard.
2. **Per-turn workflow ids.** A completed DBOS workflow id never re-executes, so turn N runs
   as `task-{id}-t{n}` (turn 1 keeps `task-{id}`). Every task-level wake — gate resolution,
   resume, cancel — is addressed per turn; status-writing effects carry `turn` so a
   crash-recovered old-turn workflow can never write over the turn that superseded it; the
   sweeper rescues a continuation's commit→enqueue crash window from `planning` under the
   same deterministic id.
3. **The thread transcript is the context.** `operations/thread.py` re-derives the whole
   conversation from the append-only durable events — user turns, narrations, Q&A, steers,
   publishes, deliveries, failures — walking the parent chain for forks. It is **never
   persisted** (deterministic per (events ≤ boundary seq, budget)); it reaches the planner
   inline (`# The conversation so far`, ~12k tokens), every executor as read-only
   `/task/thread.md` (~16k tokens, rendered to the planning boundary so retries read
   byte-identical history), and the context ledger as a thread-lane receipt carrying the real
   token spend. Framed like `context.md`: instruction, never a citable source. The old
   `_thread_lane` précis is deleted.
4. **Fork is the only branching action.** Still one click, still born held — now inheriting
   the parent's dials (budget cap, fleet seats, deliverable, skill pins; `inherit_mode` skips
   flag re-checks the parent already passed) and, via the transcript's chain walk, the
   parent's entire conversation. Cancel clears a held fork's hold; `[Forked]` prefixes never
   stack.
5. **Materials continue.** Turn N mounts the task's own prior-turn deliverables
   (lineage-latest, cut at the turn boundary) read-only; a re-published filename chains as
   the next revision of the thread's lineage and emits `artifact.revised`. The boundary cut
   is what keeps two fleet seats publishing the same name in one turn from reading each other
   as "prior".
6. **The web stays put.** The composer's follow-up paints an optimistic bubble, POSTs the
   same `/follow_up` route (now honest), and never navigates; the SSE socket is never closed
   on a terminal frame — a settled task is a conversation at rest. The current plan rolls
   into the rail's history when the next turn opens.

## Consequences

- One task = one conversation = one list row = one ledger. Spend accrues across turns against
  one budget; each turn delivers, notifies, and runs the archivist once (per-turn guard).
- `POST /tasks/{id}/follow_up` returns the **same** `task_id` (plus `turn` and `last_seq` for
  the CLI's replay cursor). A live task answers 409 "steer it instead"; the `client_token`
  replay guard moved from the tasks table to the `task.continued` event.
- Steers are scoped to the turn they were given in (payload `turn`); the transcript still
  renders every one.
- Legacy parent chains (pre-pivot follow-up children) stay untouched — append-only history is
  not rewritten; the ancestor walk still renders them and the transcript still climbs them
  (hop-capped).
- The clarifier and namer run on turn 1 only: the thread is the clarification, the title
  exists.

## Rejected

- **Child tasks with a fixed UX** (thread identity server-side over child rows): keeps
  "delivered is sealed" but leaves every conversation scattered across N rows — N list
  entries, N budgets, N ledgers for what the user thinks of as one thing, and a join for
  every read. The row model should match the mental model.
- **Persisting the model's message history** for resumption: pydantic-ai transcripts are
  per-attempt state (`plan_steps.resume_state`, deleted on use); a durable copy would drift
  from the record and double the storage of what the event log already proves. The rendered
  transcript is honest, replayable, and cache-friendly (append-only prefix).

**Amended 2026-11-15 (ADR-092, the planner, clarifier and router deleted):** A follow-up's turn is the Lead's, which reads the whole thread and asks for itself; there is no intake round on any turn of a Lead-decided task, and a recipe turn (a dial) asks nothing either. The namer still runs on turn 1 only.
