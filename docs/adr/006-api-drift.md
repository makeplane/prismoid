# ADR-006 — Library-vs-sketch API drift

**Status:** living document · **Context:** HANDOFF §0, §15 — *"Library-vs-sketch drift → follow
the library, keep the intent, log in `docs/adr/006-api-drift.md`."*

The brief was written against an earlier generation of these libraries. Where the installed
version disagrees with the sketch, **the library wins and the intent is preserved**. Every such
divergence is recorded here, with the intent it was serving and how that intent is now met.

---

## 001 — `ORJSONResponse` is deprecated; FastAPI's default path is faster (M0.5)

**Sketch (§15, practices):** *"ORJSONResponse"* as a performance practice; §5.2's app sets it as
the default response class.

**Library (fastapi 0.139):** `fastapi.responses.ORJSONResponse` is deprecated. The deprecation
message says why: *"FastAPI now serializes data directly to JSON bytes via Pydantic when a return
type or response model is set, which is faster and doesn't need a custom response class."*

**Resolution — the intent is better served by doing less.** §15's intent is *the JSON fast path*.
In this version the fast path is the **default**, and it is faster than routing through a custom
response class. Our views all declare their return type (§5.3 mandates it: `-> TaskDetailOut`),
so we take that path for free. `default_response_class` is therefore *not* set.

Error envelopes are hand-built dicts, not typed returns, so they do not get Pydantic's path.
`api/errors.py` renders them through a six-line `ErrorResponse(Response)` backed by `orjson` —
which keeps the §3 row honest and keeps one JSON serializer in the process.

**Impact on §15's budget table:** none, in the favourable direction. The M8 perf work measures
against the real numbers regardless.

---

## 002 — `include_router` no longer flattens routes into `app.routes` (M0.5)

**Sketch (§5.2):** `app.include_router(r)` at the end of `mount()`.

**Library (fastapi 0.139 / starlette 1.3):** the call works and every route is reachable — but the
included router is attached to the app as **one nested route object**, not flattened into
`app.routes`. Enumerating `app.routes` therefore shows 3 entries where the sketch's mental model
expects 57.

