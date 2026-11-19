# Prismoid - the design brief

## The vocabulary, the rules, the packages and the shape of the system, for contributors

---

# 0. How to use this document

This is the design brief Prismoid was first built from, kept as the reference for its words, its rules
and its shape. Read it as a map of the system and the reasons behind it; `STATE.md` says what is
true today, and the decision records in `docs/adr/` say what changed since.

- **§1-2 are the law.** The vocabulary and the non-negotiable rules outrank convenience and
  elegance. The root `CLAUDE.md` restates them with every rule added since; `just check` and
  `/review` (ADR-075) hold both.
- **§3 is the dependency table.** Every dependency earns its row: a new one is a decision record
  plus a row here, in the same change (Rule 13). Code and docs cite its rows as "§3".
- **§4-15 are the design:** the layout, the web layer and the CLI, authentication, the domain, the
  engine, models, artifacts, skills, the model glossary, the database, the guardrails, and the
  performance budgets (§15's budgets are requirements).
- **Later decisions win.** The brief keeps the words of its time in places: read Space as Project,
  Organization as Workspace, and `computer.*` / `COMPUTER_*` as `prismoid.*` / `PRISMOID_*` (ADR-067 maps
  every retired name). §4's tree is the first layout; the README's folder table and
  `docs/architecture.md` show today's. Where a decision record amends a section, the record is
  the current word.
- **How work is done** is the root `CLAUDE.md`'s process rule (ADR-008): `just check` before every
  commit, `just test` before a workstream merges, the drills on their cadence, one real run for a
  user-visible change, and `STATE.md` updated when a workstream lands. The first build's own
  protocol (its milestones, logs, environment list, first rules file and definition of done) is
  not carried here; §14 keeps the URL-topology rule that environment list held.
- **Library-vs-sketch drift:** follow the library, keep the intent, record it in
  `docs/adr/006-api-drift.md`.

---

# 1. Context and vocabulary

**Prismoid** is a general-purpose digital worker: it takes an outcome, plans it into **Steps**, executes them with **sub-agents** (model × tools × skills × contract) inside sandboxed **frames**, and **verifies before delivering** — every artifact re-derived/re-built/re-checked, then cryptographically **signed**. Failures trigger targeted retries; unresolvable ones escalate with a diagnosis. Every state change is an event; the event log is the flight recorder. Equation: `VALUE = PRIZE × DELIVERY × MEMORY − COST − RISK`; Phase 1 makes **DELIVERY** near-certain and ships the product spine — auth, Projects, Capture, Inbox, the usage ledger, user-editable skills — production-grade. Sandbox + read-only web only.

**Vocabulary (fixed):**

| Word | Means | Notes |
|---|---|---|
| **Task** | The unit of work a user submits. `POST /v1/tasks`. | The domain noun |
| **Step** | A node in a Task's plan (`step \| gate \| artifact`). | Plan node, not a durability primitive |
| **Job** | A durable background process. Lives in `jobs/`. | *A Task is what the user asked for; a job is the machine process that runs it.* Declared `@durable.job(name)` (ADR-069; a DBOS workflow underneath) — "workflow" is reserved for Plane's product concept |
| **Effect** | Journaled I/O inside a job (`@durable.effect` fns in `jobs/effects.py`; a DBOS step underneath). | The durability primitive |
| **Workspace** | The tenant (ADR-061, named by ADR-067): a person may belong to many; every tenant row carries `workspace_id`. | Plane's word for the same thing |
| **Project** | A **simple folder** to organize Tasks — optional; Tasks are created anywhere and **moved in/out/between projects freely**. Owned by one person, shareable (members: editor \| viewer). Boundary for budgets, rollups, flags. | Task↔project stays one nullable FK; membership is actor↔project (§6) |
| **Capture** | Soft intake (note/url/idea), promotable to a Task. | Runs nothing |
| **Inbox** | Notifications (task_completed, gate_opened, question, escalation, budget, project_shared). | Points at gates; never sends mail; pushes to its recipient's own phones (ADR-100) |
| **Fleet** | Task mode (ADR-010): every eligible enabled model answers the task independently, in parallel; a chair tabulates the answers side by side, reporting agreement only where it exists. | A plan shape, not a subsystem |
| **Question** | A structured, **numbered** ask to the user, carried by an `input` Gate; resolved by three actions (accept \| decline \| cancel), with expiry + timeout policy. | Aligned with MCP elicitation so external clients render natively (§8) |
| **Publish** | The only way content becomes an Artifact: `frame.publish()` from `/work` → `/artifacts` + manifest. | §10 |
| **Bot** | A user-authored agent, as data (ADR-045): `bots_registry` row, draft → test → publish behind a 409 selection eval, Project-scoped or personal. Compiles to an `AgentDef` per task via `TaskDeps.bots` — never enters `REGISTRY`; a built-in always wins a name collision. | The roster One routes across |
| **Routine** | Standing work (ADR-048): a saved composer payload + a **When** (schedule \| api \| event, + manual Run-now). A Routine executes nothing — every fire mints an ordinary Task; every fire and non-fire journals a `routine_runs` row with its reason. | The third axis: when work begins |
| **Template** | A bot published as an immutable snapshot that travels as a link (ADR-056 §4): `bot_templates` row under a plaintext `bt_` slug; only a PUBLISHED head travels; the anon preview shows everything that installs; one click lands a DRAFT copy in the installer's personal scope, wired to their own connectors. Credentials structurally never travel. | How a bot moves between people |
| **Conversation** | Talk with one bot (ADR-057, ADR-059): a `conversations` row and its `messages` (user \| bot \| system lines). **A Conversation executes nothing** - a reply is ONE frameless model call, STREAMED (`GET /v1/conversations/{id}/events`: the two-lane socket, one table over); every piece of work it needs mints an ordinary Task through the door (`ai/policy/conversation`: a pure ladder, the model as a proposer, consent as the gate: a gesture, a tap, or a standing grant inside the bot's cap - and only for what a message cannot hold: a memo IS the reply). Every user line wears its verdict and reason; a chat-born task's start, delivery, failure, and gates come home as system lines, and the conversation rides the task's THREAD as turn 0 (never an attached source). **The one place a bot talks** - a task thread's talk turn is the Lead's reply. Replaces ADR-045's DM-as-task lane. | The fourth twin: talk is not work |
| **Channel** | A bot's external door (ADR-056 §5, amended ADR-057): a `bot_channels` row (webhook `bc_` token \| per-bot email address \| BYO Telegram) - a TRANSPORT for a Conversation (one per `(channel, thread)`); inbound text is DATA behind `CHANNEL_LAW`; an outsider talks and never starts spend unless the owner's `work_grant` says so; refusals are message rows wearing their reason (the `channel_messages` ledger is retired); approve/budget/connect gates never leave the product. | How the outside talks to a bot |

Metric: **IPT — Interventions Per Task** (unplanned human touches; designed gates excluded).

**Doctrine:** layer-first, one place per kind, legible in **three screens** — `api/urls.py` (every route), `ai/agents/all.py` (every agent), `jobs/all.py` (every job). Fat models, thin views (≤20 lines), `operations/` as the only composer. The law: **views never touch agents; agents never contain business logic; operations compose both.** Three data homes: rows → `db/models/`, domain values → `schemas/`, responses+request bodies → `api/serializers/`. The engine `ai/` is persistence-free and extractable.

---

# 2. Non-negotiable rules

1. **Three data homes**; no bare dicts across boundaries. Every db model opens with its glossary line (§12) as a docstring.
2. **One place per kind:** tables `db/models/` · routes `api/urls.py` · views `api/endpoints/` (**one file per resource**, named functions) · request/response shapes `api/serializers/` (**one file per resource**, `*In`/`*Out`) · verbs `operations/` · background work `jobs/` · engine `ai/` · flags `features/`.
3. **Thin views:** auth → validate → one operation or fat-model call → serialize. No business logic, no `ai/` imports, no transactions started in `api/`.
4. **Operations own transactions**; enqueue after commit with deterministic job ids (`task-{id}`) + the sweeper (§5.4).
5. **The worker owns all machine work.** No I/O in job bodies — I/O lives in effects.
6. **ORM rows never cross upward.** **`ai/` is persistence-free** and imports neither `db`, `operations`, `jobs`, nor `api`.
7. **Model strings only** in `ai/gateway/providers.py` + `ai/tools/providers/`; everything else speaks `ModelClass`. **Adding a model is data, not a deploy** (§9).
8. **Every provider call emits a `UsageEvent`** (native units + write-time `cost_usd`). No metering, no merge.
9. **Artifacts exist only via `frame.publish()`** (§10): nothing outside `/artifacts` + manifest is harvestable; nothing unverified ships without an explicit `unverified` mark. Every tool call passes the `tool_record` and approvals capabilities; an ungranted tool is not mounted. Secrets never enter frames, prompts, or fixtures.
10. **Append-only:** `task_events`, `plan_edits`, `usage_events`. Every task-scoped span/event carries `task_id` + `step_id` (+ `project_id`).
11. **Migrations only** (Alembic). Namespace `prismoid.*`; env prefix `PRISMOID_`.
12. **Performance is a requirement** (§15 budgets; nightly gate fails >20% p95 regression).
13. **Boring beats clever.** New dependency = ADR + a §3 row. No row, no import.
14. **Every route requires an Actor** except `/healthz`, `/v1/auth/login`, `/v1/auth/signup`, `POST /v1/auth/forgot-password` + `POST /v1/auth/reset-password` (*added 2026-08-16, ADR-031: a locked-out user has no credentials by definition; the pair's authentication is the emailed single-use, hashed, TTL-bound `cr_` reset code, and completing a reset revokes every live session and every live API key*), and `GET /v1/connectors/oauth/callback` (*added 2026-08-08, ADR-020: a browser returning from an identity provider carries no bearer token; the route's authentication is its single-use, hashed, TTL-bound `cn_` state parameter*). Auth resolves to the same Actor identity agents and services use — one attribution system. **Vocabulary at the boundary:** the external API speaks *user*, *agent*, and *API key*; the word "actor" appears in responses only inside event/audit payloads (`actor: {type, id, name}`). Internally, everything joins on `actor_id`.

---

# 3. Packages — every dependency earns its row

| Package | Role | Used in | Why this, not the alternative |
|---|---|---|---|
| `fastapi` | HTTP framework | `api/` | Async-first, Pydantic-native; Django/DRF rejected (sync heritage + an ORM we don't want) — but we adopt DRF's *serializer-per-resource clarity* (§5) |
| `uvicorn[standard]` | ASGI (+uvloop, httptools) | `runner/` | The §15 perf floor; gunicorn deferred until evidence |
| `pydantic` v2 / `pydantic-settings` | Validation; config | everywhere / `conf.py` | The constitution; one typed Settings tree |
| `pydantic-ai-slim[anthropic,openai,google,mcp,retries]` | Agent runtime (Agent, tools, output_type, MCP, usage; `retries` = tenacity-backed transports, ADR-024 — tenacity was already transitively locked via google-genai, the extra declares it honestly) | `ai/` | Model-agnostic, typed outputs, first-party DBOS integration; LangChain/LangGraph rejected (second brain). xAI/DeepSeek/Ollama ride the OpenAI-compatible provider — zero extra deps; the same wire serves grok-imagine images (ADR-024) |
| `mcp` SDK + `fastmcp-slim[client]` (carried by the `[mcp]` extra) | MCP client for connectors (ADR-020): `MCPToolset` transport + the typed OAuth wire models (`mcp.shared.auth`) our server-side flow parses with | `ai/tools/connectors.py`, `operations/connectors.py` | The official SDK the extra already names; hand-rolled MCP framing rejected. `authlib` arrives transitively and is **never imported** — the audit ban stands; the OAuth legs are 4 typed httpx calls (ADR-020) |
| `dbos` | Durable jobs, queues, cron, recv/send on Postgres | `jobs/durable.py` ONLY (ADR-069; the audit holds it) | In-process, on the Postgres we run; Temporal (cluster) and Celery+Redis (no journal, no parking) rejected |
| `logfire` (extras: `fastapi,system-metrics,asyncpg,httpx`) | OTel tracing/metrics: ASGI + host vitals + DB round-trips + outbound HTTP + pydantic-ai agent spans | `runner/` + spans | One instrumentation story; OTLP dual-export for sovereignty; the extras are the same story's remaining instruments, not new dependencies |
| `python-json-logger` (≥3.3, BSD-2-Clause) | stdout log lines as JSON in staging/prod (ADR-101) | `prismoid/stdout_log.py` ONLY | The de facto JSON formatter for stdlib logging, already used across Plane's Python services; Logfire has no JSON console |
| `sqlalchemy[asyncio]` 2.0 / `asyncpg` / `alembic` | Fat models / driver / migrations | `db/`, `migrations/` | `Mapped[]` typing; SQLModel rejected (fuses row-home and schema-home); asyncpg also powers LISTEN/NOTIFY |
| `argon2-cffi` | Password hashing (argon2id) | `operations/auth.py` | OWASP first choice; bcrypt's 72-byte quirk avoided; passlib unmaintained-risk rejected |
| `pydantic-evals` | Golden tasks, skill evals, LLMJudge | `evals/`, verifier judge | Evals and verification share one vocabulary |
| `sse-starlette` / `orjson` / `httpx` | **SSE framing (co-primary transport)** / JSON fast-path / HTTP client | `api/stream.py` / `api/app.py` / CLI+providers | EventSource auto-reconnect + `Last-Event-ID` = replay built into the transport (§5.5); WS for duplex-preferring clients; measurable p95 win; client-per-provider keep-alive |
| `httpx2` (≥2.13; BSD-3-Clause, pydantic's maintained httpx successor) | The provider wire: the gateway's shared per-provider client + `AsyncHTTPX2TenacityTransport` (ADR-072) | `ai/gateway/throttle.py`, `ai/gateway/providers.py` ONLY | What the provider SDKs (openai ≥3, anthropic ≥1, google-genai) and pydantic-ai ≥2.52's retrying transport speak; the httpx transport is deprecated. Already locked transitively; declared honestly (ADR-024's precedent). Everything else stays on `httpx` |
| `boto3` | S3/MinIO (artifacts, snapshots, skill bodies) | `operations/`, `ai/frames/`, `ai/skills/` | Ubiquitous, boring |
| `pixeltable-pgserver` (==0.7.2) | Embedded Postgres 18 binaries (Unix socket only) for local mode, ADR-077 | `local/postgres.py` | The maintained fork of `pgserver`/`postgresql-wheel`; macOS arm64 + x86_64 wheels; Docker/Homebrew rejected for `prismoid up` |
| `docker` (SDK) / `cryptography` / `typer` | Frame driver / Ed25519 + Fernet at-rest cipher (connector secrets, ADR-020) / CLI | `ai/frames/` / signing + `operations/cipher.py` / `runner/` | Driver swaps later behind the Protocol; audited primitive; CLI is a thin API client |
| `sandbar` (own subproject, Go) | Firecracker-microVM frame backend, over HTTP | `ai/frames/sandbar_driver.py` → `sandbard` daemon | Own-kernel isolation for untrusted model code (ADR-012 risk #1, ADR-013). Not a linked dep — Plane speaks its HTTP API; E2B/Modal rejected as external SaaS (the sandbox must be ours, self-hostable, behind the FrameDriver seam). Repo root `sandbar/`; stdlib-only + `golang.org/x/sys` |
| `reportlab` + `pypdf` (frame image, and the device runtime below) | PDF authoring / PDF read-back inside the frame | `ai/frames/image/Dockerfile` · `local/runtime_requirements.txt` (never the worker venv; the document suite imports pypdf lazily, in-frame) | The PDF lane (ADR-021): `document` accepted `.pdf` with no way to write one or read one back — a judge could pass a file nothing had read. reportlab pinned at 4.5.x (boring beats the weeks-old 5.0); pypdf closes the verification blind spot. WeasyPrint rejected (system pango/cairo in a slim image) |
| `poppler-utils` (frame image, apt; on the device the user's own `brew install poppler`, optional: `prismoid up` says what is off without it, ADR-078) | `pdftoppm` rasterizes a rendition's pages to PNGs so the author and the QA gate can LOOK at a deliverable | `ai/frames/image/Dockerfile` · `image/house/plane_house/render.py` · `ai/verification/precheck.py` · `local/runtime.py` (`preflight` finds it) | The visual pass ADR-026 deferred, lifted by ADR-034. One small static binary set (pdfinfo/pdftotext ride along). PyMuPDF rejected: a second PDF engine beside LibreOffice/pypdf, a 40 MB wheel, AGPL |
| `libreoffice-writer/-calc/-impress` + Liberation/Carlito/Caladea fonts (frame image only) | Full-fidelity renditions (ADR-023): docx/pptx → pixel-faithful PDF, xlsx → recalculated styled HTML, at harvest in the work frame | `ai/frames/image/Dockerfile` → the static runner `ai/frames/preview.py` (soffice headless, per-run recalc-forced profile) | The industry headless office renderer — ~600 MB is the honest cost of "people expect THE DOCUMENT", recorded in the ADR; no Java, no GUI stack. The metric-compatible font trio keeps Word's line breaks. Rejected: client-side office parsers (untrusted zips in the reader's origin, ADR-022) and per-format bespoke PDF pipelines (one engine, three formats) |
| `openpyxl` · `python-docx` · `python-pptx` · `pandas` · `matplotlib` · `pyyaml` · `reportlab` · `pypdf` · `markitdown[docx,pptx,xlsx,pdf]` · `mammoth` · `pdfminer.six` · `pdfplumber` (the device runtime: installed on the user's Mac, never in Prismoid's own `pyproject`) | What a frame image bakes in, for a task on the device: previews, QA prechecks, verify suites and an agent's own document scripts (ADR-078) | `local/runtime_requirements.txt` → a venv at `<home>/runtime/`, built by `local/runtime.py` at `prismoid up` | The image's pins line for line (`tests/unit/local/test_runtime_build.py` fails when they differ), so a file is read by the same code on the device and in the cloud; wheels only, so nothing a package ships runs at install. The user's `python3` is theirs and cannot be relied on |
| `uv` (expected on the user's PATH; not a Prismoid dependency) | Builds the device runtime: a relocatable Python 3.12 venv (ADR-078) | `local/runtime.py` | Prismoid's own setup already needs uv (`just install`), so it is normally there. Absent, the build uses the Python Prismoid runs under plus pip; a build that fails never stops `prismoid up`, and a typed publish then says how to retry |
| `chromium` (Debian bookworm-security, one exact build pinned) + `fonts-noto-color-emoji` (browser frame image only, apt) | The navigator's browser (computer use, ADR-066): headless, driven over a DevTools pipe by the stdlib sidecar as its own uid | `ai/frames/image_browser/Dockerfile` → `sidecar/browser.py` | The distro build: security updates through Debian, no second package source, and the pin is the tag (a bump is a new image on every worker; a stale pin fails the build, the alarm to bump). Runs `--no-sandbox` with the container as the boundary (cap_drop=ALL, no-new-privileges, egress via the allowlisting proxy only), recorded in the ADR with the owner's alternatives. Playwright rejected: its driver brings Node into the frame, and the sidecar must not need Node |
| `@dnd-kit/core` + `/sortable` + `/utilities` (web) | Collection drag gestures — board card→column + reorder, row reorder in table/list/gallery, calendar chip→day (ADR-015 M3, Collections v4) | `web/…/renderers/collection/**` ONLY (oxlint-fenced, `no-restricted-imports`; the collection's lazy chunk — read-only viewers mount no hooks) | Headless + MIT + keyboard/SR sensors (ADR-016, amended 2026-07-31 for sortable); react-beautiful-dnd archived, pragmatic-dnd heavier, hand-rolled pointer math has no a11y. Gantt bars stay hand-rolled (continuous axis). Chunk ≤48 kB gz |
| `pyyaml` | Skill manifests + eval cases (YAML load/dump) | `ai/skills/`, `evals/` | The authoring lingua franca of skill manifests and `cases.yaml`; the stdlib has no YAML, and JSON is the wire format (`orjson`), never the hand-edited one |
| `shiki` (web) | Syntax-highlighted code artifacts — the `CodeBlock` renderer | `packages/blocks/src/code-block/` ONLY (ADR-074; `web/app/components/kit/answer-code-block.tsx` reads its language list) | TextMate-grammar fidelity in the reader; a **real npm dependency, not vendored** (STATE.md's earlier "vendored" wording was wrong). Prism/highlight.js rejected for grammar accuracy |
| `@fontsource-variable/jetbrains-mono` (web) | The mono face: one `--font-mono` token every `font-mono` rides (ADR-053) | `web/app/app.css` import | Code became a first-class answer surface; the OS default stack read as unfinished. Self-hosted like Inter; ADR-017's one-face law governs the heading seam only |
| `mermaid` (web) | ```mermaid fences in rendered answers → diagrams (ADR-053, owner call) | `web/app/components/kit/mermaid-block.tsx` ONLY, dynamic import - never an eager chunk | The buggiest surface in the 2026-08 landscape, so bound: closed fences only, `securityLevel: strict`, error falls back to the code block, static render is a placeholder |
| `trafilatura` (≥2.2) | `web_fetch`'s page reader: URL → main-content markdown + citation metadata (ADR-046) | `ai/tools/fetch.py` ONLY | The LLM-corpus standard (RefinedWeb/FineWeb/FineWeb2 name it; NeMo Curator + IBM data-prep-kit ship it; best single tool, SIGIR 2023). **Apache-2.0 since 1.8.0 — pre-1.8 is GPL and never enters.** readability-lxml rejected (strictly weaker; trafilatura embeds it as fallback); resiliparse rejected (corpus-scale speed we don't need); html2text rejected (GPL) |
| `cronsim` (≥2.6) | Routine fire-time math: next-fire + missed-fire computation with Debian-cron-exact DST semantics (ADR-048) | `schemas/routines.py` ONLY | The library whose DST behavior an incident review can defend: matches Debian cron verbatim (spring-forward fires once after the gap, fall-back never twice), validated against 5000+ expressions; croniter rejected — its DST record is the documented reason Sentry and Healthchecks migrated to cronsim (croniter remains a DBOS transitive, unimported by us). Zero-dep, zoneinfo-native |
| `markitdown[docx,pptx,xlsx,pdf]` (0.1.x) | The file→markdown door: fetched binaries via the resource-limited sidecar (`ai/tools/convert.py`); in-frame `markitdown <file>` for mounted inputs (ADR-046) | `ai/tools/convert.py` (sidecar-only) · frame image. **Base dependency, and one image serves api+worker+migrator — so its ≈257 MB transitive tail (onnxruntime/pandas/sympy/numpy/lxml/pillow) ships to the API too; accepted knowingly, see ADR-046 Consequences** | MIT, Microsoft, the format-breadth standard (mammoth DOCX genuinely good). Never `[all]` (Azure SDKs, network-calling audio); its `convert_uri`/MCP lanes REFUSED (live SSRF findings — only our guard dials); PDF-structure ceiling recorded, docling named as the upgrade. pymupdf4llm/marker rejected (AGPL / GPL+restricted weights) |
| `websockets` (≥16.1,<18) | The worker's CDP connection to a remote browser (`WsCdp`): the device Machine's relay (ADR-078) | `ai/frames/cdp_ws.py`, `device/cdp_relay.py`, `runner/cli/machine.py` (the CLI's `--shot`, its sync client) | Already in every image (dbos, `uvicorn[standard]`, google-genai carry it): the pin only names it. Its asyncio client brings masking, fragmentation, ping/pong and close codes. The CLI's own stdlib client (`runner/ws.py`) was retired for its sync client: one WebSocket implementation, not two |
| `@novnc/novnc` (1.7.0, exact; MPL-2.0) | The lease holder's VNC client in the task pane, for a driver whose screen door serves a control stream (ADR-065, ADR-103) | `web/app/lib/screen-vnc.ts` ONLY (dynamic import; loaded only when the relay opens a control stream) | Its RFB attaches to a WebSocket-like raw channel, so Prismoid's one screen socket carries the desktop and no driver ticket reaches the browser. Rejected: a browser socket straight to a driver's relay (a ticket in a browser URL) and an RFB client of our own |
| `turbo` (dev, root workspace) | The JS task runner: `build`, `dev`, `typecheck`, `lint`, the guards, `test`, and the `check` gate, cached and graph-ordered (ADR-071) | `turbo.json` · root `package.json` scripts · `just web-check`/`web-build` | Plane's monorepo tool, so Prismoid's layout and commands match Plane's. Every JS version lives in the pnpm 12 workspace catalog (`pnpm-workspace.yaml`). Nx rejected (a second build model); bare `pnpm -r` has no cache and no task graph |
| `oxlint` + `oxfmt` (web dev) | The web gate's linter (`pnpm lint`: the ported ESLint recommended + typescript-eslint recommended + the hooks rules + jsx-a11y + the dnd-kit fence) and the formatter (`pnpm format`, `pnpm format:check`, Prettier-compatible, `printWidth` 100) (ADR-073) | `web/.oxlintrc.json` · `web/.oxfmtrc.json` | Plane's toolchain; one Rust binary each in place of ESLint + typescript-eslint + eslint-plugin-react-hooks + @eslint/js and Prettier + its tailwind plugin. The rules were carried by `@oxlint/migrate`, not rewritten. Biome rejected (a second rule vocabulary, not plane's) |
| `@makeplane/propel` (web, via `@prismoid/blocks`; exact pin; AGPL-3.0-only, makeplane's own) | Plane's design system: tokens (Tailwind v4 source CSS) and Base UI components. Stage 1 took the tokens with a monochrome accent, stages 2-3 moved every call site onto its components, and stage 4 deleted the shadcn kit (ADR-074) | `packages/blocks/src/styles.css` (the theme) → `web/app/app.css` | Plane's component library, so Prismoid looks and behaves like Plane. Built on the same Base UI as the kit it replaces (one copy, ≥1.8). Keeping the shadcn kit and Prismoid's own palette rejected: a second visual language beside Plane's |
| `happy-dom` (21.0.3, web + blocks dev, exact) | The DOM for interaction tests, opted into per file with `// @vitest-environment happy-dom`; every other test stays in node (ADR-079) | `web/app/**/*.dom.test.tsx` · `packages/blocks/src/**/*.test.tsx` | Fast, and enough DOM for Base UI and Testing Library. jsdom rejected: slower to start and heavier, with nothing this suite needs that happy-dom lacks. A global DOM environment rejected: it would slow the ~1,200 node tests for no gain |
| `@testing-library/react` + `@testing-library/dom` (16.3.3 / 10.4.2, web + blocks dev, exact) | Render a component and query it the way a person finds it: by role and accessible name (ADR-079) | the same DOM test files | The standard React testing library; querying by role makes every interaction test an accessibility check too. Enzyme rejected (unmaintained, tests internals) |
| `@testing-library/user-event` (14.6.10, web + blocks dev, exact) | Drive a component with real event sequences: typing, Tab, Enter, Space, arrow keys (ADR-079) | the same DOM test files | Fires the keydown, input and keyup chain a browser does, so keyboard paths are tested as they run. `fireEvent` alone rejected: one synthetic event per call misses focus moves and default actions |
| `@vitest/coverage-v8` (4.1.12, web + blocks dev, exact, matches `vitest`) | `pnpm test:coverage` in `web/` and `packages/blocks/` (ADR-079) | `web/vitest.config.ts` · `packages/blocks/vitest.config.ts` | V8 coverage needs no instrumenting build step. istanbul rejected: a transform per file for the same numbers |
| `pytest-cov` (dev; brings `coverage`) + CI-only tools, never installed into the project: `pip-audit` and `pip-licenses` (uvx, exact pins), `gitleaks` (pinned release binary, checksum-verified), `staticcheck` and `govulncheck` (`go run`, exact pins) | Coverage of the unit and integration suites with a no-regression floor in the job summary; the `security` job's vulnerability, secret and license gates; sandbar's static analysis (ADR-080) | `backend/pyproject.toml` (`[tool.coverage]`) · `.github/workflows/ci.yml` · `backend/tools/license_gate.py` | pytest-cov is the standard pytest driver for coverage.py and combines xdist workers' data itself; coverage.py's `greenlet` concurrency sees the ORM's async lines. The scanners are the ecosystems' own (PyPA, the Go team) or the de facto standard (gitleaks), run at a pinned version so a gate does not move under a PR. Rejected: a hosted coverage service (a third party for one number) and the gitleaks Action (needs a license key for organisations) |
| `@playwright/test` (1.65.1, web dev, exact; Apache-2.0) + Chromium's headless shell (downloaded by `playwright install`, never by `pnpm install`) | The browser smoke flows: sign-in, a project, settings, `/ds` and a held task record against a real API and worker, no model (ADR-081) | `web/playwright.config.ts` · `web/e2e/**` · `just e2e` · `.github/workflows/e2e.yml` (nightly) | Microsoft's runner, with role locators that read like the Testing Library queries the DOM tests use; one runner for the flows and their traces. Cypress rejected (runs inside the page, a second way of writing tests). Not in the frame image: the frame's browser stays the sidecar's CDP pipe (ADR-066) |
| `googleapis/release-please-action` (v5, CI only, Apache-2.0; never installed into the project) | Release PRs, on a manual run only: reads the conventional commits on main, bumps `version.txt`, `backend/pyproject.toml`, `backend/uv.lock` and `prismoid.__version__`, writes the CHANGELOG section; tags (a draft GitHub release) only on a manual run with `tag` checked, publishes nothing (ADR-082) | `.github/workflows/release.yml` · `release-please-config.json` · `.release-please-manifest.json` | Google's maintained tool for exactly this, driven by the commit convention the repo already follows, and the version stays readable in the tree with no build or runtime dependency. git-cliff rejected (a changelog generator only: the bump, the PR and the tag would be ours to script); hatch-vcs rejected (a new build dependency, and a build outside a git checkout needs the version passed in) |
| `pgsty/silo` (container image, `RELEASE.2026-10-26T00-00-00Z` pinned by digest; AGPL-3.0-or-later; never a Python or JS package) | The self-hosted S3 store: the dev stack, CI, the nightly frame tests, `just e2e` and the single-host `minio` profile; its bundled `mcli` makes the buckets (ADR-083) | `backend/deploy/compose.yaml` · `deploy/compose.prod.yaml` · `web/e2e/stack.sh` | A maintained fork of the MinIO server, whose own images are gone: the same S3 API, `MINIO_*` settings, health routes and on-disk format, so existing volumes open as they are. The frozen `bitnamilegacy/minio` archive, SeaweedFS and Garage (different systems, different layouts), RustFS (early) and Chainguard's MinIO (unpinnable free tier) rejected. Production may point `S3_*` at any S3 instead |
| `searxng/searxng` (container image, pinned by digest; AGPL-3.0; never a Python or JS package) | The self-hosted search engine behind `web.search`: `just searxng` in dev and the opt-in `search` profile of the single-host stack (ADR-047) | `deploy/compose.prod.yaml` (`searxng`) · `deploy/searxng/settings.yml` · `justfile` | Prismoid speaks its HTTP JSON API at arm's length and ships none of it (ADR-047 names it the self-host engine). It publishes no port; the worker reaches it on the compose network. A keyed engine (Brave, Tavily) needs no container and stays the other way to turn search on |
| `google-auth[requests]` (≥2.56; Apache-2.0; already locked through `google-genai`, which depends on it with this extra) | The FCM push wire's OAuth access token, minted from the Firebase service account (ADR-100) | `prismoid/fcm.py` ONLY | The declaration makes an existing lock honest (ADR-024's precedent): no package joins the lock. firebase-admin rejected (Firestore, Cloud Storage and the API client for one POST); a hand-signed JWT bearer grant rejected (re-implements what google-auth gets right) |

Dev/test: `ruff` · `pyright` strict · `pytest`+`pytest-asyncio` · `pytest-xdist` (2026-08-25 speed pass: `just test` fans out `-n auto --dist loadfile`, one cloned template database per worker — tests/conftest.py) · `pytest-benchmark` · `locust` · `import-linter` · `testcontainers` · `polyfactory` · `pre-commit` · `just`. *(A `playwright`-in-the-frame browser-smoke lane was planned here but never shipped — recorded honestly in ADR-015/ADR-021; `SmokePolicy.BROWSER` stays declared-never-read.)*

**Deliberately not used:** Celery/Redis-broker · Temporal · Kafka · LangChain/LangGraph · SQLModel · Django · Redis cache (rollups + PG first) · GraphQL · passlib · python-jose/JWT (sessions are opaque + revocable in Postgres — JWT's statelessness is a bug for a product with a kill switch).

---

# 4. Repository layout (complete)

```
computer/
├── HANDOFF.md  CLAUDE.md  pyproject.toml  uv.lock  justfile
├── .pre-commit-config.yaml  .importlinter  .env.example
├── .github/workflows/{ci.yml, evals.yml, chaos.yml, perf.yml}
│
├── src/computer/
│   ├── conf.py                     # ONE Settings tree (COMPUTER_*)
│   ├── runner/                     # main.py (typer: run api|worker · login · do · tasks ·
│   │                               #   gates · inbox · skills · models) · worker.py (DBOS boot)
│   ├── db/
│   │   ├── engine.py session.py base.py
│   │   └── models/                 # one file per table, fat, glossary docstring:
│   │       ├── actor.py user.py credential.py session.py api_key.py
│   │       ├── space.py space_member.py task.py plan.py event.py artifact.py verification.py gate.py
│   │       ├── capture.py inbox.py usage.py price_book.py model_catalog.py
│   │       ├── skill.py routing.py feature.py
│   ├── schemas/                    # ids capability contract plan events verification outputs
│   │                               #   usage fleet artifacts(manifest)
│   ├── api/
│   │   ├── app.py urls.py deps.py middleware.py errors.py links.py stream.py
│   │   ├── endpoints/              # ═ ONE FILE PER RESOURCE, named functions (§5) ═
│   │   │   ├── auth.py tasks.py spaces.py capture.py inbox.py gates.py
│   │   │   ├── artifacts.py skills.py registry.py admin.py health.py
│   │   └── serializers/            # ═ one file per resource: *In (request) + *Out (response) ═
│   │       ├── auth.py tasks.py spaces.py capture.py inbox.py gates.py
│   │       ├── artifacts.py skills.py registry.py usage.py
│   ├── operations/                 # auth tasks planning execution verification gates spaces
│   │                               #   capture inbox usage fleet skills models memory(stub)
│   ├── jobs/                       # all.py(★) run_task.py run_step.py sweeper.py effects.py queues.py
│   ├── ai/                         # ═ THE ENGINE (persistence-free, extractable) ═
│   │   ├── __init__.py deps.py     # TaskDeps: frame, warden, emit_event, emit_usage, budget,
│   │   │                           #   producer_family, skill_pins
│   │   ├── agents/  verification/  gateway/  frames/  skills/  tools/
│   │   │   # agents: base registry all(★) + one file per agent + prompts/
│   │   │   # verification: pipeline signing + suites/{document,code,deck,quotes,…}
│   │   │   # gateway: classes catalog resolve providers fallback meter
│   │   │   # frames: driver docker_driver layout egress publish image/
│   │   │   # skills: format index loader registry_client evals
│   │   │   # tools: registry frame_tools browser warden vault providers/(elevenlabs stub…)
│   ├── features/  testutils/
│
├── skills/                         # builtin skill sources (seeded into the registry at migrate)
├── evals/  migrations/  docs/{adr/, runbooks/}
├── deploy/compose.yaml  Dockerfile
└── tests/{integration/, chaos/, perf/}
```

---

# 5. The web layer — router, resources, serializers

## 5.1 The shape (DRF-informed, kept simple)

One **resource file** per domain object in `endpoints/`, containing **named functions** (`create_task`, `list_tasks`, `get_task` — never bare `post`/`get`). One matching file in `serializers/` holding every shape for that resource with a two-suffix convention: **`*In` = request body, `*Out` = response.** That answers "which serializer does GET vs POST use" by making it unaskable — the function signature says it: `create_task(body: TaskCreateIn) -> TaskAcceptedOut`, `get_task(...) -> TaskDetailOut`, `list_tasks(...) -> PageOut[TaskSummaryOut]`. The router stays a single explicit table (SCREEN 1) — we keep Sentry's one-screen `urls.py`, DRF's serializer clarity, and drop the one-file-per-endpoint sprawl.

## 5.2 `api/urls.py` — SCREEN 1 (complete)

```python
"""Every route in the product. One line per route. Read top to bottom = the API."""
def mount(app: FastAPI) -> None:
    r = APIRouter(prefix="/v1")
    # auth (the only unauthenticated routes besides /healthz are signup + login)
    r.add_api_route("/auth/signup",            auth.signup,            methods=["POST"], status_code=201)
    r.add_api_route("/auth/login",             auth.login,             methods=["POST"])
    r.add_api_route("/auth/logout",            auth.logout,            methods=["POST"])
    r.add_api_route("/auth/me",                auth.me,                methods=["GET"])
    r.add_api_route("/auth/api-keys",          auth.create_api_key,    methods=["POST"], status_code=201)
    r.add_api_route("/auth/api-keys",          auth.list_api_keys,     methods=["GET"])
    r.add_api_route("/auth/api-keys/{key_id}", auth.revoke_api_key,    methods=["DELETE"])
    # tasks
    r.add_api_route("/tasks",                  tasks.create_task,      methods=["POST"], status_code=202)
    r.add_api_route("/tasks",                  tasks.list_tasks,       methods=["GET"])
    r.add_api_route("/tasks/{task_id}",        tasks.get_task,         methods=["GET"])
    r.add_api_route("/tasks/{task_id}/cancel", tasks.cancel_task,      methods=["POST"])
    r.add_api_route("/tasks/{task_id}/move",   tasks.move_task,        methods=["POST"])
    r.add_api_route("/tasks/{task_id}/steer",  tasks.steer_task,       methods=["POST"])
    r.add_api_route("/tasks/{task_id}/pause",  tasks.pause_task,       methods=["POST"])
    r.add_api_route("/tasks/{task_id}/resume", tasks.resume_task,      methods=["POST"])
    r.add_api_route("/tasks/{task_id}/follow_up", tasks.follow_up_task, methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/usage",  tasks.get_task_usage,   methods=["GET"])
    # spaces · capture · inbox · gates · artifacts
    r.add_api_route("/spaces",                 spaces.create_space,    methods=["POST"], status_code=201)
    r.add_api_route("/spaces",                 spaces.list_spaces,     methods=["GET"])
    r.add_api_route("/spaces/{space_id}",      spaces.get_space,       methods=["GET"])
    r.add_api_route("/spaces/{space_id}",      spaces.update_space,    methods=["PATCH"])
    r.add_api_route("/spaces/{space_id}/usage",spaces.get_space_usage, methods=["GET"])
    r.add_api_route("/spaces/{space_id}/members",            spaces.add_member,           methods=["POST"], status_code=201)
    r.add_api_route("/spaces/{space_id}/members",            spaces.list_members,         methods=["GET"])
    r.add_api_route("/spaces/{space_id}/members/{actor_id}", spaces.change_role,          methods=["PATCH"])
    r.add_api_route("/spaces/{space_id}/members/{actor_id}", spaces.remove_member,        methods=["DELETE"])
    r.add_api_route("/spaces/{space_id}/artifacts",          spaces.list_space_artifacts, methods=["GET"])
    r.add_api_route("/spaces/{space_id}/transfer",           spaces.transfer_space,       methods=["POST"])
    r.add_api_route("/capture",                capture.create_capture, methods=["POST"], status_code=201)
    r.add_api_route("/capture",                capture.list_captures,  methods=["GET"])
    r.add_api_route("/capture/{capture_id}/promote", capture.promote_capture, methods=["POST"])
    r.add_api_route("/inbox",                  inbox.list_inbox,       methods=["GET"])
    r.add_api_route("/inbox/{item_id}/read",   inbox.mark_read,        methods=["POST"])
    r.add_api_route("/inbox/read-all",         inbox.mark_all_read,    methods=["POST"])
    r.add_api_route("/gates",                  gates.list_gates,       methods=["GET"])
    r.add_api_route("/gates/{gate_id}/resolve",gates.resolve_gate,     methods=["POST"])
    r.add_api_route("/artifacts/{artifact_id}",              artifacts.get_artifact,      methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/download",     artifacts.download_artifact, methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/verification", artifacts.get_verification,  methods=["GET"])
    # skills — user-editable, draft → test → publish (§11)
    r.add_api_route("/skills",                       skills.create_skill,   methods=["POST"], status_code=201)
    r.add_api_route("/skills",                       skills.list_skills,    methods=["GET"])
    r.add_api_route("/skills/{name}",                skills.get_skill,      methods=["GET"])
    r.add_api_route("/skills/{name}",                skills.update_draft,   methods=["PUT"])
    r.add_api_route("/skills/{name}/test",           skills.test_skill,     methods=["POST"])
    r.add_api_route("/skills/{name}/publish",        skills.publish_skill,  methods=["POST"])
    r.add_api_route("/skills/{name}/versions",       skills.list_versions,  methods=["GET"])
    # registry · admin
    r.add_api_route("/agents",                 registry.list_agents,   methods=["GET"])
    r.add_api_route("/admin/routing-table",    admin.get_routing,      methods=["GET"])
    r.add_api_route("/admin/routing-table",    admin.put_routing,      methods=["PUT"])
    r.add_api_route("/admin/models",           admin.list_models,      methods=["GET"])
    r.add_api_route("/admin/models",           admin.add_model,        methods=["POST"], status_code=201)
    r.add_api_route("/admin/models/{id}",      admin.set_model_enabled,methods=["PATCH"])
    r.add_api_route("/admin/price-book",       admin.get_prices,       methods=["GET"])
    r.add_api_route("/admin/price-book",       admin.put_prices,       methods=["PUT"])
    app.include_router(r)
    app.add_api_websocket_route("/v1/tasks/{task_id}/stream", stream.task_ws)   # ?token=cs_…
    app.add_api_route("/v1/tasks/{task_id}/events", stream.task_sse, methods=["GET"])   # SSE co-primary (§5.5)
    app.add_api_route("/healthz", health.healthz, methods=["GET"])
```

## 5.3 A resource pair, verbatim — `endpoints/tasks.py` + `serializers/tasks.py`

```python
# api/serializers/tasks.py — every task shape, one file. *In = request, *Out = response.
class TaskCreateIn(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    space_id: UUID | None = None
    mode: Literal["standard", "fleet", "council"] = "standard"  # 3rd = legacy wire value
    clarify: Literal["auto", "never", "always"] = "auto"   # one intake round max (§8)
    budget: Budget | None = None

class TaskMoveIn(BaseModel):
    space_id: UUID | None = None          # null = back to personal

class TaskSteerIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)

class FollowUpIn(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)

class TaskAcceptedOut(BaseModel):
    task_id: UUID; stream_url: str

class TaskSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID; goal: str; status: str; mode: str; space_id: UUID | None; created_at: datetime

class TaskDetailOut(TaskSummaryOut):
    plan: PlanOut; cost: CostSummaryOut; criteria: dict

def task_summary(t: Task) -> TaskSummaryOut: ...
def task_detail(t: Task, cost: CostSummary) -> TaskDetailOut: ...
```

Signature style (mandated): `api/deps.py` exports **Annotated aliases** — the modern FastAPI idiom (used by the official template) that gives class-based views' deduplication with none of their hidden state:

```python
# api/deps.py — the only three things a view may depend on, as aliases
ActorDep   = Annotated[Actor, Depends(require_actor)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
PageDep    = Annotated[Page, Depends()]
```

```python
# api/endpoints/tasks.py — thin, named, ≤20 lines each; aliases only, never raw Depends
async def create_task(body: TaskCreateIn, actor: ActorDep,
                      session: SessionDep) -> TaskAcceptedOut:
    view = await operations.tasks.create_task(session, actor, body)      # tx + event + enqueue
    return TaskAcceptedOut(task_id=view.id, stream_url=links.task_stream(view.id))

async def list_tasks(page: PageDep, actor: ActorDep, session: SessionDep,
                     status: TaskStatus | None = None,
                     space_id: UUID | None = None) -> PageOut[TaskSummaryOut]:
    rows = await Task.list_for(session, actor, status=status, space_id=space_id,
                               limit=page.limit, cursor=page.cursor)      # fat-model keyset
    return PageOut.build([task_summary(t) for t in rows], page)

async def get_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskDetailOut: ...
async def cancel_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskSummaryOut: ...
async def move_task(task_id: UUID, body: TaskMoveIn, actor: ActorDep,
                    session: SessionDep) -> TaskSummaryOut: ...
async def steer_task(task_id: UUID, body: TaskSteerIn, actor: ActorDep,
                     session: SessionDep) -> TaskSummaryOut: ...
async def pause_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskSummaryOut: ...
async def resume_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskSummaryOut: ...
async def follow_up_task(task_id: UUID, body: FollowUpIn, actor: ActorDep,
                         session: SessionDep) -> TaskAcceptedOut: ...
async def get_task_usage(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskUsageOut: ...
```

`gates.resolve_gate` and `spaces.get_space_usage` follow the same shapes as before (operation resolves + `DBOS.send` wake; rollup read, never ledger scan). Views come in exactly three shapes — write-that-starts-machine-work, write-that-wakes-a-job, read — and no others.

## 5.4 Lifecycles, error envelope, enqueue-after-commit, stream — normative

**Lifecycle A (submit):** middleware (request_id → Logfire) → router → deps (actor, session) → view validates `TaskCreateIn` → operation runs ONE transaction (scope-check Space → insert Task → `TaskEventRow.append(TaskCreated)` with `pg_notify`) → COMMIT → enqueue `run_task` with deterministic id `task-{id}` → view returns 202 with `stream_url` built from `PUBLIC_BASE_URL`. Client opens WS `?since=0&token=cs_…` → replay `seq > since` → live via the **one process-wide LISTEN task** fanning out to per-task subscriber sets → every job state change appends an event → socket (<250 ms). Live token/preview deltas ride the same socket as a second lane (§8.6). **Lifecycle B (gate):** job hits a gate step → `open_gate` effect (row + event + InboxItem) → `DBOS.recv(topic="gate:{step_id}")` parks the job at zero cost for minutes or days → human resolves via `gates.resolve_gate` → operation commits, then `DBOS.send(f"task-{task_id}", resolution, topic=…)` → job resumes < 500 ms. **Errors:** every non-2xx body is `{"error": {"code", "message", "request_id"}}`; handlers map `NotFound→404`, `Forbidden→403`, `Unauthorized→401`, `InvalidTransition→409`, `BudgetExceeded→409 budget_exceeded`, `PolicyDenied→403`, validation→422 with `fields`, unhandled→500 `internal` (logged with request_id, nothing leaked). **Ghost-freedom:** deterministic job ids make double-enqueue harmless (DBOS dedupes); `jobs/sweeper.py` (`@DBOS.scheduled("* * * * *")`) re-enqueues `created` Tasks older than 60 s.

## 5.5 SSE — the co-primary transport, fully specified

WS and SSE are **two framings of one generator** — same events, same lanes, same auth. SSE is not a fallback; for web clients it is the *simpler correct choice*, and since every command in the product is already REST (resolve, steer, pause, cancel), **SSE + REST is a semantically complete client** — WS adds a duplex socket preference, not a capability.

**Framing** (`Content-Type: text/event-stream`, sse-starlette `EventSourceResponse`): each frame carries `event: <type>` (the union discriminator — `task.created`, `message.delta`, …) and `data: <json>` (the event body, identical to the WS payload). Heartbeats are **comment frames** (`: hb`) every 15 s — comments keep proxies alive without waking client handlers. `retry: 3000` advises the reconnect delay.

**The id rule (correctness, not style):** **only Lane-1 durable frames set `id: <seq>`; Lane-2 delta frames never set `id`.** EventSource auto-reconnects with `Last-Event-ID: <last seen id>`, which the server treats exactly as `?since=<seq>` — replay from the record, then live. If an ephemeral delta ever advanced `Last-Event-ID`, a reconnect after it would silently skip durable events; id-less deltas make the cursor move only on durable ground. Net effect: **the reconnect contract is implemented by the browser itself** — zero client reconnect code — and the §8.6 reconciliation rule (deltas terminate in durable finals/snapshots) guarantees nothing rendered is lost.

**Auth:** browsers' EventSource cannot set headers → `?token=` (the existing WS convention; scrubbed from logs). Non-browser clients (curl, httpx, the CLI) use the normal `Authorization: Bearer` header — both paths hit the same `require_actor`.

**Deployment gotchas (the ones that silently kill SSE):** flush per event; `Cache-Control: no-store`; `X-Accel-Buffering: no` (nginx-family buffering); **no compression on stream responses**; terminate at an HTTP/2-capable ingress (HTTP/1.1's 6-connections-per-origin limit is the one real SSE constraint; over h2 it vanishes). These land in `docs/runbooks/ingress.md` and are asserted by an integration test that reads two events through the compose proxy.

**Debuggability as a feature:** `curl -N "$BASE/v1/tasks/$ID/events?since=0&token=$TOK"` is the canonical first look at any task — flight recorder in a terminal. Reserved alignment: the `to_ag_ui()` adapter (§8.6) emits over this exact framing, since AG-UI's primary transport is SSE.

## 5.6 The command surface — the CLI, fully specified

The CLI is how the project is *interfaced, validated, and demonstrated* — a thin client of §5.2's routes (never the DB), and the instrument every milestone's real-world validation runs through. The complete tree:

```
computer
├── run api|worker                     # the two run modes (one image, §3)
├── login | logout | whoami            # session auth; keys via $COMPUTER_API_KEY
├── do "<goal>" [--space S] [--mode fleet] [--clarify auto|never|always]
│               [--wait|--no-wait] [--assert-verified] [--budget-usd N]
├── tasks   list | show ID | events ID [--follow] [--since N] | usage ID [--by family]
│           steer ID "msg" | pause ID | resume ID | cancel ID
│           move ID [--space S|--personal] | follow-up ID "<goal>"
├── gates   list | resolve ID (--accept [-a "1: b, 2: yes"] | --decline | --cancel)
├── inbox   list [--unread] | read ID | read --all
├── spaces  list | create NAME | show ID | usage ID [--month YYYY-MM]
│           members ID (add EMAIL --role editor|viewer | rm ACTOR | role ACTOR ROLE)
│           transfer ID --to EMAIL
├── capture add "text" [--space S] | list | promote ID
├── artifacts show ID | download ID [-o PATH] | verify ID
├── skills  list | pull NAME | push NAME | test NAME [--scratch "goal"]
│           publish NAME | versions NAME
├── models  list | add PROVIDER:ID --classes … --price … [--evaluate] [--enable] | enable ID | disable ID
├── agents  list
├── admin   routing (get|put FILE) | prices (get|put FILE)
└── dev     frame-selftest | gateway-ping --class C ["prompt"] | seed
```

**Interface rules (rock-solid, enforced in review):** every command supports **`--json`** (machine-readable output — logs and scripts consume it; human output is pretty tables/streams); exit codes mean things — `0` success, `1` error, `2` denied/authz (and a usage error: click exits 2 on a bad flag or argument), `3` timeout-waiting, `4` conflict (a stale steer, `client.EXIT_CONFLICT`); `tasks events --follow` is the **terminal flight recorder** (SSE under the hood — Lane 1 always, Lane 2 rendered live with `--live`); `do --wait` streams narration and events into the terminal as the task runs (the demo surface); destructive commands confirm unless `--yes`; `--api URL` and `$COMPUTER_CLI_API_URL` select the target; secrets never echo. **Every feature's real run is expressed in these commands or raw `curl` — if the CLI can't demonstrate a feature, the feature isn't done.**

---

# 6. Authentication — the module

**Design:** email + password (argon2id) → **opaque, revocable session tokens** stored hashed in Postgres (`cs_` prefix, 32 random bytes, TTL from conf) — JWT rejected deliberately: statelessness defeats the kill switch and logout. Programmatic access uses **API keys** (`ck_` prefix), also hashed, shown in plaintext exactly once at creation. Both resolve to the same **Actor** — humans, agents, and services live in one identity system, so attribution never forks.

**Forgotten passwords (ADR-031):** `POST /v1/auth/forgot-password` answers 202 whether or not the email exists, mints a single-use `cr_` code (same 32-byte `secrets` family, sha256 at rest in `password_resets`, TTL 15 min from conf, re-request supersedes, ≤5/user/hour durable throttle) and mails it via `computer/mailer` — `console` backend (dev default) prints the whole email to the API stdout, `smtp` (stdlib, STARTTLS) for anything shared. `POST /v1/auth/reset-password` claims the code atomically (the `cn_` UPDATE…RETURNING shape), re-hashes with argon2id, **revokes every live session and every live API key**, and sends the courtesy "your password was changed" notice; invalid/expired/replayed/superseded are one indistinguishable 401. Sends run off the request path (background task; a failed send un-mints its row so a dead relay never spends the cap), and the console backend is refused at boot in staging/prod (ADR-031 amendments, 2026-08-17). The rows are the audit trail (`created_at`/`consumed_at`/`superseded_at`, swept hourly after a 30-day grace); Logfire lines carry ids, never codes (`cr_` joined the scrub pattern). CLI: `computer forgot-password` / `computer reset-password`; web: `/forgot-password` + `/reset-password` off the login card.

**Models (principal + satellite):** `Actor` is deliberately **thin** — id, kind `human|agent|service`, display_name, scopes, status — attribution and RBAC only; it is what every `*_id` in the system points at. Humans get a satellite: `User` (actor_id pk/fk, email unique, name, avatar_url, timezone, preferences JSONB, deleted_at for GDPR erasure) — the thing that logs in, has a profile, and appears in Settings. `Credential` (user_id pk/fk, password_hash, updated_at) and `AuthSession` (id, token_hash, user_id, expires_at, revoked_at, user_agent) belong to **Users**; `ApiKey` (id, key_hash, actor_id, name, last_used_at, revoked_at) belongs to **Actors**, so agents and service accounts can hold keys later without a migration. Fat-model methods: `User.signup(session, email, name, password)` — one transaction creating Actor(kind=human) + User + Credential · `AuthSession.issue(user) -> plaintext` / `.resolve(session, raw) -> Actor` (hash, expiry, revocation; returns the underlying Actor) · `ApiKey.issue(actor)` / `.resolve(...)` — `last_used_at` bumps **at most once per minute** (throttled; attribution reads must never become hot writes). **Boot sync:** worker startup upserts one `kind=agent` Actor per `ai/agents` registry entry (name = registry name), so machine attribution is uniform with human attribution from day one.

**`require_actor` (deps.py):** parse `Authorization: Bearer <token>`; dispatch by prefix — `ck_` → ApiKey path, `cs_` → AuthSession path; anything else → 401. WebSockets can't set headers from browsers, so the stream accepts `?token=` (documented; tokens are hashed at rest and never logged — Logfire scrubbing pattern added). CLI: `computer login` (email/password → stores session token in `~/.computer/credentials` chmod 600) or `COMPUTER_API_KEY` env.

**Operations (`operations/auth.py`):** `signup` (gated by `COMPUTER_AUTH_SIGNUP_ENABLED`; first signup becomes admin-scoped), `login` (constant-time verify; fixed-window in-process limiter — 10/min per email+IP — since replaced by durable buckets in `operations/throttle`), `logout` (revoke), `create/list/revoke_api_key`. Admin scope guards `/admin/*` (scope check in `require_actor`'s Actor.scopes).

**The boundary vocabulary (developer-facing):** `GET /v1/auth/me` returns a `UserOut` (id, email, name, timezone, preferences) — never an "ActorOut"; `GET /v1/agents` returns `AgentOut`s; keys are "API keys." The only place external payloads say `actor` is the event/audit envelope — `actor: {type: "user"|"agent"|"api_key", id, name}` — the term developers already know from GitHub-style audit APIs. Internally everything still joins on one `actor_id`; the split is presentation and lifecycle, not attribution.

A Space is deliberately **project-simple** — think of Claude's Projects: a folder that organizes Tasks. Tasks are created anywhere ("out of thin air") and moved in, out, or between Spaces at any time; membership and budgets are what the folder adds, nothing more.

**Spaces — ownership & sharing (the authorization model).** One owner, stored on the row: `spaces.owner_actor_id` — "one space owner exists" is a **column, not a convention**; transfer is an explicit owner-only endpoint. Sharing = `space_members` rows (`space_id, actor_id, role editor|viewer, added_by, added_at`, pk `(space_id, actor_id)`); the owner is never duplicated into the table. Effective role: `Space.role_of(actor)` = owner if the column matches, else the membership role, else `None`; **`Space.get_for(session, id, actor, min_role="viewer")` is the ONLY space fetch path** and raises NotFound below `min_role` — existence is never leaked. The matrix: **viewer** — read the space, its tasks, their streams/events, artifacts, and usage; **editor** — viewer + create tasks in the space, promote captures into it, cancel/steer space tasks, resolve their gates; **owner** — editor + PATCH (name/budget), member management, transfer. Task visibility follows: a spaceless Task is visible only to its creator; a space Task is visible to all members; `Task.get_for` checks creator-or-membership, and consequential actions on space tasks (cancel, gate resolve) require editor+. **Artifacts: a Space contains artifacts *through its Tasks*** — `GET /v1/spaces/{id}/artifacts` is a derived query joining via `tasks.space_id`, because publish remains the only door (Rule 9) and provenance keeps exactly one parent chain; there is no artifact→space FK. Adding a member creates a `space_shared` InboxItem for them; members may remove themselves; `list_spaces` returns owned + member-of. Deliberate Phase-1 deferrals: space-wide gate routing, email invitations to non-users, a persistent membership audit log beyond Logfire spans.

**Moving Tasks (`POST /v1/tasks/{id}/move`, body `{space_id: UUID | null}`).** The task's creator moves their own task; a destination space requires editor+ there; `null` moves it back to personal. Allowed in any status — a running task simply falls under the destination's budget at its next Bursar check. Emits a `task.moved` event (from → to) on the stream. **Ledger law:** `usage_events` are attributed at time of spend and are never rewritten — spend before the move stays where it happened; spend after accrues to the new space; `/tasks/{id}/usage` is unaffected (it aggregates by task). Visibility follows the FK instantly: into a shared space, members can now see it; out, they can't. Deferral: owner-eject (a space owner moving someone else's task out to their personal) is Phase 2.

**Why owner/creator FKs target `actors` (and how "user" stays true).** `users.actor_id` is a PK/FK onto `actors` — the two ids are the *same value* — so the only question is which table the constraint references. We reference **actors**, with the human requirement as a fat-model invariant (`Space.create` demands `kind=human`, i.e., a User row exists): every FK in the system keeps pointing at one spine, and the agent-parity future (a standing role owning its workspace) becomes a one-line policy change instead of a migration. At the boundary, the user instinct wins completely: serializers present `owner` and members as **user objects**, and `add_member` takes an **email** + role (resolved through `users.email` → actor). The database speaks actor; the product speaks user; they are the same row. Recorded as **ADR-007** — deferred-then-decided choices get an ADR precisely so they are never relitigated.

---

# 7. Domain keystones (stable since v4; normative)

**Fat model pattern** (`db/models/task.py`): status `_ALLOWED` map + `transition()` raising `InvalidTransition`; `next_seq()`; `get_for(session, id, actor)` as the ONLY fetch path (eager-load, **space-aware scope per the §6 sharing matrix**, NotFound); `list_for(...)` keyset on `(created_at, id)`. Companions: `TaskEventRow.append` (seq + insert + `pg_notify`) · `Gate.resolve` · `PlanStepRow.frontier` · `Artifact.attach_verification` · `SpaceUsageDaily.bump`. **All queries are classmethods on models; no `select()` outside `db/models/`.**

**Engine seam** (`ai/agents/base.py`): frozen `AgentDef(name, capability, output_ref, tools, skills, prompt, verifier_suite, exclude_family, budget, description)` + `register()` into `REGISTRY` + `build(name, deps, **overrides)` → `gateway.resolve` (Metered+Fallback) → warden-wrapped toolsets → skill index + pinned bodies → `DBOSAgent(Agent(..., output_type=outputs.get(...)))`. Adding an agent = one file + one prompt + one import in `all.py`; it's live in the registry, the Planner vocabulary, `GET /v1/agents`, and the parametrized CI test.

**Operation template** (`operations/execution.py`): fetch via fat models → `StepSpec.model_validate(row.spec)` → `TaskDeps.acquire` → `ai.agents.build(...).run(..., usage_limits=...)` → event append in a transaction → return a `schemas` value, never rows.

**Jobs** (`jobs/run_task.py`, `run_step.py`): exactly the v4 loops — intake → plan → frontier fan-out on `step_queue` → durable joins → replan-as-edit → deliver (+inbox); gate park via `recv`; verify → retry (≤3, targeted, gotcha-recorded) → sign; escalate-with-diagnosis (+inbox). Job bodies: zero I/O; `effects.py` = thin `@DBOS.step` sheet → operations; big payloads by id.

**`.importlinter`:** layers `runner / api / jobs / operations / ai : features / db / schemas : conf`; engine-layers inside `computer.ai` (`agents : council / deps / gateway : frames : skills : tools`); forbidden `api ↛ ai` and `ai ↛ db|operations|jobs|api`.

---

# 8. The orchestration engine — from prompt to delivery

The definitive lifecycle. Everything here is normative; the §7 fat-model `_ALLOWED` map implements exactly the machine below.

## 8.1 The state machines

```
TASK   created ──▶ clarifying? ──▶ planning ──▶ running ◀──▶ waiting ──▶ delivered
                        │              │            │  ▲        │
                        ▼              ▼            ▼  │        ▼
                    cancelled       failed       paused ┘   cancelled/failed
STEP   pending ▶ ready ▶ running ▶ verifying ▶ done | retrying(n≤3) | escalated | skipped
```

`waiting` = parked on any gate (question, approval, budget) at ~zero cost. `paused` = user-initiated: dispatch stops at the next frontier boundary (running steps finish), resume re-enters the loop. `clarifying` occurs **at most once**, before planning. Transitions outside this map raise `InvalidTransition` — the map is the spec.

## 8.2 Questions — asking the user (three-action, MCP-elicitation-aligned)

A **QuestionSet** is what an `input` gate carries: `title`, `context` (why we're asking), `expires_at`, `on_timeout ∈ {proceed_with_defaults | wait | cancel}`, and an ordered list of Questions — `id`, `prompt`, `type ∈ {text | number | boolean | single_select | multi_select}`, `options [{value, label, description?}]`, `required`, `default?`, `allow_other`. **Flat fields only, primitives + enums** — deliberately matching MCP elicitation's restricted schema so any MCP-capable client renders our questions natively; the canonical rendering is **numbered (1..N)** on every surface, and chat/CLI surfaces accept the plain-text grammar `1: b, 2: yes, 3: skip`.

**Three origins:** (a) **intake clarification** — the Planner asks everything at once, one round maximum, numbered, governed by `TaskCreateIn.clarify ∈ {auto | never | always}` (auto = only when ambiguity is material); (b) **planned decision gates** — the Planner places them ahead of time ("pick the voice from these three samples"); (c) **mid-run `ask_user`** — any top-level step agent may raise questions (§8.3). Questions ship as **one batch per gate — never drip.**

**Resolution — one endpoint, three actions** (verbatim from the MCP elicitation model): `POST /v1/gates/{id}/resolve {action: "accept" | "decline" | "cancel", content?}`. *accept* → `content` is validated against the QuestionSet schema (422 with `fields` on mismatch); *decline* → proceed with defaults where they exist, else escalate per the set's policy; *cancel* → cancel the task. Approval gates are the degenerate case (`content = null`) — one model unifies questions and approvals. On resolve: answers recorded on the gate row, `gate.resolved` carries them on the stream, and they are written to **`/task/answers.json`** in the frame and injected into the prompts of dependent steps. Timeouts: a reminder InboxItem fires at TTL half-life; at expiry, `on_timeout` applies. URL-mode elicitation (OAuth, payments — interactions that must not pass through us) is the reserved Phase-2/3 stub, aligned with the spec's URL mode.

## 8.3 Mid-run `ask_user` — suspend and resume, durably

`ask_user(questions)` is an **external deferred tool** (the Pydantic AI pattern): the agent calls it, the tool defers, and the run **ends** with a `DeferredToolRequests` output. The execute effect then: persists the run snapshot (message history, stored by id), inserts a dynamic `input` gate step via a normal replan-edit, and the job parks on `DBOS.recv` — minutes or days at zero cost. On resolve, execution **resumes the agent with the original message history plus `DeferredToolResults`** keyed by tool_call_id; from the model's point of view the answer simply arrives as the tool's return value. Phase-1 simplification with teeth: `ask_user` is granted to **top-level step agents only** — sub-agents escalate needs through their parent's contract (nested deferral bubbling is a known library edge; if it bites, ADR-006). The same suspend/resume spine serves Phase-3 approval-required tools (`requires_approval=True` → `ToolApproved | ToolDenied`) — mandates land on rails that already exist.

## 8.4 Steering, pause, cancel, follow-up

**Steer** (`POST /v1/tasks/{id}/steer {message}`): the user talks to a running task — "actually, use euros." Emits `task.steered`; the kernel merges it **only at the next frontier boundary**, where the Planner turns it into a criteria amendment + plan edit (history preserved). Steers never interrupt a running step; multiple steers coalesce into one edit. **Pause / resume** (`POST …/pause`, `…/resume`): pause stops dispatching at the boundary — running steps finish, status → `paused`, cost → zero; resume re-enters the loop. **Cancel:** running steps receive a soft stop, the frame snapshots then destroys, status → `cancelled`; the ledger keeps everything spent (append-only, always). **Follow-up** (`POST …/follow_up {goal}`): creates a **child task** (`tasks.parent_task_id`) inheriting the space, with the parent's artifacts mounted **read-only at `/task/inputs/`** and the parent's criteria + delivery summary in context — continuing is cheaper than starting fresh, by construction.

*Amended 2026-10-02 (Phase 1c): ADR-064 replaced this paragraph's steering rule. The steer endpoint now writes a `steers` row and `plan.edited{steer}` (`task.steered` is removed), and steers and pauses apply at safe points inside a running step, not only at the frontier boundary; cancel stays the only hard stop.*

## 8.5 The walkthrough — one task, end to end

1 `POST /v1/tasks` → row + `task.created` → job `task-{id}` enqueued (after commit; sweeper guards the gap). 2 **Clarify** (≤1 round): numbered QuestionSet gate → `waiting` → answers → `/task/answers.json`. 3 **Plan**: criteria + plan graph (agents by registry name, skill pins, decision gates placed) → `task.planned`. 4 **Dispatch loop**: frontier → budget check (Bursar; space budget if any) → parallel step jobs on the queue. 5 **Execute**: `build()` agent runs in the frame; tools journal; usage meters (`usage.recorded`); mid-run `ask_user` may park it. 6 **Verify**: type suite in-frame + cross-family judge. 7 **Retry** (≤3, targeted, gotcha recorded) or **escalate** with a diagnosis (+ inbox). 8 **Sign** → `verification.signed`; publish → harvest → `artifact.produced`. 9 **Steers** merge at boundaries; replans are edits with history. 10 **Join** parallel branches; loop until the frontier is empty. 11 **Deliver**: summary written for a human who wasn't watching + artifacts with verification reports + reusable-asset offers → `task.delivered` + inbox. 12 **Follow-up** continues the thread as a child task. Every arrow above is a `TaskEvent` on the stream; the flight recorder is `ORDER BY seq`.

## 8.6 The live surface — two lanes, deltas, gaps, formatting

**Two lanes, one socket.** Lane 1 — **the record**: durable `TaskEvent`s, per-task `seq`, persisted, replayable (everything defined so far). Lane 2 — **the wire**: ephemeral delta frames tagged `lane: "live"`, AG-UI-shaped, keyed by `step_id` + channel id and anchored with `after_seq` (the last durable seq) so clients order them against the record. Lane-2 frame types: `message.start/delta/end` (agent narration), `artifact.delta` (text-artifact preview as it is written), `progress` (phase, counters, elapsed — frontend-only, never fed back to the model). **Never persisted per-frame.**

**The reconciliation rule (the crux).** Every Lane-2 stream terminates in Lane-1 durability: `message.end` → one durable `step.narration` event carrying the full markdown; artifact deltas → the published artifact itself (content-addressed) plus durable `artifact.snapshot` checkpoints every ~8 KB during long writes. Reconnect is therefore boring: replay Lane 1 (`since=seq`) → render finals/snapshots → live deltas resume. Deltas lost in a disconnect never matter, because their terminal state is durable — the snapshot+delta pattern mapped onto our durability split.

**Gaps — the no-dead-air rule.** While a step runs, the client sees an honest signal at least every **10 seconds**, sourced in priority order: model tokens (`message.delta`), tool journal entries (`tool.called`, Lane 1), frame exec output tails (`progress`), and — if all are silent for 10 s — a synthesized `progress {phase: "thinking", elapsed, tokens_so_far}` from the meter. First Lane-2 frame within **2 s** of step start. The 15 s transport heartbeat remains the floor beneath all of it.

**The formatting contract.** Streamed text is **markdown, append-only**: never HTML (XSS), never retro-edited mid-render — a correction is a new message, not a rewrite of streamed history. Rich interactions (question forms, artifact cards, verification reports) render from **typed events**, never from magic strings inside markdown. Artifact previews by type: text-like artifacts (md, code, csv heads) stream content deltas via throttled `frame.write_file`/publish middleware; **binary artifacts never stream bytes** — an xlsx streams facts (sheets written, rows, elapsed) and renders on harvest; web apps expose a preview URL only after their smoke test.

**Mechanics + the honest ceiling.** Model tokens flow from pydantic-ai streaming through the execute effect's emitter to the hub; the coalescer bounds Lane 2 to **≤10 frames/s per channel, ≤4 KB per frame** — under Postgres's NOTIFY payload limit, so Lane 2 rides the same single LISTEN/NOTIFY fan-out as Lane 1 (boring beats clever). On SSE, only Lane-1 frames carry `id:` — Lane-2 never advances `Last-Event-ID` (§5.5). The hub sits behind an interface: a dedicated pub/sub can replace it at scale without touching one emitter. Reserved, not built: a thin `to_ag_ui()` adapter mapping both lanes onto AG-UI event types, making any AG-UI frontend a free client — Pydantic AI already ships the integration, so the adapter is mostly renaming.


---

# 9. Model agnosticism — a new model is one line, not a deploy

Models ship weekly; the system must absorb them at data speed. Three tiers, by decreasing frequency:

**T1 — new model, known provider (the weekly case; e.g. xAI ships `grok-5-fast`).** One CLI line, zero deploys, < 2 minutes to first routed token:

```
computer models add xai:grok-5-fast \
  --classes fast-executor,frontier-reasoner \
  --family xai --context 256000 \
  --price input_token=0.20/M output_token=0.80/M \
  --evaluate
```

This calls `POST /v1/admin/models`: inserts a `ModelCatalogEntry` (provider, model_id, family, classes, context, base_url?, enabled) + `PriceBook` rows, and — because xAI speaks the OpenAI-compatible protocol — the gateway's generic provider serves it immediately; `resolve()` builds chains from **enabled catalog entries** per class, so the model flows into routing on the next request. `--evaluate` runs the 20-case smoke suite through the new model per declared class and prints scores before you `--enable` it (default: added disabled; enabling is a second explicit line or `--enable`). Repricing later changes new `usage_events` only (price_version audit).

**T2 — new provider, OpenAI-compatible API (DeepSeek, Ollama, vLLM, most startups).** Add `COMPUTER_GATEWAY_<NAME>_API_KEY` (+ optional base_url) to env, then T1's line with `--base-url`. Still zero code.

**T3 — genuinely novel SDK (rare).** One adapter function in `ai/gateway/providers.py` + a §3 row + ADR. Nothing else changes: catalog, routing, metering, evals all operate on the adapter's output.

Supporting rules: `providers.py` contains **constructors per protocol** (anthropic, openai, google, openai_compatible), never per-model logic; `gateway/catalog.py` is the DB-backed inventory with hot reload; capability classes are the only vocabulary application code speaks (Rule 7); the Bursar prices whatever the catalog declares. The comfort guarantee, verbatim in the docs: *"When a model drops at 9:00, it's routed, metered, and smoke-scored in your Computer by 9:05 — and it never touched a deploy pipeline."*

---

# 10. The artifact pipeline — how the sandbox makes things

The frame is the computer; this is the contract for how work inside it becomes a deliverable. A step whose agent needs no computer stages its files on paper, an in-memory frame with the same publish door and manifest (ADR-063, amended 2026-11-06).

**Layout (created on `frame.create()`):** `/task` (plan.md + criteria.json — the agent's own map) · `/work` (scratch: clones, notebooks, intermediates — wiped after harvest) · `/artifacts` (publish target — the ONLY harvestable directory) · `/skills` (loader target). No default network route; only the egress proxy enforcing per-task `allow_domains`; every `exec` journaled as `tool.called`.

**Publish (the only door):** agents produce files in `/work` using the toolchain (openpyxl / python-docx / python-pptx / reportlab / pypdf / pandas / matplotlib — all pinned in the image; ADR-021 corrected this line's earlier pandoc/node aspiration to what actually ships), then call the tool:

```
frame.publish(path="/work/model.xlsx", type="spreadsheet",
              title="3-statement model", sources=["/work/sources/10k.pdf", "https://…"])
```

`ai/frames/publish.py` validates the extension against the **artifact-type registry** (`schemas/outputs.py`: type → extensions, verifier_suite, smoke policy — `spreadsheet/.xlsx→spreadsheet-suite`, `document/.md .docx→document-suite`, `deck/.pptx→deck-suite`, `code|webapp/dir→code-suite (tar + the artifact's own test.sh in a fresh frame — the planned Playwright smoke never shipped, ADR-021)`, `dataset/.csv .parquet→dataset-suite`, `council_opinion|council_report→council-suite`), computes sha256, copies into `/artifacts/`, and appends a `manifest.json` entry `{path, type, title, sha256, sources, agent, model, skill_pins, at}`. Publishing twice with the same path = new manifest version, old bytes retained in `/work` history until harvest.

**Harvest (effect, after step success):** read `manifest.json` → upload each entry to MinIO content-addressed → insert `Artifact` rows with the manifest entry as provenance → emit `artifact.produced`. **Verify:** the type's suite runs **in the frame against the published bytes** (recompute cells from `sources`; rebuild code in a fresh frame; re-extract document numbers), then the cross-family judge, then Ed25519 signing → `verification.signed`. **Deliver:** presigned links via `PUBLIC_BASE_URL`-constructed routes. Worked micro-example: modeler writes xlsx in `/work` → `frame.publish` → harvest → spreadsheet suite recomputes every formula cell and cross-foots → judge checks acceptance criteria → sign → the download carries the verification report.

Snapshots: `frame.snapshot()` tarballs `/task /work /artifacts` → MinIO at every step boundary; retry-after-crash restores the last snapshot, which is why 12-hour tasks survive anything.

---

# 11. Skills — user-editable, fast loop: draft → test → publish

Skills are **data, not deploys.** The registry is DB-backed (`skills_registry`: name, version, status `draft|published|archived`, origin `builtin|user`, manifest JSONB, body in MinIO by content hash, eval_status, created_by); the twelve builtin skills in `skills/` are **seeded as v1 at migrate** and are editable by anyone via new versions (origin tracks provenance; builtins are never mutated in place).

**The loop (target: edit-to-running in under a minute):**

1. `PUT /v1/skills/report` with the markdown body (or `POST /v1/skills` for a new one) → creates the next **draft** version. Runtime enforcement at write time: description ≤50 words with "Load when…" phrasing, index entry ≤100 tokens, body ≤5K tokens, `evals/cases.yaml` present — violations 422 with `fields`.
2. `POST /v1/skills/report/test` → runs the skill's routing cases + end-to-end case via pydantic-evals (and optionally `{"scratch_goal": "…"}` to run one real scratch Task with the draft) → returns the report inline.
3. Use before publishing: a Task may reference `"report@draft"` — resolvable **only by the draft's author** — so you can trial it on real work without affecting anyone.
4. `POST /v1/skills/report/publish` → allowed only with green evals (the doctrine is an HTTP 409, not a review comment) → new published version.

**Pinning (reproducibility):** `draft_plan` resolves every skill ref to a concrete version at plan time and stores the pins in each `StepSpec` (and in artifact provenance). Publishing affects **new tasks only** — running tasks keep their pins, exactly like PriceBook repricing. The loader fetches (name, version) bodies from the registry with a local content-addressed cache, then copies into the frame — hot by construction, no restart anywhere.

**CLI loop:** `computer skills pull report` (writes `./skills-work/report/`) → edit → `computer skills push report` (new draft + auto-test, prints the report) → `computer skills publish report`. **Authoring doctrine (enforced + taught):** description = routing trigger in the user's frustrated vocabulary with at least one "Not for…"; body = judgment, not tutorials (the sentence test); gotchas are append-only and the highest-value section; heavy reference → `references/`; scripts invoked, not pasted; cross-family behavior checked by `test` automatically. **Custom agents doctrine:** one `AgentDef` file; narrow output schema; minimal tool grants; declare `verifier_suite`; `exclude_family` only with a reason; one real-model eval case; agents are roles ("maker"), never models.

---

# 12. Model glossary (these lines ARE the docstrings)

**Actor** — the thin attribution principal (kind `human|agent|service`, display_name, scopes, status); everything attributable traces to one; named in external payloads only inside event/audit envelopes. · **User** — the human satellite of an Actor: email, profile, preferences, GDPR lifecycle — the thing that logs in. · **Credential** — a User's argon2id password hash. · **AuthSession** — a User's revocable login token (hashed) with expiry. · **ApiKey** — an Actor's revocable programmatic token (hashed), shown once; `last_used_at` throttled to once/minute. · **Space** — a project-simple folder for Tasks: optional, owned by exactly one person (`owner_actor_id`, human-invariant), shareable; Tasks move in/out/between freely; the boundary for budgets, rollups, and flags; contains artifacts through its Tasks. · **SpaceMember** — one shared-access grant: (space, actor, role `editor|viewer`); the owner is the column, never a row. · **Task** — a unit of work a user submits; plans into Steps; delivers signed artifacts; at most one Space; may have a parent (a follow-up inherits space + artifacts). · **PlanStep / PlanEdge / PlanEdit** — a plan node with contract, state, attempt / a typed dependency / an append-only replan record. · **TaskEvent** — one append-only, per-Task-sequenced state change; flight recorder and wire format. · **Artifact** — a content-addressed deliverable created only by publish, carrying its manifest provenance. · **Verification** — the signed report attached to an Artifact. · **Gate** — a designed human interrupt (`approve | input | budget`) that parks a job until resolved by the three-action model; `input` gates carry a numbered QuestionSet with expiry and an `on_timeout` policy. · **Capture** — a soft intake record, promotable into a Task. · **InboxItem** — a notification to an Actor, read/unread. · **UsageEvent** — one provider call's native units + write-time cost; the money ledger. · **PriceBook** — versioned unit prices per provider/model/unit. · **SpaceUsageDaily** — the (space, day, kind) rollup. · **ModelCatalogEntry** — one available model: provider, id, family, classes, context, base_url, enabled. · **SkillRecord** — one skill version: draft/published/archived, origin, manifest, eval status. · **RoutingTable** — versioned per-class chain ordering over enabled catalog entries. · **Feature** — a flag scoped to a Space or global. · **Routine** — standing work (ADR-048): name, instruction, a When (`trigger_kind` + spec), behaviours (overlap/catch_up/notify/posture, caps, seasons), a REQUIRED per-run budget; owner human, Space-scopable; active \| paused \| retired, never deleted. · **RoutineRun** — one fire decision, append-only: `(routine_id, occurrence)` unique is the exactly-once keystone; decision names every non-fire's reason; the outcome lives on the minted task. · **RoutineEventCursor** — the event matcher's durable watermark (one row).

# 13. Database (Alembic 0001)

`actors users credentials auth_sessions api_keys spaces space_members tasks plan_steps plan_edges plan_edits task_events artifacts verifications gates captures inbox_items usage_events price_book space_usage_daily model_catalog skills_registry routing_table features` (+ later arrivals: `bots_registry plugin_installs system_beats memories memory_uses connectors password_resets admin_actions routines routine_runs routine_event_cursor`; + DBOS system tables, untouched). `task_events` pk `(task_id, seq)`, app-side seq, `pg_notify`, BRIN `at`. Indexes: `tasks(space_id)` `tasks(status)` `tasks(parent_task_id)` `tasks(created_at,id)` `plan_steps(task_id,state)` `gates(state)` `inbox_items(actor_id,read_at)` `captures(status)` `usage_events(space_id,at)` `usage_events(task_id)` `users(email)` (unique) `space_members(actor_id)` `auth_sessions(token_hash)` `api_keys(key_hash)` `skills_registry(name,status)`.

# 14. Guardrails (must NOT do)

No business logic in views or serializers · views only `create_/list_/get_/update_/cancel_/resolve_/mark_` named functions in resource files · view signatures use the `ActorDep`/`SessionDep`/`PageDep` aliases — raw `Depends(...)` in a view signature is a violation · no `select()` outside `db/models/` · no ORM objects to agents, prompts, or responses · no agent construction outside `ai/agents/base.build()` · no `db`/`operations` imports under `ai/` · no dependency without a §3 row + ADR · no `create_all` · no model strings outside the two provider files (catalog rows are data, not strings-in-code) · no URL literals outside conf/compose/env/providers · no usage aggregation outside `operations/usage.py` + rollup · no adapter without a `UsageEvent` · no plaintext tokens/passwords at rest or in logs (hash at the door; Logfire scrub patterns for `cs_`/`ck_`) · no JWT · "actor" never in external API responses outside event/audit payloads · `api_keys.last_used_at` bumps throttled to ≤1/min · no second task↔space FK/join table/back-reference (membership is actor↔space — a different relation) · space budget, members, and transfer are owner-only · moving a task never rewrites `usage_events` (attribution is at time of spend) · artifacts are never parented to a Space directly (the space artifacts view is a query over its tasks) · Capture runs nothing · Inbox notifies only · a fleet is a plan shape (ADR-010) · questions ship as ONE numbered batch per gate (never drip), with at most one intake clarification round · every QuestionSet carries expiry + `on_timeout` · answers validate against the QuestionSet schema (422 on mismatch) · `ask_user` is granted to top-level step agents only (sub-agents escalate via their contract) · steering merges only at frontier boundaries — cancel is the only mid-step interrupt · Lane-2 deltas are ephemeral (never persisted per-frame) and every delta stream terminates in a durable final or snapshot · streamed text is markdown, append-only — never HTML, never retro-edited · binary artifacts never stream bytes (progress facts only) · on SSE, only durable Lane-1 frames set `id:` (Lane-2 never advances `Last-Event-ID`) · stream responses are never compressed or proxy-buffered · artifacts only via publish · draft skills usable only by their author · a user-visible change without one real run (`just smoke` or the CLI flow) is red (pytest alone never proves one) · every CLI command supports `--json` · no Phase-2+ scaffolding beyond named stubs · **nothing thrown away** (replacement = removal + replacement + reason, same commit).

**URL topology** (the first build's environment list held it; `backend/tools/audit.sh` checks it): every URL is a setting, and literals live only in conf/compose/env/providers. `PUBLIC_BASE_URL` constructs all links (ws:// by scheme swap). Object storage has two endpoints on purpose: an internal one for uploads, a public one for presigned downloads. Frames route only through the egress proxy. The keys themselves are `backend/.env.example` and `backend/prismoid/conf.py`.

# 15. Performance — budgets, practices, harness

Budgets (p95, compose baseline; `tests/perf/`; nightly gate fails >20% regression): list endpoints (50 rows) **<100 ms** · `POST /v1/tasks` **<150 ms** · `POST /v1/capture` **<50 ms** · `POST /v1/auth/login` **<200 ms** (argon2 dominates; parameters tuned and documented) · event append→WS delivery **<250 ms** · submit→first event **<1 s** · submit→`task.planned` **<5 s** · gate resolve→job wake **<500 ms** · first live delta after step start **<2 s** · no-dead-air: an honest signal every **≤10 s** while a step runs · frame cold start **<2 s** · space usage MTD **<120 ms**. The standing lane (ADR-048): the minute sweep's due-scan at 1 000 active routines **<50 ms** (the `ix_routines_due` partial index doing its job) · fire→`task.created` **<5 s** end-to-end, of which the in-process mint path (gate + task + receipt, one tx) is asserted directly in `tests/perf/test_routine_budgets.py` (measured p95 ~11 ms; the queue-latency remainder rides the smoke drill). The long-form lane (ADR-025/026, W=12): outline **<90 s** · outline→sections-on-stream **<2 s** · per-section (~2k words) **<5 min** · assembly md **<3 min**, docx/pdf incl. rendition + figures **<8 min** · QA pass **≤300 s** hard, cost share **≤5%**, pre-checks **≤10 s** at 110 pp · end-to-end 30 pp **<25 min** · 60 pp **<40 min** · 110 pp **<75 min** · full event replay (5k frames) **<2 s**. Size buckets (ADR-029, per kind): extended document 15 pp end-to-end **<20 min** · extended deck (~25 slides, sectioned + md_to_pptx) **<30 min** · extended workbook (~6 sheets, md_to_xlsx) **<25 min** · brief/standard one-shots unchanged from their kinds' existing budgets · size resolution itself is pure arithmetic, **<1 ms** in planning. The consensus plane (ADR-028): consensus proposals run PARALLEL — outline phase at `quality=max` **≤1.5× the single-outline wall** (merge included) · harmonize wave **<3 min** at width (fully parallel; ~2× section tokens, the lane's cheapest phase) · panel QA **adds no wall-clock** over one read (concurrent seats) and stays inside the QA cost share · a retry-turn outline (full thread context) gets **<6 min**, the fresh-task 90 s row unchanged (chaos-drill benchmark finding). Practices: uvloop+httptools · ORJSONResponse · warmed asyncpg pool · eager-load + N+1 tripwire tests on every list · keyset pagination only · one process-wide LISTEN task (both lanes; Lane-2 coalesced ≤10 frames/s/channel, ≤4 KB/frame — under the NOTIFY ceiling; hub behind an interface) · long-lived `httpx.AsyncClient` per provider · stream, don't buffer (flush per event; `X-Accel-Buffering: no`; no compression on stream routes; h2 ingress) · ruff ASYNC rules · §13 indexes in 0001 · Logfire spans as the perf dashboard (queries in `docs/runbooks/dashboard.md`). Harness: `tests/perf/test_budgets.py` (pytest-benchmark) + `locustfile.py` (70/20/10 mixed load, 50 users) + `just perf`; `perf.yml` nightly vs `PRISMOID_PERF_BASELINE_PATH` (the committed `backend/tests/perf/baseline.json`).
