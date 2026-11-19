# Operations dashboard — the queries, and what they mean

Five numbers tell you whether the system is healthy: is it **autonomous** (IPT), is it **trustworthy**
(verification coverage), is it **working** (the outcome funnel), is it **affordable** (spend), and is
the **retry ladder** doing its job. Each is one SQL query against the append-only ledger — `task_events`
and `usage_events` — so every number here is re-derivable, not a cache that can drift. The numbers below
are live, from this instance's ledger; run the query to refresh them.

> Connect: `docker exec -it prismoid-postgres-1 psql -U prismoid -d prismoid`

---

## 1. IPT(A) — Interventions Per Task (the autonomy number)

**Unplanned human touches per task; designed gates excluded.** Below 0.5 is the target — fewer than
one unplanned touch for every two tasks.

```sql
SELECT round(
  (SELECT count(*) FROM task_events WHERE type = 'escalation.raised')::numeric
  / NULLIF((SELECT count(DISTINCT task_id) FROM task_events), 0), 4) AS ipt_a;
```

Live: **`0.1579`** — 39 unplanned escalations across 247 tasks. Designed gates (`gate.opened`,
`task.clarifying`) are *excluded*: choosing to ask a sharp question is autonomy, not intervention.
Reconciles with the M7.2 evaluator (`evals/evaluators.py::ipt`), which is this same arithmetic.

## 2. Verification coverage — the trust number

**What fraction of delivered tasks carry a signed, cross-family verdict.** The rest are
UNVERIFIED-ships (the deterministic floor let them pass; the judge added nothing worse) — by design,
not a hole.

```sql
WITH delivered AS (SELECT DISTINCT task_id FROM task_events WHERE type = 'task.delivered'),
     signed    AS (SELECT DISTINCT task_id FROM task_events WHERE type = 'verification.signed')
SELECT count(*) AS delivered,
       count(*) FILTER (WHERE task_id IN (SELECT task_id FROM signed)) AS delivered_and_signed,
       round(100.0 * count(*) FILTER (WHERE task_id IN (SELECT task_id FROM signed))
             / NULLIF(count(*), 0), 1) AS coverage_pct
FROM delivered;
```

Live: **`130` delivered · `116` signed · `89.2%` coverage.** The 14 unsigned are unverified-ships.

## 3. The outcome funnel — the working number

```sql
SELECT status, count(*) FROM tasks GROUP BY status ORDER BY count(*) DESC;
```

Live: `delivered 130 · failed 96 · planning 11 · clarifying 6 · waiting 2 · cancelled 1`. The failures
are a build's accumulated experiments (deliberate fabrications, weak-judge runs, chaos/endurance kills);
on the golden suite the rate is the M7.2 number, not this raw ledger.

## 4. Spend by family — the affordability number (reconciles to the cent)

```sql
SELECT family, round(sum(cost_usd), 4) AS usd, count(*) AS calls
FROM usage_events GROUP BY family ORDER BY sum(cost_usd) DESC;
```

Live: `anthropic $14.41 · qwen $0.0052 · llama $0.0001`. Priced at write time from the price book in
force (Rule 8); the per-family sum equals the grand total exactly (the M6.3 reconciliation).

## 5. The retry ladder — is verification doing its job

```sql
SELECT (payload->>'attempt')::int AS attempt, count(DISTINCT task_id) AS tasks
FROM task_events WHERE type = 'step.state' AND payload ? 'attempt'
GROUP BY 1 ORDER BY 1;
```

Live: most steps land on attempt 1; a meaningful tail reach attempt 2–3 (the M2.4 targeted-retry ladder
firing on a FAIL verdict). A handful beyond 3 are multi-step tasks accumulating attempts across steps —
worth an eye, not an alarm.

---

## Alerts — what to page on (M9.6)

Each dashboard number has a threshold that means "look now." Wire these against the metrics above (or the
Logfire spans that feed them); the number in parentheses is the current live value, for calibration.

| alert | condition | why it pages |
|---|---|---|
| **autonomy regressed** | IPT(A) > 0.5 (now 0.16) | the system started needing a human every other task — something is asking too much |
| **verification coverage dropped** | signed/delivered < 90% (now 89%*) | deliverables are shipping unverified more often than designed |
| **error/failure spike** | task `failed` rate over 1 h > baseline × 2 | a provider outage, a bad deploy, or a poisoned input |
| **budget breaches climbing** | `escalation.raised` rate over 1 h > baseline × 2 | tasks are hitting budget walls — a pricing change or a runaway plan |
| **latency regressed** | any §15 p95 over budget for 5 min | the perf gate (M8.5) catches it nightly; this catches it in the moment |
| **spend anomaly** | hourly `sum(cost_usd)` > 3× the trailing-24 h hourly mean | a stuck retry loop or an accidental fleet-wide route to a frontier model |

\* the coverage floor is a *design* line, not a bug — the alert is for a *drop below* it, not the gap
itself (UNVERIFIED-ships are intended). Tune the multipliers to your traffic; a good alert fires on the
day something breaks and stays silent every other day.

## Observability plumbing (M9.6)

One instrumentation story: the app emits OpenTelemetry spans through Logfire. **OTLP dual-export is a
single env var** — set `OTEL_EXPORTER_OTLP_ENDPOINT` (and `OTEL_EXPORTER_OTLP_PROTOCOL`) and the same
spans stream to your own collector *in addition to* Logfire, so telemetry sovereignty needs no second
SDK. Verified live: with the env var pointed at a local `otel/opentelemetry-collector-contrib`, an
app-emitted span (`m9.6-dual-export-probe`) arrived in the second collector's output.

## Reconciliation — why these can be trusted

Every number is a `GROUP BY`/`count` over an **append-only** table (`task_events`, `usage_events` never
`UPDATE`), so the dashboard cannot disagree with the system: it *is* the system's own record, aggregated.
IPT here equals `evaluators.ipt`; the spend equals the `/tasks/{id}/usage` endpoint summed; coverage
counts the same `verification.signed` frames the CLI shows in `prismoid tasks events`. A number that
looks wrong is a query to re-run, not a cache to bust.