**Resolution — count the contract, not the internals.** Route enumeration (M9.2's *"OpenAPI route
count equals `urls.py` route count"*, and the M9.7 frontend export) goes through
`app.openapi()["paths"]`, which is the thing we actually ship and the thing a client actually
sees. Verified at M0.5: **44 paths / 56 operations**, matching the 56 `add_api_route` calls in
`urls.py` (the WebSocket route is correctly absent from an OpenAPI document).

This is a strictly better check anyway: `app.routes` is an implementation detail of the framework,
while the OpenAPI document is the published contract. A test that watched `app.routes` would have
been testing FastAPI; a test that watches OpenAPI tests *us*.

---

## 003 — `@asynccontextmanager` with `-> AsyncIterator[T]` is deprecated (M0.2, M0.5)

**Library (typeshed, current):** annotating an `@asynccontextmanager`-decorated function as
`-> AsyncIterator[T]` is deprecated; it must be `-> AsyncGenerator[T]`. Caught by pyright strict
in `db/session.py` and `api/app.py`.

**Resolution:** use `AsyncGenerator`. FastAPI dependencies that `yield` but carry no decorator
keep `AsyncIterator`, which is still correct for them.

---

## 004 — the ASGI instrumentation 500s a CORS preflight it can't name (web/F1)

**Library (opentelemetry-instrumentation-fastapi 0.63b1 × fastapi 0.139):** the same nested
`_IncludedRouter` from #002 bites a second place. Naming a request's span, the instrumentation's
`_get_route_details` guards the `route.path` lookup for a FULL route match but **not** for a PARTIAL
one. A CORS preflight — `OPTIONS` against a method-specific route — matches PARTIAL, the matched
object is an `_IncludedRouter` (no `.path`), and the resolver raises `AttributeError`. Because the
ASGI instrumentation wraps *outside* every user middleware (CORS included), the exception escapes as
a bare **500** with no envelope, no CORS headers, and no `request_id`.

**Impact.** Silent and total for the browser: a `Bearer` request is never a "simple" request, so the
SPA precedes every authenticated call with a preflight. A 500 there means the real request never
fires — a signed-in user sees an app that fetches nothing, with nothing in any response body to
explain why. The Python CLI never noticed, because it doesn't preflight.

**Resolution — shim the resolver, don't move CORS.** Reordering CORS doesn't help (the instrumentation
is outside it). `create_app` calls `_make_asgi_span_naming_preflight_safe()` before
`instrument_fastapi`, wrapping `_get_route_details` so an unresolvable route falls back to the raw
path instead of crashing. Idempotent; scoped to the one library function; remove when upstream guards
its PARTIAL branch. Regression guard: `tests/integration/test_cors_preflight.py` asserts
`OPTIONS /v1/auth/me` and `OPTIONS /v1/tasks` return 200 with `access-control-allow-*` — it fails
(500) without the shim.

## #004 — react-router 7 → 8 (security-forced major, 2026-08-05)

**Library (react-router 8.3.0, @react-router/dev 8.3.0, @react-router/node 8.3.0):**
GHSA-qwww-vcr4-c8h2 (RSC-mode CSRF — action execution before the 400) is patched only in
8.3.0; every 7.12+ release stays flagged. This app runs SPA mode against a separate FastAPI
backend — the RSC code path is not in use — so the vulnerability was not exploitable here,
but the alert closes properly only by taking the major. The three packages move in lockstep
(a runtime/plugin split is exactly the mismatch pnpm's auto-override would have caused; the
override was removed and the upgrade done as a coordinated bump).

**Drift.** One rename in our surface: framework-mode loader data is `loaderData` everywhere
`data` used to be — `meta({ data })` → `meta({ loaderData })` (tasks.$id, spaces.$id) and
`useMatches()` rows (`m.data` → `m.loaderData`, breadcrumbs). Typegen, SPA build, and all
222 web tests pass untouched otherwise.

**Alongside (same sweep):** cryptography 49 → 50 (GHSA-g6cj-pr64-35w5, PKCS#7 oracle; our
Ed25519 signing surface is API-identical — signing suite green), and pnpm overrides pinning
vulnerable TRANSITIVE floors — undici ≥7.29, hono ≥4.12.34, @hono/node-server ≥2.0.5,
fast-uri ≥3.1.5, ip-address ≥10.3.1, js-yaml ≥4.3, postcss ≥8.5.23, brace-expansion ≥2.1.4
/ ≥5.0.9 (the 5.0.7 lockfile straggler needed an explicit `pnpm update brace-expansion`).
Overrides live in web/package.json `pnpm.overrides`; drop each when its parent chain
(shadcn CLI, @redocly/openapi-core, eslint's minimatch, vite) catches up upstream.

**Amended 2026-09-27 (the second sweep, 27 Dependabot alerts).** Three floors moved and the
override FORM changed. `pnpm audit --fix` writes open-ended targets (`>=3.1.5`), and an
open-ended target resolves to the newest release of ANY major: that is how ajv (declares
fast-uri `^3.0.1`) had been floated onto fast-uri 4.1.2, and how a re-resolve of the js-yaml
floor would have landed 5.x under four parents that declare 4.x. The overrides touched here
are bounded to the major the parent declares: js-yaml `^4.3.2` (still needed:
@redocly/openapi-core pins 4.2.0 exactly), fast-uri `^3.1.6` (one entry replaces two; ajv is
back on its own major at 3.1.8, and the 3.x line carries the same four fixes), hono `^4.13.5`.
The untouched entries keep the old form until their next bump. vitest's floor moved in
devDependencies (`^4.1.11`); qs, nanoid, browserslist and baseline-browser-mapping needed no
override, their parents' ranges already admit the patched release (`pnpm update <name>`).
Backend: httpx2 / httpcore2 2.5.0 → 2.13.1, transitive under genai-prices, lock-only
(`uv lock --upgrade-package`); httpx2-jsfetch enters the lock behind
`sys_platform == 'emscripten'` and installs nowhere we run.

## 005 - pydantic-ai 2.9.1 hooks that Phase 1c relies on (ADR-064, 2026-10-02)

**Sketch (the program spec):** a step runner "drains the task's signals mailbox" before a model
call, before a tool call, and when a run ends, and may abort an in-flight call.

**Library (pydantic-ai 2.9.1):** the seam is an `AbstractCapability` (`ai/capabilities/steering.py`).
There is no generic drain hook; the intent is met through these, each pinned by a unit test in
`backend/tests/unit/test_steering_capability.py`:

- `before_model_request`: on 2.9.1-2.53 its return value REPLACED the run's message history, so a
  steer was injected by returning the history plus the steer line. From 2.54 the edit is the
  request's only (entry 007): the hook enqueues the line (`RunContext.enqueue(..., priority="asap")`),
  which pydantic-ai delivers into the request being prepared and into the run's history both. The
  inbox's `told` marker keeps it from being injected twice. Pinned by
  `test_a_steer_is_injected_before_the_next_model_request_and_persists` and
  `test_a_steer_is_sent_once_and_the_history_keeps_it`.
- `before_tool_execute` raising `SkipToolExecution` holds a call chosen before a steer: the model
  gets the held text as the tool result and no retry is spent. Pinned by
  `test_a_call_chosen_before_the_steer_is_held_and_spends_no_retry`. Raising `CallDeferred`
  parks a pause, and is a `UserError` unless the agent's output type includes
  `DeferredToolRequests` (hence only `can_ask_user` agents park). Pinned by
  `test_a_pause_defers_the_next_call_only_when_the_run_can_park` and
  `test_build_mounts_steering_only_with_an_inbox_and_parks_only_an_asking_agent`.
- `after_node_run` with `RunContext.enqueue(..., priority="asap")`: validate hooks never fire for
  plain-text output, so the final-answer safe point enqueues the steer as a pending message; the
  auto-injected `PendingMessageDrainCapability` turns a run that would end into one more model
  request carrying it, with no output retry spent. Pinned by
  `test_a_run_about_to_finish_takes_the_steer_first`.

**Intent preserved:** safe-point application without driving `agent.iter()` by hand. Early
cancel of an in-flight call has no hook and is not built (spike NO-GO).

## 006 - OpenAI-compatible streams that report cumulative usage on every chunk (2026-10-25)

**Library (pydantic-ai 2.9.1):** `OpenAIStreamedResponse` adds every chunk's `usage` into the
run's total (`self._usage += chunk_usage`). OpenAI sends usage once, on the last chunk, so the sum
is right there. Baseten - and vLLM with continuous stats - send the CUMULATIVE total on every
chunk, so the sum is wrong, and wrong quadratically in the reply's length. Found live: a
14-second GLM-5.3-Flash memo call metered at 1.92M input / 211k output tokens ($0.39, true cost
about $0.006). The library's own switch (`openai_continuous_usage_stats`) replaces instead of
adds, but it also asks every server for continuous stats, and on a server that reports usage
once and then sends a usage-less chunk it would reset the total to zero.

**Resolution:** `providers._openai_compatible` builds a subclass whose `_streamed_response_cls`
(the library's documented override point) returns a stream whose `_map_usage` (private) yields
`usage_increment(seen, counted)`: a report >= what was counted in every field is cumulative and
contributes only its increase; anything else is added whole. Right for both server shapes, and
never under-counting. `test_gateway::test_a_streamed_openai_compatible_call_is_metered_once`
streams both shapes through the real class - it fails if a pydantic-ai upgrade moves the hook.
First-party OpenAI rows (the Responses API) are untouched.

## 007 - pydantic-ai 2.53 -> 2.68: retries are tool returns, hook edits stay in the request (2026-11-14)

**Locked:** `pydantic-ai-slim`, `pydantic-evals`, `pydantic-graph` 2.53.0 -> 2.68.0 and
`genai-prices` 0.2.0 -> 0.2.3, nothing else (`uv lock --upgrade-package` for those four only;
anthropic 1.17.0, openai 3.41.0, google-genai 2.34.0, fastmcp-slim 3.4.11 and mcp 1.30.0 unchanged).
`pydantic-evals` pins `pydantic-ai-slim` exactly, so the two always move together.
`[tool.uv] constraint-dependencies` bounds `fastmcp-slim<4` and `mcp<2`: fastmcp-slim 4 would pull
the MCP SDK 2.x, a protocol move the connector client has not made.

**Retries (2.60).** `RetryPromptPart` is deprecated. A retried tool call is a
`ToolReturnPart(outcome="retried")` (with `retry: RetryInfo`), a retry tied to no call is a
`RetryFeedbackPart`, and `FunctionToolResultEvent.part` is always a `ToolReturnPart`. Every reader
of a history that meant "a call that ran" now says `outcome != "retried"`: the run guard's recall
(`ai/capabilities/loops.py`), the browser's open history (`ai/tools/browser_policy.py`) and page collapse
(`ai/tools/browser_history.py`), and the proof checks (`operations/execution/proof.py`). The
activity feed's `ok` (`ai/agents/tap.py`) is `outcome == "success"`: a call retried, failed (an MCP
`is_error`), denied or interrupted is not shown as ok. A history a 2.53 park stored still loads: its
`"retry-prompt"` parts come back as the new parts, it resumes, and the helpers skip its retries
(`tests/unit/test_history_compat.py`, over `tests/fixtures/pydantic_ai/parked_history_v2_53.json`,
with pydantic-ai's deprecation warnings as errors). The move is forward-only: a history 2.68 dumps
holds `"retry-feedback"` parts 2.53 cannot read, so a park written after the upgrade does not
survive a rollback to 2.53. `tools/gen_parked_history.py` wrote the fixture under 2.53 and
refuses any other version; it is the one place `RetryPromptPart` is still named.

**Hook edits (2.54).** A `before_model_request` hook's edited message list is no longer written
back to the run's history (only messages appended to the list are, with a deprecation warning).
Prismoid's steer and the run guard's notices are enqueued instead (`RunContext.enqueue(...,
priority="asap")`, the library's documented way for a request-scoped line): the model gets the
line in the request being prepared, and the history keeps it as its own `ModelRequest` after that
request (the wire is the same: the model is sent it inside that request). A hook that only edited the
list sent a steer once and lost it from the history, and said a budget notice again on every
request; finding the trailing request in `ctx.messages` by identity missed whenever an earlier hook
had handed the request on as a copy. Pinned by `test_steering_capability`
(`test_a_steer_is_sent_once_and_the_history_keeps_it`) and `test_run_guard`
(`test_the_notices_enter_before_the_request_once_each`), each also behind a hook that copies the
request, with warnings as errors.

**Other changes that touch Prismoid.** 2.62: an `MCPToolset` with no `id` takes its server's hostname,
which keys the server's instructions; two connectors on one host would share it, so each toolset
gets `id=spec.slug` (`ai/tools/connectors.py`, `test_each_connector_toolset_is_keyed_by_its_slug`).
2.64: a status the retrying transport gave up on is a `ModelHTTPError` from every provider (OpenAI
and Google used to leak the raw `httpx2.HTTPStatusError`), so `FallbackModel` now changes models on
all of them; the raw-httpx branch in `model_incapable` stays for errors raised mid-stream
(`ai/gateway/throttle.py`, `ai/agents/base.py`). Entry 006's override points (`_streamed_response_cls`,
`_map_usage`) are still where it says, re-exported from `pydantic_ai.models.openai`.

**Security advisories, read against the code on 2.68.**
- GHSA-4cj4-hgwf-4hc4 (file-shaped JSON in a tool result rebuilt as a file part on reload): the
  path is reached, since a park saved before the call log reloads its history through the v1
  decoder (`schemas/park.py`, `operations/execution/run.py`); the precondition is not met, since every Prismoid tool
  returns `str` or `BinaryContent` and a connector's structured result is turned into fenced JSON
  text (`fenced_result`). 2.66 closes it either way.
- GHSA-7494-925c-rrxv (exponential `$ref` inlining): not reached by today's catalog. The only
  profile Prismoid can build that inlines definitions is a `gpt-oss*` model id on the OpenAI-compatible
  path (`openai_model_profile` picks `_GptOssJsonSchemaTransformer`, `profiles/openai.py:674`,
  which `OpenAIProvider.model_profile` reaches); the catalog has no such row, but adding one is
  data, not a deploy. Anthropic, Google (`GoogleJsonSchemaTransformer`; only the realtime OpenAPI
  transformer inlines), first-party OpenAI and every other OpenAI-compatible id leave
  `prefer_inlined_defs` off. An inlining profile would meet user-attached MCP schemas; on 2.68 an
  oversized one raises `InlinedSchemaTooLargeError` and fails the run instead of hanging it.
- GHSA-5ppx-9jq6-6pmw (`web_fetch_tool` and the harness browser resolving refused hosts): not
  reached. Prismoid has no `web_fetch_tool` and no harness; its web door is `ai/tools/fetch.py` with
  resolve-and-pin.
