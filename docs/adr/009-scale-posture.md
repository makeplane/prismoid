# ADR-009 — Scale posture: what scales today, what waits for a trigger

*Status: accepted · July 2026 · companion to the post-m10 architecture audit*

## Context

The 2026-07 architecture audit asked whether the system scales "in any direction." The answer
was: far, in most directions, because the seams were cut in advance — but six frontiers needed
either code or a recorded decision with a trigger. This ADR is that record. The principle it
inherits: **boring beats clever, and a seam beats a rewrite** — nothing here is speculative
infrastructure; everything is either landed now or gated on an observable trigger.

## What landed with this ADR

- **Replica-aware throttles.** Both in-process limiters (the per-actor backstop in `api/deps.py`
  and the login throttle in `operations/auth.py`) divide their limits by
  `COMPUTER_API_REPLICA_COUNT` (default 1), so the fleet-wide ceiling stays the configured
  number behind an evenly-distributing load balancer. They remain what they honestly are —
  floors against runaways, not precision limiters; precision moves to the ingress when one
  exists (see the runbook).
- **A worker executor id.** `COMPUTER_WORKER_EXECUTOR_ID` flows into `DBOSConfig(executor_id=…)`;
  empty keeps DBOS's single-instance default. Queue concurrency caps
  (`Queue(concurrency=…)`) are **global across executors** in DBOS — verified against the
  installed package — so adding workers never multiplies in-flight work.
- **The ghost sweep's detection invariant** has a standing test
  (`tests/integration/test_sweeper.py`); `tools/make_ghost.py` remains the live-stack instrument.
- **The web service joined the gate**: eslint + vitest (reducer contract tests) run in
  `just check` and therefore in CI.
- **`docs/runbooks/ingress.md`** now exists (it was referenced but never written): the SSE
  deployment rules plus the query-string scrubbing requirement (`?token=` must never reach
  ingress access logs).

## The gated decisions — triggers, not dates

| Frontier | Decision | Trigger to act |
|---|---|---|
| **N workers** | Allowed only after the two-worker chaos drill passes: two workers with distinct `COMPUTER_WORKER_EXECUTOR_ID`s, kill one mid-task, require adoption and exactly-once completion (`just chaos` with a second worker up). | The first time one worker's step throughput or frame capacity saturates. |
| **Frame pool** | Frames stay on the worker's Docker daemon. The driver interface (`ai/frames/driver.py`) is the seam; a remote pool (or gVisor/Firecracker fleet) replaces the driver, not the callers. | Frame cold-start p95 breaches §15 (<2 s) under normal load, or worker host CPU/RAM saturates. |
| **Connection ceiling** | pgbouncer (transaction pooling) in front of Postgres. | Third API replica (64 conns each; Postgres default ceiling ~100). |
| **`task_events` retention** | Native monthly range partitions; partitions older than 12 months detach and archive to object storage. Append-only and keyset pagination make this mechanical. | 50 M rows, or the first nightly perf-gate breach on a list/replay budget — whichever first. |
| **Live-lane bus** | Postgres NOTIFY stays. The hub (`api/hub.py`) is the documented seam: a dedicated pub/sub (NATS/Redis) replaces the guts of that file and no emitter changes. | Sustained NOTIFY saturation: hub delivery lag visible against the ≤250 ms append→delivery budget at peak. |
| **Deploy target** | Compose remains the deploy unit through beta. First external deployment gets a real target: managed Postgres, S3-compatible store, one container host; secrets leave `.env` for the platform's secret store. | The first user who is not the owner. |

## Consequences

- Scaling the API tier is now a config change (`REPLICA_COUNT`) plus an ingress; scaling the
  worker tier is a drill plus an env var — in both cases the code is already correct or the
  gap is named.
- Every trigger above is observable in the existing instrumentation (perf gate, Logfire spans,
  compose stats). Nobody has to remember this document; the alarms point back at it.
- What deliberately did **not** happen: no Redis, no k8s manifests, no partitioning migration,
  no second bus — §14's "boring beats clever" applies to infrastructure twice as hard as to
  code. Each waits for its trigger, behind its seam.
