# ai/context - folder rules
Scope: `backend/prismoid/ai/context/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
The text a model reads that is not a tool, as pure wording over values. `operations/` owns selection,
budgets, token counting and the database, and passes values in (`operations/context.py`,
`operations/thread.py`, `operations/execution/inline.py`, `operations/turn/intake.py`).

## What is here
- `blocks.py`: remembered context (the Context Pack's lanes), today, and what a cloud Machine can reach
  (`MACHINE_REACH`).
- `thread.py`: the thread transcript.
- `brief.py`: the step brief - contract, inputs, connectors, last failures.
- `inline.py`: files a brief or the Lead's prompt carries in (attachments fenced as data, skills as guidance).
- `turn.py`: the tail of a Lead run's message - the turn's facts and the re-plan note - and `ENDING_TOOLS`,
  the one table naming each Lead ending's output tool.
- `citations.py`: the pages a citing brief lists by source id, and the published text with each `[S#]`
  mark linked; the mark grammar itself (`cited_ids`, `relink_citations`) stays in `ai/tools/research.py`.
- `retry.py`: the note a continued attempt reads.

## Where a thing goes
- New model-read text that only renders: a function here over a frozen value.
- Not here: a size cap, a token count or which file is read -> `operations/`; text-only behaviour that
  hooks a run -> `ai/capabilities/`; a tool's own description -> `ai/tools/`.

## Rules
- Write each block as a pure function of a frozen value (`ContextPack`, `ThreadTranscript`, `StepSpec`,
  `LeadFacts`) - same input, same bytes, so the cached prefix holds.
- Say in the framing whether a block is instruction or evidence. The project charter, skill notes and the
  thread are instruction; remembered notes and outside text (a file, a page) are data.
- Fence data with `prismoid.schemas.egress.fenced`, imported directly with no run nonce - a nonce would change
  the bytes per run and break the cached prefix.
- Put stable text first and per-turn text last; order every list by a stable key, never by arrival.
- Keep the thread append-only, so turn K's render is a byte-prefix of turn K+1's.
- Do no I/O: no database, frame, network, or clock read a caller could pass in. The one exception is
  `blocks.py` reading the system tz tables once, cached.
- Never import a layer above this one: `ai/verification`, `ai/plugins`, `ai/deps`, `ai/rules`,
  `ai/capabilities`, `ai/agents`.

## Checked by machine
- `.importlinter` engine-layers: `context` sits under `verification` and over
  `gateway | frames | skills | tools | bots` (`just layers`).
- `tests/laws/test_law_intent_patterns.py`: this folder is a strict home (no `re`, no word tests).
- `tests/laws/test_law_rendered_tool_references.py`, `tests/laws/test_law_no_dataset_phrases_in_prompts.py`,
  `tests/laws/test_no_em_dash.py`.
- `tests/unit/ai/agents/test_request_snapshots.py`: a wording change lands with its
  `tests/snapshots/<agent>/request.txt` diff.

## Verify
`uv run pytest tests/unit/ai/context tests/unit/ai/agents/test_request_snapshots.py -q`.
