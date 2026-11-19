# ai/bots - folder rules
Scope: `backend/prismoid/ai/bots/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
The bot file format (ADR-045). A bot is an Agent Plugins v1 directory: `plugin.json` at the root, with
`so.plane.prismoid/bots/<name>/BOT.md` and `cases.yaml` below it. This folder validates and renders the
format; it compiles nothing and stores nothing.

## What is here
- `format.py`: the one door - `parse`, `parse_dir`, `bots_in`, `render_bot_md`, `render_cases`, and
  `manifest_refusal`, the one list of rules a manifest must keep, read by the push door's 422 and by the
  compiler's refusal of a stale row.

## Where a thing goes
- A new manifest rule: one refusal in `format.py`, joined into `manifest_refusal`.
- Not here: compiling a bot to an `AgentDef` (narrowed to its base agent's hands) ->
  `ai/agents/bot_compile.py`, which sits higher; the `plugin.json` writer -> `ai/plugins/format.py`;
  drafts and publish (green selection evals, else 409) -> `operations/bots.py`.

## Rules
- Validate structured `job` / `use_when` / `not_for` and compose the description through
  `schemas.bots.compose_description`, so every bot's routing text has one shape.
- Take tools from `BOT_TOOLS` only and capability from `BOT_MODEL_CLASSES` only - the judge and image
  classes are reserved.
- Return refusals as `BotError(fields=...)` in words an author can act on.
- Never change `NAMESPACE`: every published bundle is filed under it.

## Checked by machine
- `.importlinter`: `bots` is a bottom-line sibling of `gateway | frames | skills | tools`.

## Verify
`uv run pytest tests/unit/ai/bots tests/unit/ai/agents/test_bot_compile.py -q`; the doors end to end:
`uv run pytest tests/integration/bots -q`.
