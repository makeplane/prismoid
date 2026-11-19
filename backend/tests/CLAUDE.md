# backend/tests - folder rules
Scope: `backend/tests/`. Read with the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The backend's tests; [README.md](README.md) maps the tiers and how to run each. `unit/` needs no
stack, mirrors `prismoid/` folder by folder and runs in `just check`; `integration/` needs the compose
stack (Postgres, Silo), is grouped by product domain and carries the `integration` marker; `conformance/` holds the
contract every frame driver passes; `laws/` holds one file per standing rule (ADR-075, see its
README); `perf/` holds the §15 budgets; `fixtures/` and `snapshots/` hold data; `support/` holds the shared
test code (`fakes/`, `rigs/`, plain helpers); `contract/` holds the turn-record
harness (its own CLAUDE.md); `drills/` holds the call log's durability drills (marker `drill`, deselected by
default; run `uv run pytest tests/drills -m drill` with the stack up). `conftest.py` wires the engine per
session and the time-windowed cleanup. It is NOT a library: test modules do not import each other.
A new test goes in the folder that mirrors its code (unit) or its domain (integration), with a
basename no other test file uses; every folder but `perf/` has an `__init__.py`.

## Do
- Test behaviour through public names. A decision is tested as a pure function with rows and
  `now` as arguments; if the only way in is a private, promote the decision first.
- Inject what varies (the clock, the sleep, the upstream, the lease) and wait on events, never
  on wall-clock windows (`test_screen_relay.py`'s `drain()`).
- Patch a name in the module that defines it, or where the code under test looks it up; when a
  move retargets a name, the patch moves in the same commit.
- Mark a test that needs the stack `integration`, and put a frame-spinning file in its
  `xdist_group` so frame tests serialize onto three workers.
- Clean up only what the test created, by time window, so a run never destroys a developer's
  data (the `conftest.py` lesson).
- Put a new standing rule in `laws/` as one file: its docstring names the rule, its population is
  derived from code (registry, metadata, routes, AST), never a hand list, and a test proves it
  bites. Literal greps go in `backend/tools/audit.sh`; import rules go in `.importlinter`.
- Run `just test` (with the compose stack up) before a workstream merges; prompt or agent
  changes also run `just evals`.

## Never
- Import another test module or `tests.conftest`; shared test code goes in a plain module
  under `tests/support/` (`cast.py` holds `member`; stand-ins in `fakes/`, a family's setup in
  `rigs/`), defined once.
- Build a path from `Path(__file__).parents[N]`; use `tests/support/paths.py` (`BACKEND`, `REPO`).
- Define a `db`, `actor` or `client` fixture in a test file again (27, 20 and 14 copies today);
  if `tests/integration/conftest.py` lacks the fixture, add it there, never
  in the test file.
- Add a `reportPrivateUsage` ignore; the budget may not grow. A private patched or imported in
  three or more test files is a seam to promote.
- Sleep on the wall clock and then assert; inject the clock or wait on the event instead.
- Run the suite against a live dev api or worker; `just test` refuses (`no-stack-guard`).
- Push a test file past 800 lines; split it by unit under test.

## Checked by machine
- `quality:tests.private_access`: the count of `reportPrivateUsage` ignores (359 today) plus every
  other escape (520 in all) may not grow; a file-level mode below strict (`# pyright: basic`,
  `# pyright: standard`, in any operand position) or `reportPrivateUsage=false` directive, a bare
  `# pyright: ignore` or a `# type: ignore` in any form counts as one too (pyright reads no rule
  list on `# type: ignore[x]`: it silences the whole line).
- `quality:py.file_lines`: 800 lines per file; 21 test files are baselined (a planned
  split shrinks them). Renaming one: `git mv` it, then `just ratchet` re-keys its baseline.
- `quality:comments.process_refs`: no ruling, review or plan citations, task ids or dated incidents in test comments either.
- `tests/laws/test_law_patch_where_defined.py`: a patch on a re-export nothing reads fails.
- `tests/laws/test_law_rules_files.py`: every folder rules file cites only what exists.
- `just test-unit` runs `tests/unit` and `tests/laws`; `just test-integration` runs
  `integration` and `conformance` against the stack.

## Write it like this
- `unit/operations/machines/test_machine_decisions.py`: "No I/O - rows and `now` are arguments", through public
  names in `prismoid.schemas.machines`.
- `unit/operations/test_screen_relay.py`: injected fakes (upstream, leases, clock, sleep) and `drain()`
  instead of wall-clock waits, the method explained in the docstring. Copy its fakes, not its
  size: it is one of the 21 baselined files over 800 lines.
- `laws/test_tenancy_map.py`: a law asserted over the real model metadata, not over text.
- `conftest.py` (`scratch`): cleanup scoped to the test's time window, with the reason.

## Review focus
- A test that passes without testing: a patch on the wrong module, an assertion inside a branch
  that never runs, a mock that returns what the assertion expects.
- A new private import or patch where a public seam would do, especially `execution._*` or
  `turn.replan._run`.
- A copied fixture or helper that already exists elsewhere in the suite.
- A law with a hand-maintained list instead of a population derived from code.
- A test that depends on order, on the wall clock, or on another test's rows.
