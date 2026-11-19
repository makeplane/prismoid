# ai/capabilities - folder rules
Scope: `backend/prismoid/ai/capabilities/`. Narrows ../CLAUDE.md, ../agents/CLAUDE.md and the root CLAUDE.md;
never contradicts them.

## Purpose
Per-run behaviour only: pydantic-ai capabilities that hook a run's model requests and tool calls and keep
their state per run (built per build, or a fresh instance from `for_run`). A capability decides facts - tool
results, page refs, spend - never what the person meant.

## What is here
- `tool_record.py`: `ToolRecorder`, one `tool.called` row per call; answers OUTCOME UNKNOWN for a call the
  log saw start and never end.
- `durability.py`: `PrismoidDurability`, a step attempt on its call log, and `LOG_VERSION` (ADR-089).
- `steering.py`: `Steering` applies steers at safe points (ADR-064) and owns the run's borrowed-connector
  flag (`Steering.borrows`) the approval rules read.
- `loops.py`: `RunGuard`, the stall, held-call and budget brakes.
- `grant_fence.py`: pushes the device frame's current grants before each tool dispatch (ADR-078).
- `machine_acquire.py`: moves a step on paper to its Machine before the first call that needs the
  computer, and replays logged writes onto fresh paper on a re-entered run; the swapping frame itself is
  `OnDemandFrame` in `frames/on_demand.py`.
- `taint.py`: `RunTaint` raises the run's taint watermark from each request and derives the run's fence nonce.
- `compaction.py`: keeps history under the model's window - spill, clear (a browser seat's old pages
  collapse, `collapse_old_pages`), then a handoff the summarizer writes in a logged call.
- `output_checks.py`: a step's `Outcome` checked in-run (proof, `acceptance` lines in `Outcome.criteria`,
  the last act); a miss is a `ModelRetry`.
- `citations.py` (`CitationCheck`), `record_sources.py` (`RecordSources`): a cited or recorded source id
  must be one the run's reads were given.
- `forced_answer.py`: tools off on the last request a step's ceiling allows, so the run ends in an answer.
- `lead_check.py`: holds a Lead run to its allowed endings (`LeadEndings`) and to `rules/plan.py`
  (`PlanCheck`); a broken plan goes back twice, then a one-step fallback plan stands; the plan is priced
  through `gateway/run_cost.py`.

## How a capability reaches a run
`build()` mounts, outermost first: `ToolRecorder`, `RunTaint`, the `MOUNTS` families' capabilities,
`AgentDef.capabilities`, `OutputChecks`, `PrismoidDurability`. `MOUNTS` (`ai/agents/mounts.py`) adds
`Steering`, then `RunGuard`, then `GrantFence` - so a steer holds a call before a brake sees it, and a held
call never asks - plus `MachineAcquire` and `Compaction`, each inert without its port.

## Where a thing goes
- A new per-run behaviour: one module named for the concern, never a mechanism (`hooks`) or a grab bag
  (`utils`).
- Not here: a pure decision with no hook -> `ai/rules/`; a frame -> `ai/frames/`; text the model reads ->
  `ai/context/`; a run-loop helper -> `ai/agents/`.

## Rules
- Log a read a replay must see again: a `@durable_operation(name=...)` on a capability with a pinned `id`
  (`Steering.id = "steering"`, operation `inbox.pending`) - an unlogged read differs on replay.
- Pin every unit name (agent, toolset id, tool, operation) per `LOG_VERSION`: a new name joins the current
  section of `tests/fixtures/operation_name_pins.json`; a rename or removal appends a section and bumps
  `durability.LOG_VERSION`, because a renamed unit misses its logged row and runs again.
- Read what a run needs from `TaskDeps` ports; keep run state on the capability, never on `TaskDeps`.
- Write a held call's row through `tools/call_rows.say`, so the recorder writes it once.
- Share a safe-point helper two capabilities need from one home (`tools/call_rows`, `tools/ask`) - copies
  drift apart.
- Never import `ai/agents` (the roster sits above this layer).
- Never match the person's words with `re`, `fnmatch` or a word list - intent is the model's.

## Checked by machine
- `.importlinter`: `capabilities` sits under `agents` and over `rules`.
- `tests/laws/test_law_tool_record.py` (one row per call), `tests/laws/test_law_call_log_scope.py` (only a
  step attempt runs on the log), `tests/laws/test_law_intent_patterns.py` (this folder is a strict home;
  `loops.py` is counted against its baseline instead).
- `tests/unit/ai/tools/test_operation_name_pins.py`: the pins equal the real roster and mounts.

## Verify
`uv run pytest tests/unit/ai/capabilities tests/unit/ai/tools/test_operation_name_pins.py -q`.
