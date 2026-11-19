# ADR-090 · Capabilities are the extension unit

**Status:** accepted · 2026-11-14 · amended 2026-11-15 (the field rule in §2)

## Context

Agents grew by flags. Each new per-run behaviour (the run guard, steering, the browser page fold)
added a field to `AgentDef` or a branch in `build()`, and prompts were whole-file strings that one
hash test guarded, so a wrong answer could not be traced to the text that was in force.

## Decision

1. **An agent is data.** `AgentDef` holds its instruction blocks (the prompt's `##` sections), its
   model class, its grants (read from `ai/agents/grants.py`), the capabilities it adds and its
   output type. `build()` in `ai/agents/base.py` stays the only constructor.
2. **A new per-run behaviour is a pydantic-ai capability** in `ai/capabilities/<concern>.py`: hooks
   around model and tool calls, its state per run. It reaches a run one of two ways: a tool family's
   mount in `ai/agents/mounts.py` (every seat it applies to), or `AgentDef.capabilities` (one agent).
   Never a branch in `build()`. A typed `AgentDef` field holds only a fact read before the run's
   capabilities exist: by `build()` (its model class, `endings`, `effort`, a lane part's `modes`, a
   format maker's `makes_formats`) or by operations seating its step (`cites`: the task's sources load
   for it); what a run does with that fact is a capability (`cites` reads ids, `CitationCheck` checks
   them). `TaskDeps` carries ports and task
   facts (`keep_page`, `ending_checks`, `unattended`), each added with its reason stated; state a
   run keeps lives in a capability.
3. **Prompts are named blocks.** Every `ai/agents/prompts/<role>.md` opens on a `##` heading; each
   block holds one concern and at most 1,600 characters. `build()` renders the blocks stable first
   (the identity in `prompts/_identity.md` on every seat that carries it, the role, the device law
   on the device) and records their ids on the run as `prompt_version`.
4. **The model's input is a reviewed file.** `tests/snapshots/<agent>/request.txt` is each roster
   agent's first request through the real `build()` (instructions, tools, output tools; run ids and
   the clock stripped). A prompt or tool change lands with its snapshot diff, re-recorded by
   `uv run python -m tests.unit.test_request_snapshots`. The same test holds the cache-prefix
   invariant: the static prefix carries no run id, no date and no per-turn text.

## Consequences

- A behaviour is added, removed or tested as one class; `build()` does not grow.
- The whole-prompt hashes in `tests/unit/test_device_law.py` are removed; the snapshots replace them.
- A block heading costs a few words of every request; the prompt budget counts them.

## Alternatives considered

- **Keep flags on `AgentDef`.** Rejected: each flag is a branch in `build()` and a field every agent file
  reads past.
- **Keep the golden hashes.** Rejected: a hash says that the text moved, never what moved.
