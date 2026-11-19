# jobs - folder rules
Scope: `backend/prismoid/jobs/`. Read with the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The machine processes. A job is a durable background process (`@durable.job(name)`); an effect
is journaled I/O inside one (`@durable.effect`). `all.py` is SCREEN 3: every job, every queue,
every schedule, readable top to bottom. `durable.py` is the one door to the durable engine
(ADR-069). A job body composes operations; it is NOT where business rules live (that is
`operations/`), and a Task is what the user asked for, not a job.

## Do
- Declare a job with `@durable.job(name)` and its I/O with `@durable.effect`, both from
  `prismoid.jobs.durable`; schedules and queues go through the same module.
- Register every job in `all.py` under the name its own file gives it.
- Keep I/O in effects, not in the job body (HANDOFF §2 rule 5). A deliberate exception says why
  in the docstring, as `gate_sweeper.py` does (a sweep must read the current set, not a replay).
- A job id is deterministic and built by a `*_job_key` builder in `operations/dispatch.py`
  (`task-{id}`, the per-turn `task-{id}-t{n}`); enqueue happens after the operation commits.
- Treat every `@durable.job` and `@durable.effect` name, and every sentinel a workflow returns,
  as frozen: DBOS journals them, and a replayed workflow must see the same names in the same
  order (ADR-069).
- Extract a long workflow body into plain async helpers that call the same effects in the same
  order; a new effect inserted between existing ones changes replay.
- Routines (ADR-048): the sweep is read-only and dumb, two arms (due, parked drain); no
  workflow ever sleeps on a queue slot; the tick-scoped workflow id
  `routine-{id}-{occurrence}~{tick}` dedupes within a tick.
- Verification runs on its own queue after delivery and never blocks the thread (ADR-019),
  except fleet, which keeps in-line verify (ADR-010).

## Never
- Import `dbos` anywhere but `durable.py`.
- Import a job under another name in `all.py` (`execute_step as run_step` is the one left,
  planned to be renamed); Screen 3 must read true.
- Build a new job id with an ad hoc f-string at the call site (the existing literals move
  to builders in a planned refactor).
- Place a new effect anywhere but `jobs/effects.py` while the effects-home decision is open.
  Effects live in `jobs/effects.py` per HANDOFF §1; three modules (`machines.py`, `verify.py`,
  `bot_studio.py`) already keep effects beside their job; which convention wins is an open owner
  decision, and until then new effects follow HANDOFF §1.
- Branch on a string verdict where a `StrEnum` would do (`run_task` nests five deep on strings
  today).

## Checked by machine
- `backend/tools/audit.sh`: "dbos is imported only by jobs/durable.py" (ADR-069).
- `tests/laws/test_law_transports_in_api.py`: no server framework under `jobs/`.
- `.importlinter`: the layers contract; `jobs` sits under `api`, above `operations`.
- `quality:py.complexity`, `quality:py.nesting`, `quality:py.positional`,
  `quality:comments.process_refs`. Baselined here: `run_task` (complexity 24, nesting 5),
  `effects.execute_step`, `channels.sweep_channels`, `machines.machine_sleep` (8 positional).
  A planned refactor flattens `run_task`; new jobs meet the caps.

## Write it like this
- `durable.py`: a deep module with a small, named surface; its docstring states the contract a
  replacement engine must keep, so the DART port is this file.
- `gate_sweeper.py`: the minimal scheduled job. It opens the session, makes one operation call,
  and justifies its Rule 5 exception in the docstring.
- `all.py`: one import line and one roster line per job.

## Review focus
- A replay hazard: a renamed job or effect, a reordered effect, a changed sentinel, a value
  read from the clock or the database in the workflow body instead of an effect.
- A job body that grew business logic (a branch on task state, a policy decision): it belongs in
  an operation the job calls.
- An enqueue before the operation commits, or a job id that is not deterministic.
- A new job missing from `all.py`, or registered under a renamed import.
- A routine change that lets a fire or non-fire go unjournaled, or a sweep that writes.
