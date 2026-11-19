# ADR-072 · httpx2 for the provider wire

**Status:** accepted · 2026-10-29 · forced by the Dependabot pass of 2026-10-29: every
`pydantic-ai-slim` advisory (GHSA, medium to high) is fixed only from 2.52.0. **Amends** ADR-024
(the retrying transport keeps its policy and changes its HTTP library).

## Context

`pydantic-ai-slim` ≥ 2.52 requires `openai` ≥ 3.19, `anthropic` ≥ 1.9 and `google-genai` ≥ 2.25.
Those SDK lines, and pydantic-ai's own retrying transport, moved from `httpx` to `httpx2`, pydantic's
maintained successor fork (BSD-3-Clause, same API). The `httpx` transport,
`AsyncTenacityTransport`, still runs, but it is deprecated and slated for removal in pydantic-ai v3.
The SDKs accept a legacy `httpx` client only through a compatibility shim, and their types name
`httpx2`, so pyright strict rejected the gateway's shared client.

## Decision

1. **The gateway's shared per-provider client is an `httpx2.AsyncClient`** (`ai/gateway/throttle.py`)
   wrapped in `AsyncHTTPX2TenacityTransport`. ADR-024's retry policy is unchanged: 429/502/503/504,
   `Retry-After` honoured, three attempts, the transport as the only retry authority. The
   constructors in `ai/gateway/providers.py` take `httpx2.AsyncClient`. Everything else (the CLI,
   tools, connectors, `web.fetch`) stays on `httpx`.
2. **`httpx2` is declared** in `pyproject.toml` (it was already locked transitively; the
   declaration makes it honest, as ADR-024 did for tenacity) and has a HANDOFF §3 row.
3. **The bump stays as small as possible:** `pydantic-ai-slim`/`pydantic-evals` 2.52.x,
   `anthropic` 1.x (not 2.x), `openai` 3.x (the minimum pydantic-ai accepts), `mcp` 1.30 (not
   2.x; `mcp.shared.auth` is used directly, ADR-020), `fastmcp-slim` 3.4.x. `logfire` goes to 5,
   because 4.x caps OpenTelemetry below the patched 1.45.1. Its global `instrument_httpx()` also
   instruments `httpx2`, so provider calls keep their spans and the Telegram URL redaction.
4. **The forced-choice heal now uses the neutral profile key** (`ModelProfile(supports_forced_tool_choice=False)`);
   `anthropic_supports_forced_tool_choice` is deprecated.

## Consequences

- pydantic-ai 2.52 answers typed output on models with native structured output (the Claude 5.5
  rows) through `output_config.format: json_schema`, not a forced tool call. The forced-choice
  refusal that `_ForcedChoiceFallback` heals cannot happen on those rows. The heal stays for
  tool-output models (Haiku 4.5), and its test asserts both paths.
- A run that dies mid-tool-call now records a trailing empty `ModelRequest(state="interrupted")`.
  A retry takes such a run up on its call log (ADR-089), where the tool record answers the cut call.
- `anthropic` ≥ 1 refuses a non-streaming call whose `max_tokens` could run past ten minutes when
  the client is on the SDK's default timeout. The shared client sets its own timeout (600 s, connect
  10 s), so production never meets that refusal. Test fakes must do the same.
