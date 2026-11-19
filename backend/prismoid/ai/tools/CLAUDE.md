# ai/tools - folder rules
Scope: `backend/prismoid/ai/tools/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
The functions a model calls, one module per family, and the small helpers every family shares. A toolset
takes callables and values that operations wired - never `TaskDeps`, never a row.

## Families
- `frame_tools.py`: `read_file`, `write_file`, `edit`, `shell`, `publish` on the step's frame; `edits.py` is
  the match ladder behind `edit`.
- `fetch.py`: `web_fetch`, the only way an agent reads a URL (ADR-046); `body.py` reads its response under
  the byte cap; `reading.py` reads a fetched page for a goal; `convert.py` runs markitdown in an rlimited subprocess and never fetches.
- `research.py`: `find_in_page` and `note` over the pages a run already read, and the `[S#]` mark grammar
  (`cited_ids`, `relink_citations`).
- `providers/search.py`: `web_search`, the only search-wire file - engines, keys, prices and the chain
  (`search_chain`, `search_price`) as config (ADR-047). `providers/images.py`: `generate_image`, the only
  image-API file.
- `browser.py`: the operator's browser verbs; `browser_policy.py`: their decisions, pure.
- `connectors.py`: the only MCP client code - a person's servers, mounted and probed (ADR-020).
- `ask.py` (`ask_user`, `request_connector`: deferred, no I/O), `proposals.py` (`propose_task`),
  `routines.py` (`routine_create`), `chat.py` (card verbs), `skills.py` (`skill_read`), `memory.py`
  (`memory_search`): each calls a closure operations wired; they propose, never arm.

## Shared (the bottom of the folder)
- `catalog.py`: every tool once - model name, journal name (`journal_name`), `SideEffect`, `Taint`, `mints`.
- `call_rows.py`: the `tool.called` row port and the history predicates over it.
- `labels.py`: what a journal summary or live label may say (`label_for`, `terminal_line`).
- `fence.py`: the run's nonce on data fences. `output.py`: head-and-tail truncation. `pin.py`: resolve once,
  dial the vetted address. `spans.py`: tracing without URLs or queries. `wall.py`: no tool waits past the
  run's deadline.
- Not here: text a model reads that is not a tool -> `ai/context/`; a run hook -> `ai/capabilities/`;
  whether a call asks a person -> `ai/rules/approvals.py`.

## Rules
- Derive every spelling of a tool's name from `catalog.py` - a hand conversion drifts on the next rename.
- Journal and label argument keys, host and path - never values, typed text or a query string, which may
  carry what a person typed.
- Worker-process network tools (fetch, search, images, connectors) bypass frame egress by design; the
  grant, the parse-time vet (`schemas/egress`), the pin and the journal are the policy. A refusal is a
  result the model reads, never a raise.
- Ask a door's approval callable before an act; an ask raises `ApprovalRequired` before the call runs.
- Return outside text inside a data fence (`fence.fenced`), with its `Taint` declared in the catalog, so
  the run's watermark rises.
- Write a tool's own row only when it depends on how the call went (which engine served, a refusal
  returned as text): `call_rows.say`, never the journal port directly.

## A new tool
1. A `catalog.py` `TOOLS` row: name, journal name, `SideEffect` (drives approvals, steering re-runs and
   Machine acquisition), `Taint` when the result is not Prismoid's own text, `mints=True` when a call writes
   rows that outlive the task. A tool missing here cannot mount.
2. A row in `ai/agents/grants.py` per agent that holds it; nothing mounts ungranted.
3. Its mount: a frame tool in `frame_tools.py`; any other family is a `Family` in `ai/agents/mounts.py`
   `MOUNTS`, serving its catalog names.
4. Its call-log unit names join `tests/fixtures/operation_name_pins.json`; renaming or removing one appends
   a section and bumps `LOG_VERSION` in the same change (ADR-089).
5. The request-snapshot diff in `tests/snapshots/<agent>/request.txt` (ADR-090).

## Checked by machine
- `tests/laws/test_law_tool_catalog.py`, `tests/laws/test_law_tool_record.py` (one row per call),
  `tests/laws/test_law_rendered_tool_references.py` (a prompt names only tools its reader holds),
  `tests/laws/test_law_no_dataset_phrases_in_prompts.py` (tool descriptions included).
- `tests/unit/ai/tools/test_operation_name_pins.py`, `tests/unit/ai/capabilities/test_tool_record.py`.
- `just layers`, `just quality`; `backend/tools/audit.sh` (URL and model literals only under `providers/`).

## Verify
`uv run pytest tests/unit/ai/tools -q`.
