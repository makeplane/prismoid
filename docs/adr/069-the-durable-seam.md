# ADR-069 · The durable seam

**Status:** accepted · 2026-10-29 · the owner's ask of 2026-10-29 ("centralise DBOS to plan its
removal"), first step of the planned port (DART
replaces DBOS, decided 2026-09-26). **Amends** nothing; HANDOFF §1's Job/Effect rows and §3's
`dbos` row now name the seam.

## Context

DBOS was imported by 17 files under `prismoid/` (16 in `jobs/`, plus `runner/worker.py`) and 3 tests,
several through private paths (`dbos._utils`, `dbos._error`, `dbos._croniter`, `dbos._context`). Every job body
spelled the engine: `@DBOS.workflow`, `@DBOS.step`, `@DBOS.scheduled`, `DBOS.recv_async`,
`SetWorkflowID` around `queue.enqueue_async`, `DBOS.list_workflows_async`, `WorkflowStatus` rows.
`operations/` was already clean (the Dispatcher port, ADR-001 seam 1); the engine leaked everywhere
below it. A port to DART touching every job body at once, under live sessions, is the risk the
program spec names.

## Decision

1. **One module is the durable runtime: `prismoid/jobs/durable.py`.** It is the only file in `prismoid/`
   that imports `dbos`; `tools/audit.sh` fails the build on any other import ("dbos is imported
   only by jobs/durable.py"). Tests reach the engine through it too; `tests/unit/test_durable.py` is
   the one test file that may spell DBOS (it pins the translations).
2. **Engine-neutral verbs**, each a direct DBOS call today:

   | Verb | Means | DBOS today | DART (§4.0) |
   |---|---|---|---|
   | `job(name)` | a durable process, deterministic id | `@DBOS.workflow(name=)` | `@handler` on a topic |
   | `effect(attempts=, interval_s=, backoff=, retry_if=)` | journaled I/O | `@DBOS.step(...)` | `job.step(key, fn)` |
   | `schedule(cron)` / `next_fire` | a clock starts the job | `@DBOS.scheduled` / its croniter | DART schedules |
   | `queue(name, concurrency, poll_s=)` | a lane, cap global across workers | `dbos.Queue` | topic + per-topic cap |
   | `start` / `start_sync` / `join` / `cancel` | a child job | `SetWorkflowID` + `enqueue[_async]`, `get_result`, `cancel_workflow_async` | enqueue child, `children_summary`, cancel |
   | `wait(topic, timeout_s)` | park until signalled | `DBOS.recv_async` | `job.sleep()` + `wake` + signals mailbox |
   | `sleep(seconds)` | durable timer | `DBOS.sleep_async` | `job.sleep()` |
   | `signal(key, payload, topic)` | wake a parked job | `DBOS.send_async` | `wake` + `signal` row |
   | `runs` / `runs_sync` → `Run` | live jobs (queued/running/delayed) | `list_workflows[_async]` → `WorkflowStatus` | executors table + job queries |
   | `forget_sync` / `cancel_sync` / `version()` | the boot ghost sweep (ADR-027) | `delete_workflow`, `cancel_workflow`, `GlobalParams.app_version` | job delete / cancel; DART pins no version |
   | `Client` | the API's enqueue-and-signal handle (Rule 5) | `DBOSClient` | a DART client |
   | `init` / `launch` / `shutdown` | the worker's runtime | `DBOS(config)`, `launch`, `destroy` | DART worker start / drain |
   | `NoSuchJob`, `ReplayMismatch` | the two engine errors the code names | `dbos.error.*` (public module) | DART's equivalents |

3. **Thin, and journal-identical.** No step, workflow, queue or recorded call changed its name, and
   each verb calls the engine exactly as the job bodies did - so a workflow journalled before this
   change recovers after it. Proven live 2026-10-29: a task journalled by main's worker (run_task
   joined on a run_step parked in a paced `DBOS.sleep`) was killed mid-park, recovered by this
   branch's worker on the same database, executor and version, and finished SUCCESS with the
   branch appending to main's journals and no replay mismatch. Three deliberate differences: the
   listings pass `load_output=False`; the heartbeat's listing no longer loads inputs it never read;
   and a row whose input the engine cannot read (it hands back the raw string) lists with unknown
   inputs instead of failing the whole listing - the boot ghost sweep reads old releases' rows.
4. **The seam's own parameters are positional-only** (`start`, `start_sync`, `Client.enqueue`): a
   job's keyword arguments pass through untouched, whatever they are called. Two jobs take a
   `version` (`test_bot`, `machine_egress`); the first draft's keyword `version` collided with them
   in the API's enqueue (found in review, pinned by `test_durable.py`).
5. **Where DBOS semantics show through, the docstring says so** - `wait` holds its queue slot (the
   named defect DART fixes), a finished `job` id never re-runs, `effect` defaults to one attempt,
   a missed `schedule` tick is never backfilled. Those are the contract a DART implementation keeps,
   or its ADR says it does not.

## Consequences

- The DART port becomes `jobs/durable.py`, its unit test, and the replay-shaped tests
  (`test_pause_park.py`'s `_Journal` models DBOS's replay; DART has none) - not 20 files.
- Two places still carry DBOS-shaped assumptions above the seam, by design, and move with the port:
  the pause-park hand-back counter (`execution.HANDBACKS`, which exists because DBOS replays a
  body) and the ADR-027 version pinning (`PRISMOID_WORKER_APP_VERSION`, moot without replay).
- Comments in the job modules that describe DBOS behaviour (replay, dedupe, slot-holding) stay true
  while DBOS is the engine; the port rewrites them with the behaviour.
- The audit check never shrinks (its own law); the port changes only the file name it excludes.

## Amendment 2026-11-14 - the call log beside the seam (ADR-089)

A step attempt's model and tool calls are durable on the call log (`call_log`, ADR-089), not as engine
effects: the attempt still runs in `run_step`'s body, and a replayed body re-enters the log.
