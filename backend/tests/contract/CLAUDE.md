# backend/tests/contract - folder rules
Scope: `backend/tests/contract/`. Read with `tests/CLAUDE.md` and the root CLAUDE.md (they win on conflict).

## What this folder is for
The record of a task's turn (`task_events`) is the contract between the engine and every reader: the
web, the CLI, the verifier, a resumed workflow. A scenario here runs one real turn - the real
`run_task` body, every effect, the planner, the step executor, the verifier's suite script, Postgres -
with only the edges faked (scripted models, the durable queue run in process, a fake Machine), and
holds the record to `shapes/<name>.json`. A refactor that changes no behaviour leaves every file
alone; that is its proof. It covers the seams the AI layer changes: the Lead's reply, the Lead's plan, a plan of waves,
steering, the gates, a parked step resumed, a sweep, a fleet. Routine fires and forks are not here.

## Layout
- `harness/`: the machinery that runs a scenario and reduces its record (`rig`, `voices`, `people`,
  `answers`, `moves_lanes`, `world`, `resume`, `shape`). A harness change is engine-neutral; it
  never edits a shape.
- `scenarios/`: the scenarios, as data, one file per family: `all.py` (a reply, a plan, waves, and
  the list every test reads), `steering.py` (a person's hands on the turn), `gates.py` (questions,
  approvals, connects, the budget, the resume fixtures), `lanes.py` (follow-ups, bots, receipts,
  sweeps, fleets).
- `shapes/` and `fixtures/`: the recorded data. `test_*.py` at the top: the tests.

## Run
- From `backend/`, against a fresh database: `PRISMOID_TEST_FRESH_DB=1 uv run pytest tests/contract -n 6`.
  Name the folder: `--record-shapes` is defined in this folder's conftest, which pytest reads only then.
- One scenario: `-k <name>`. The shape rules alone (no database): `uv run pytest tests/contract/test_shape_rules.py`.

## Do
- Add a scenario as data in its family file under `scenarios/`, then
  `uv run pytest tests/contract --record-shapes -k <name>` and read the file it wrote. Does the
  record tell the story the scenario means? Commit file and scenario together.
- Script only what the agent can really do: `calls` fails on a tool the agent was not given and
  `says` on an agent that cannot answer in text (a writer `writes`: its `submit_document` call, which
  the system publishes).
  Script exactly the replies the turn asks for: a reply left unused, or one refused (even if the
  engine swallows it), fails the run. The machinery (`rig.MACHINERY`) repeats its one reply.
- Record the engine as it is. A shape is event types, their order, the few fields that are structure
  (route, route source, step state, tool outcome, gate kind) and the plan's edges from `plan_edges`
  (the planner's `task.planned` carries none). Never text, ids, times, sizes or costs.
- Add a row to `shape.KEYS` when a scenario's record holds an event type it does not know; the
  harness refuses to guess. An event whose place in the order is timing (it lands when a background
  model call ends) goes in `shape.LOOSE`.
- State an accepted variance in `ALLOWED_VARIANCE` in `test_event_shapes.py`, with the reason, in the
  change that makes it; the recorded file stays the record of the engine before. A changed
  `route_source` passes only that way.
- A gate is answered as data: `at_park(accepts(q1="board"))` gives the nth park its answer through the
  real operation, and `lapses` runs the real sweep at a `now` past the gate's expiry (never wait for a
  clock). A park is woken only by a wake sent to its own workflow id; one sent elsewhere fails the
  run. A scenario that needs a spend or a connected service sets `world=`.
- A scenario about more than one turn lists `given` (rows the ask names: a published bot) and `then`
  (moves after the first turn: a follow-up, the receipts the queue holds), all in `harness/moves_lanes.py`.
  What a move wants held beside the record it puts in `run.facts`; ids in them are made nameless.
- A resume fixture (`fixtures/`) keeps the rows a park left; `--record-shapes -k resume_` parks the
  scenario again and rewrites it. Re-record only for a change of how a park is saved, on purpose.
  The budget gate has none: it parks `run_task` itself, which only the durable engine's journal resumes.
- Run the scenarios against a database migrated to head with no stack running (`just test` refuses
  otherwise). The shape rules in `test_shape_rules.py` need no database.

## Never
- Put a word a model wrote into a shape.
- Wait on the wall clock. A turn finishes when its effects do; a step runs when the queue starts it.
- Edit a recorded file by hand to make a test pass. Re-record, and let the diff be the review.
- Add a catalog row under a fixed name: `judge_seat` takes a model id of its own, so no other test
  reads or collides with it.
- Define `db`, `actor` or `client` here; `tests/integration/conftest.py` holds them.

## Known gaps
- Parallel waves (`scatter`'s seats, a fleet's first wave) interleave their siblings' rows by timing,
  so the shape groups a wave's rows by step (`shape._by_sibling`). Which sibling ran first is not
  protected.
- `steering.apply_planned` (a steer the plan itself carries) is not covered: `steer_while_planning`'s
  steer lands after planning read the epoch, and the boundary re-plan applies it.
- The verifier's suite runs for real (`rig._SuiteFrame`); a receipt holds the suite's own verdict
  and checks beside the signed one, which is the judge's call when the suite examined nothing.
- A call a steer holds journals `tool.called` (under its catalog name, `frame.read_file`); a call a
  pause defers journals nothing, so the two "chosen, never run" calls leave different records.
- The durable queue runs inline, so no effect journal is replayed: a replay hazard is invisible here.
- Model metering is faked with the models: only `world.price` puts the real meter in front of a voice.

## Review focus
- A shape file that changed without a scenario or a change of engine behaviour to explain it.
- A scenario that scripts more than the turn needs or leans on a field a refactor may rename.
- A new `KEYS` field that is text in disguise.
