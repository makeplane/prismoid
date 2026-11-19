# ADR-101 · Logs on stdout are JSON records; spans leave only by OTLP

**Status:** accepted · 2026-11-18

## Context

Logfire's console exporter is on by default and prints every span at `info`: each asyncpg
round-trip (`BEGIN;`, `;`, `ROLLBACK;`, the statement), each request, each job, as indented text.
On a production deployment the API wrote about 6,400 lines an hour and the worker about
6,800; 55-57 % were bare transaction statements, about 21 % SQL, and the rest span names, mostly
the load balancer's `/healthz` every few seconds. Not one line was an application event. A log
pipeline (Datadog) cannot parse the format, has no level, no trace id, and pays for every line.
The spans themselves are already exported by OTLP (`OTEL_EXPORTER_OTLP_ENDPOINT`), where a trace
backend shows them with timings.

## Decision

1. **`PRISMOID_LOG_FORMAT=json`** (the default with `PRISMOID_ENV=staging` or `prod`): Logfire's
   console is off; spans leave only by OTLP. stdout carries log records only, one JSON object per
   line (`prismoid/stdout_log.py`), written by **python-json-logger** on the root handler - the
   formatter Plane's Python services already use. stdlib
   `logging` reaches it directly; Logfire's log events (`logfire.info(...)`, a zero-length span with
   `logfire.span_type = log`) reach it through a span processor (`LogfireRecords`) that turns each
   into a stdlib record and ignores every other span. That bridge is the only Prismoid-specific piece.
2. **Trace correlation** is the usual trace-context filter pattern: `trace_id`/`span_id` (hex) of the span a record was
   written in, plus Datadog's `dd.trace_id`/`dd.span_id` (the trace id's low 64 bits and the span
   id, in decimal) and `dd.service`/`dd.env`/`dd.version`, ddtrace's names, so Datadog links a log
   to its trace. The line's other names: `timestamp` (ISO 8601, UTC), `status` (the level name),
   `logger.name`, `message`, the caller's attributes, and `error.kind`/`error.message`/`error.stack`
   for an exception. A Logfire event is attributed to the span it was written in.
3. **Scrubbed** in the same filter: every value matching `SCRUB_PATTERNS`, every query-string
   credential (`redact_query_credentials`) and every string under a secret-named key; Logfire has
   already scrubbed its events before any processor sees them.
4. **One floor**, `PRISMOID_LOG_LEVEL` (`info` by default) for `prismoid.*`, uvicorn, the durable
   runtime and Logfire events; any other library keeps the stdlib default (`warning`), so an
   outbound HTTP client's per-request `info` stays out. Loggers that print on their own (uvicorn's,
   the durable runtime's console handler) hand their records to the JSON handler instead; Python
   warnings become records.
5. **The probe is silent**: `/healthz` is excluded from the ASGI server span
   (`instrument_fastapi(excluded_urls=...)`) and runs with instrumentation suppressed
   (`middleware.QuietProbe`), so it writes no span, its SQL included. `/healthz/full` is traced.
6. `PRISMOID_LOG_FORMAT=console` keeps the old behaviour (every span on the console), the default
   with `dev`, `test` and `local`.

## Alternatives rejected

- **Only raising Logfire's console floor** (`LOGFIRE_CONSOLE_MIN_LOG_LEVEL=warn`): removes the noise
  but leaves an unparseable text format, and drops `info` events from stdout altogether.
- **Logfire's stdlib bridge** (`LogfireLoggingHandler`): turns stdlib records into spans, the
  opposite direction; stdout would still need a formatter.
- **A JSON writer of our own** (the first cut of this ADR, ~300 lines): formatting, timestamps,
  levels and exception fields are what python-json-logger already does, and every other Plane
  Python service uses it; owning a copy buys nothing. `structlog` is used by no Plane service.
- **`opentelemetry-instrumentation-logging`** for the trace ids: a twenty-line filter does the same
  with no new dependency and also carries Datadog's decimal ids.

## Consequences

- stdout in staging and prod is a few lines an hour of real events instead of thousands of span
  dumps; a trace backend reached by OTLP holds the spans.
- `logfire.info(...)` events are now visible in the log pipeline, correlated with their trace.
- A developer's laptop is unchanged.
