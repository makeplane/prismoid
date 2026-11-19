# ai/skills - folder rules
Scope: `backend/prismoid/ai/skills/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
The skill file format and what built-in skills declare. It parses and reads; `operations/skills.py` writes
versions and owns publish (a 409 unless evals are green).

## What is here
- `format.py`: the one door - `SKILL.md` (frontmatter + body) and `cases.yaml` become a `ParsedSkill`
  (`parse`, `parse_dir`). Strict mode enforces the house doctrine (a "Load when" and a "Not for", the body
  and index budgets, at least one routing case); the import door (`strict=False`) checks only the Agent
  Skills spec, and publish re-checks the doctrine.
- `deliverables.py`: the table the built-in format skills declare (`produces`, `format`, `capability`,
  `frame-profile`) - which skill builds which file, on which class and image. It reads only
  `backend/skills/` on disk, so a pushed skill never changes what a step runs.
- `loader.py`: reads a body by its sha256 through a local cache; the engine's one object-store read.

## Where a thing goes
- A new frontmatter key or doctrine rule: `format.py`, so every door obeys it.
- Not here: a built-in skill's files -> `backend/skills/`; a version row or publish -> `operations/skills.py`;
  copying a body into a frame -> `operations/`.

## Rules
- Route every way in (seed, push, plugin import) through `parse` / `parse_dir` - one validator, one answer.
- Keep bundled files to `scripts/`, `references/`, `assets/` and a root LICENSE, within the byte and count caps.
- Return bytes from the loader; `operations/` owns the frame and copies them in (a sibling of `frames`
  cannot reach one).
- A skill's `allowed-tools` grants nothing; a step runs a pinned skill only when its agent holds every
  declared tool (`agents/grants.skill_fits`).
- Never serve bytes that do not hash to their own name: a corrupt cache entry is fetched again, never
  returned.
- Never write to the store from here.

## Checked by machine
- `.importlinter`: `skills` is a bottom-line sibling of `gateway | frames | tools | bots`.

## Verify
`uv run pytest tests/unit/ai/skills -q`; the doors end to end: `uv run pytest tests/integration/bots -q`;
a skill's routing: `just evals`.
