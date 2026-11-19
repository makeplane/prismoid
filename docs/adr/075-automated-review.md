# ADR-075 · Automated review: the ratchet, the rules files, the reviewer

**Status:** accepted · 2026-10-30 (numbered 070 until #30 and the Propel stack landed first) · the owner's ask of 2026-10-29 ("make code reviews completely
automated... code has to be written like a really good engineer"). Evidence and the restructuring it
enables: an internal structure review of 2026-10-29 (unpublished).
**Amends** nothing; adds no dependency (HANDOFF §3 unchanged).

## Context

A structure review of the whole tree (2026-10-29) found the code did not get more convoluted per line
since 2026-09-27; it got concentrated and copied. Features landed as one more branch in the same few
hubs (`execute_step` 622 -> 965 lines, complexity 63 -> 99; `TaskChat` 898 -> 1,324 lines), the same
idea was hand-written up to 28 times, vocabularies stayed strings, and tests reached into privates
7x more often. Nothing stopped it: `just check` guarded layers and a list of greps, but not size,
complexity, duplication or placement, and no review step read the rules the root `CLAUDE.md` states.

## Decision

Three layers, cheapest first. Each catches what the one below cannot.

1. **The ratchet (deterministic, blocking, in `just check`).** `backend/tools/quality.py` measures
   per-symbol metrics across backend, web and sandbar and compares them with a checked-in baseline
   (`backend/tools/quality_baseline.json`) of today's offenders:
   - Caps for new code. Python: complexity 15, 80 statements, 3 nested blocks, 5 positional
     arguments, 800-line files, 20-line API endpoints. Web (`web/app` and `packages/blocks/src`): complexity 15, depth 3, 300-line
     functions, 800-line files, no comment disabling a measured rule. Go: 600-line files, 60-line functions. Comments citing review or
     planning artifacts: 0 per file. Tests: private-access ignores may not grow.
   - A symbol over its cap fails unless the baseline lists it at the same or a higher value. A symbol
     that moved (same name, new file, gone from the old file) inherits its baseline entry. `just ratchet` rewrites the
     baseline downward only; `just quality` prints the before/after table a PR quotes. `check` fails
     until `just ratchet` records moves and improvements (the baseline must be tight), and a renamed
     file is paired with its old path through git's renames (`git mv`, staged or committed).
   - Ruff, oxlint and the Go source are read with tools already in the tree; nothing new is installed.
2. **Laws (deterministic, blocking).** `backend/tests/laws/` is the one home for tests that enforce a
   standing rule; each file names its rule and derives its population from code. The four existing
   rule tests move there, joined by: every folder rules file is mirrored by an `AGENTS.md` symlink and
   cites only ADRs and checks that exist; `.offset(` never appears (keyset pagination only); a
   pydantic-ai `Agent(` is constructed only in `ai/agents/base.py` and the two model drills
   (`ai/gateway/smoke.py`, `runner/dev.py`); server frameworks (`starlette`, `fastapi`) are imported
   only under `api/` and `runner/` (`operations/screen.py` allowlisted until reduction-plan 2.3); a
   test patches a name in the module that defines it.
3. **Rules files and the reviewer (judgement, advisory).**
   - `docs/engineering.md` states the craft principles with short examples and names the files that
     show the house style.
   - Each important folder has a `CLAUDE.md` (with an `AGENTS.md` symlink, the root's convention):
     what the folder is for, Do / Never rules quoted from the root rules and the review, what is
     already checked by machine, and the exemplars to copy. Coding agents load it when they touch the
     folder; the reviewer reads it. The root `CLAUDE.md` stays the master and is not shortened here.
   - A local reviewer: the `/review` Claude Code command (`.claude/commands/review.md`) and
     `just review` (headless). It diffs the branch against `main`, routes changed files to one
     reviewer per area (`.claude/agents/review-*.md`), reads rules files **from `main`** so a branch
     cannot loosen its own review, and has a separate verifier confirm each finding before it is
     reported. It skips style, anything a check catches, pre-existing issues and nits; a rule
     violation quotes the rule. It never blocks; it reports, ranked, and offers to fix.
   - A Claude Code `PostToolUse` hook formats and lints each Python file an agent edits.
   - `.github/pull_request_template.md`, adapted from Plane's template.

## Consequences

- New code meets the caps from day one; existing debt shrinks only through planned refactors, and
  every PR shows its delta.
- The review runs locally on the developer's own Claude Code login: no API key, no CI cost, and no
  comments a human has not seen. Moving it to CI later is a workflow file, not a redesign.
- The caps are starting values. After the planned refactors' second wave they tighten.
- Out of scope here, each its own follow-up: the planned refactor waves; integration tests in CI;
  a golden-PR set that measures the reviewer's recall and precision; PR title/size checks in CI;
  moving area paragraphs out of the root `CLAUDE.md` into the folder files.
