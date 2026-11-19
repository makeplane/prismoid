# ai - folder rules
Scope: `backend/prismoid/ai/`. Narrows the root CLAUDE.md and never contradicts it; craft is docs/engineering.md.
Each subfolder with its own CLAUDE.md narrows this one.

## Purpose
The engine: agents, their runs, tools, sandboxes and model calls. It is persistence-free: values in,
values out, so it runs and is tested with no database.
Rows, transactions, jobs, HTTP, selection, budgets and permissions live in `operations/`, which composes it.

## Where a thing goes
- An agent (a role): `agents/<name>.py` + `agents/prompts/<name>.md` + one import in `agents/all.py`; its
  tools are its row in `agents/grants.py`. Checklist: `agents/CLAUDE.md`.
- Per-run behaviour (a hook around a run's model requests or tool calls, with state per run): a pydantic-ai
  capability in `capabilities/<concern>.py`, reaching a run through a `MOUNTS` row in `agents/mounts.py`
  or `AgentDef.capabilities`, never a branch in `build()` (ADR-090).
- A pure decision both hooks and operations apply (budget, plan rules, approval rules): `rules/<concern>.py`.
  Frozen values in, a verdict out, no I/O, no hook.
- Text a model reads that is not a tool (memory, thread, step brief, turn notes, citations, inlined files):
  `context/`.
- A tool: `tools/<family>.py`, named only from `tools/catalog.py`. Checklist: `tools/CLAUDE.md`.
- Checking a deliverable: `verification/`. Models, routing, metering, prices: `gateway/`.
  Sandboxes and frame drivers: `frames/`. File formats (parse, validate, render): `skills/`, `bots/`, `plugins/`.
- Ports operations fills: `deps.py`. A `TaskDeps` field is a port or a task fact with its reason on the line
  above it, never state a run changes (that is a capability's own field).
- Settings: `config.current()` at call time (`config.use_settings` in a host or a test). `config.py` is the
  only module that imports `prismoid.conf`, so a host can steer every setting through one door.
- Not here: a row, a transaction or a job -> `operations/`; a shared value -> `prismoid/schemas/`.

## Layers
`.importlinter` "engine-layers"; a higher line imports a lower one, never the reverse:
`agents` > `capabilities` > `rules` > `deps` > `plugins` > `verification` > `context` >
`gateway | frames | skills | tools | bots`. `config` is the settings door any layer may import.
- Siblings on one line never import each other.
- When `just layers` refuses a placement, move the code - an `ignore_imports` line hides the cycle it found.

## Rules
- Never import `prismoid.db`, `prismoid.operations`, `prismoid.jobs` or `prismoid.api` - the engine must run without them.
- Speak `ModelClass`, never a model id - a model id arrives as catalog data. The only model literals are the
  search engines' defaults in `tools/providers/search.py`, the directory the audit skips by name.
- Ask what a wire protocol can do through `gateway/providers.FEATURES` - one table, so a new provider is one row.
- Let `ToolRecorder` (`capabilities/tool_record.py`) write every call's row; a tool says its own row
  (`tools/call_rows.say`) only when the row depends on how the call went.
- Decide whether a call needs a person's yes only in `rules/approvals.py` (rules as data in
  `prismoid/schemas/approvals.py`) - one home, so the connector and browser doors cannot disagree.
- Run only a step attempt on the call log (ADR-089); unit names are pinned per `LOG_VERSION`, so a rename
  lands with a version bump (see `capabilities/CLAUDE.md`).
- Keep secrets out of frames, prompts and fixtures; URL literals only where `backend/tools/audit.sh` allows them.
- Make a kill switch select data (a schema, a list, a row), never a second copy of a function.
- Give an optional capability its own Protocol, never a member raising `NotImplementedError`.
- Say it in words wherever a model, a person or a bot or skill author reads: no `ADR-n`, no section marks.

## Quality (reviewers block on these)
- Placement by the table above. Size caps are `quality:py.file_lines` and `quality:py.statements`; a long
  job is named phases.
- One way per concept (one extractor, token counter, retry policy, tool-name table, `Agent(` site); the
  third copy gets one home in the same change.
- No pass-throughs: a helper earns its name (entry point, two callers, a table entry, a real phase).
- Typed values: three parameters travelling together are a frozen dataclass; `dict[str, Any]` only at
  edges; `None` means one thing; a closed set is a `StrEnum` or `Literal`.
- Every `except` says fail-closed (security, money, unattended) or fail-soft (a person is waiting); never
  swallow `CancelledError`.
- Prompts are named `##` blocks, one concern each, said once; model input is ordered by a stable key.
- One concept, one stem (file, class, function, span, test): run `rg -w <name> backend/prismoid` first.
- Tests check behaviour at the seam and mirror their module's path. Deleted means deleted: module, tests,
  law text and ADR line together.

## Checked by machine
- `.importlinter` (layers, persistence-free, one settings door); `backend/tools/audit.sh` (literals).
- `tests/laws/test_law_agent_construction.py` (one constructor, one run loop), `tests/laws/test_agent_registry.py`,
  `tests/laws/test_law_tool_record.py`, `tests/laws/test_law_tool_catalog.py`,
  `tests/laws/test_law_call_log_scope.py`, `tests/laws/test_law_model_features.py`,
  `tests/laws/test_law_routed_seats.py`, `tests/laws/test_law_intent_patterns.py`,
  `tests/laws/test_law_plain_words.py`, `tests/laws/test_law_prompt_budget.py`,
  `tests/laws/test_no_em_dash.py`, `tests/laws/test_law_rules_files.py`.
- `just quality`: `quality:py.complexity`, `quality:py.nesting`, `quality:py.positional`,
  `quality:py.local_imports`, `quality:comments.process_refs` beside the size caps; offenders in
  `backend/tools/quality_baseline.json` only shrink.

## Verify
- `uv run pytest tests/unit/ai -q` and `uv run pytest tests/laws -q` (from `backend/`); `just check` before a commit.
- A prompt or agent change: `just evals` too - it is not part of `just check`.

## Copy this
`capabilities/tool_record.py` (one choke point), `gateway/resolve.py` (one home per decision).
Critical in review: a provider call without a `UsageEvent`, a tool path around `ToolRecorder`, a
`web_fetch`/`web_search` refusal that raises or lets a query string reach a journal or a label.
