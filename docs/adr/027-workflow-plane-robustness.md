# ADR-027 — Workflow-plane robustness: ghosts, recovery, waves, and the money they burned

**Status:** accepted · 2026-08-09
**Context:** the first live long-form run (ADR-025's microtubule proof) doubled as a stress
test of the workflow plane and found it wanting: of the $8.30 the run cost, roughly $5–6 was
burned by plane defects, not by the document. The user's bar — *extremely robust and durable,
no unwanted token spend, fast for the user* — is this ADR's spec. Seven flaws, seven fixes,
each carrying the live failure that earned it.

## 1 · Application version is pinned; ghosts are swept at boot

DBOS gates recovery on an application version that defaults to a code hash, so **every restart
on changed code orphaned all in-flight workflows** — never adopted, never cancelled, and their
ENQUEUED/PENDING rows counting against **global** queue concurrency forever. Eight such ghosts
from dead sessions ate the entire steps width and froze all dispatch, silently; the only
witness was hand-typed SQL against DBOS's system tables.

Now: `COMPUTER_WORKER_APP_VERSION` (default `"dev"`) pins the version, so dev restarts ADOPT
in-flight work — safe here by construction, because workflow bodies re-execute from the DB
state machine (attempt counters, step states), not from code-shape-sensitive journals.
Production sets the release sha per deploy. And the worker boot runs `_sweep_version_ghosts()`
— the `reap_orphans` law applied to workflows: ENQUEUED/PENDING rows from any OTHER version
are unrecoverable by construction and are cancelled on sight.

**Amended 2026-10-04 (ADR-062's fence).** The API's enqueues carry the version too:
`ClientDispatcher` stamps `COMPUTER_WORKER_APP_VERSION`, so a turn runs only on a worker of the
release that started it. An unstamped row went to a worker of ANY release, and one that predated
the memory fence ran Space turns unfenced (found live in 1a). Two consequences. The API and its
workers must read the same value: the worker's beat names its version and the API warns at boot
when a live worker's differs (`operations/health.version_skew`). And the old API's queued turn is
now an old-version ghost to the new worker, so the boot sweep DELETES an ENQUEUED ghost (it never
started, and a cancelled row would squat its deterministic id - the sweeper's re-enqueue would
dedupe onto the tombstone) and cancels only PENDING ones; the sweeper then re-enqueues the task
on the new version. A rolling deploy (sweep off) drains the old version's queued work before it
retires the old workers (deploy/DEPLOY.md). **Amended 2026-10-06:** the stamp made every queued
API job a ghost at the next release, and only turns had a sweeper behind them - a line sent to a
bot while a deploy had the worker down got no reply, ever. The boot sweep now MOVES a never-started
ghost: deleted, then enqueued again on the new version under its own id with its own inputs
(`jobs/dispatcher.requeue`, any registered job); the task sweeper stays the backstop for turns.

## 2 · A parked question survives a worker death

Recovery re-runs the step workflow's body; the recv is gone, so the old code landed in the
blind "recovered" branch — the question silently dropped, an attempt burned, the task stranded
`waiting`, and delivery then died on the illegal `waiting → delivered` edge, leaving a
finished, paid-for 47-page document undeliverable without `fork_workflow` heroics.

Now the **gate row is consulted as the durable truth**: still OPEN → the step re-parks (the
answer arrives normally); RESOLVED-but-unconsumed → `resume_state` is hydrated from the gate's
recorded resolution and the same attempt continues as if the recv had returned. (Amended
2026-10-05, found live with a pause, and 2026-10-06: the row keeps the step workflow's hand-backs
in the order it took them - each park's gate and each pace's deadline, `resume_state.handbacks` -
and how many wakes it stored. A replay of `run_step` counts its own hand-backs; while the count is
behind the record the ladder re-issues the next one instead of running, so every recorded recv and
sleep hands its own result back in the journal's order, a wake already stored is not stored again,
and only the call after the last runs: the resume if its dump is still on the row, else a fresh
attempt on the infra rung. A run that parked, was resumed and was then paced replays recv, then
sleep - read from the step's state, it re-paced first and DBOS refused the replay.) Three belts
behind it: the recovered branch un-parks a WAITING task that holds no open gate; `deliver`
walks the legal `waiting → running` edge itself when every step is settled and every gate
closed; and the sweeper's `_deliver_stranded` delivers any quiet all-settled WAITING task with
the summary rebuilt from the turn's durable narrations — so even a dead parent workflow can no
longer hold a finished deliverable hostage. (Residual, accepted: a stale unconsumed wake
message can mis-pair with a later gate of the same step after such a crash — the pre-existing
duplicate-send edge, unchanged.)

## 3 · A failed wave stops its siblings

`dispatch_frontier` awaited handles one by one: the first child failure aborted the parent
into `fail_task` while every later child **kept executing — billing model calls onto a task
already marked failed**. The wave now waits FIRST_EXCEPTION and cancels outstanding siblings
(`DBOS.cancel_workflow_async`; the executor's terminal check ejects them at the door) before
the failure propagates. Orphaned spend on a dead task was the plane's single worst money leak.

## 4 · Recoveries are not retries

`attempt` counts every sandbox ever built (its number rides the frame name, which must stay
unique across crashes) — but the retry caps now read `model_attempts() = attempt −
infra_attempts` (`plan_steps.infra_attempts`, migration `d5e7f9a1b3c4`), and the recovered
branch increments the infra side. A worker restart, a reaped parked frame, a wall-clock stop —
none of them spend the user's ladder, verify, or QA budget anymore. In the live run this
compounding cost the QA gate its whole rebuild budget before QA ever ran.

**Amended 2026-11-06: the weather is infra, and infra is capped.** Two more deaths join
the infra side: a provider death (`ModelIncapable` with `provider`, unless the provider `refused`
the request - a 400/413 stays the model's) and a tool whose transport stopped answering mid-run (a
`ConnectionError`: `FrameUnreachable`, the device helper, a connector's server), which before this
escaped the ladder and failed the whole task. The transport first retries the same model on
429/500/502/503/504/529. The infra deaths the ladder itself caught are counted apart
(`plan_steps.infra_failures`, migration `infra_failures`; a worker restart and a steer's re-run stay
out) and capped by `PRISMOID_WORKER_MAX_INFRA_ATTEMPTS` (3 retries, then the step escalates with its
diagnosis), so a wall-clock stop no longer repeats every half hour until the budget gate. A provider
death waits before the next attempt on the workflow's durable clock (a `pace` hand-back:
`PRISMOID_WORKER_PROVIDER_BACKOFF_S`, doubling to `PROVIDER_BACKOFF_MAX_S`), and the gotchas and a
guarded run's conversation ride the row across the wait (`parks.back_off`). The booking is the pure
`operations/execution/infra.py`.

The run guard leaves the computer alone. Before, only the computer was guarded, so a provider blip or
a sandbox hiccup threw away a writer's, a coder's or a researcher's whole conversation, and its next
attempt started from nothing. Now every asking step agent with hands (a frame or web tool) is guarded,
and so are the scatter map (the researcher's map mode, ADR-091) and every bot holding a frame or web tool. Guarded means three things: the
guard's brakes and a retry that takes up its run on the call log (ADR-089). The law reads
`looping <= guarded <= hands` (`tests/laws/test_agent_registry.py`, which replaced "only the computer
is guarded"). A chat reply, a gather seat and the section writer stay unguarded. A gather seat's retry
moves a wide wave's seat down its chain, and the section writer holds only `read_file`. Only the
computer is told to call `done`. A guarded agent that ends with its answer is told to finish with what
it has. It may still `publish` what it built, since that is how it hands work over.

## 5 · Sections hold their budgets

The nine writers overshot 9,000 target words to 12,731 (47 pages instead of 30) — the wave's
overshoot is additive, and the QA gate rightly rejected it. The section goal now names the
hard ceiling (1.15× target, cut before closing) above the acceptance band, with the live
failure quoted at the writers.

## 6 · Width matches the lane

`COMPUTER_WORKER_STEP_CONCURRENCY=12` in dev (`.env.example` documents 8 for small hosts, 16
at ≥48 GB). The ninth section of a nine-wide wave no longer waits a full slot-turn behind an
eight-wide cap.

## 7 · A frozen queue is a field, not a forensic session

Two bounds and one instrument. The step wall clock: `COMPUTER_WORKER_AGENT_WALL_CLOCK_S`
(default 1800) bounds the whole agent run — CLASS_TIMEOUT_S bounds one request; nothing
bounded a wedged tool loop holding a queue slot forever — and a timeout is an infra-flavoured
rung, never a dead task. The instrument: the worker's minute pulse now carries queue vitals
(`system_beats.info`, migration `e6f8a0b2c4d5` — enqueued/running/oldest-wait per queue, ghost
count, clog verdict at 120 s), `/healthz` translates it (`checks.queues: "ok" | "CLOGGED: …"`),
and a clog logs loudly from the pulse itself, because the beat may be the only process that
ever looks.

## Verified

`just check` green · 685 backend unit + schema-parity integration green · contract regenerated
(healthz gains `queues`) · migrations round-trip · live: worker boots pinned (`Application
version: dev`), width 12, ghost count 0, healthz `queues: ok` from the first scheduled pulse.
The definitive live drill — kill the worker mid-wave with a parked question and watch the run
deliver without hands — is named in STATE as the plane's standing chaos exercise.
