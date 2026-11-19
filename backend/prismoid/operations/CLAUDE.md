# operations - folder rules
Scope: `backend/prismoid/operations/` (the engine ladder in `execution/` adds its own file). Read with the
root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The verbs. Operations compose rows (`db/models`), values (`schemas`) and the engine (`ai/`) into
one unit of work, and they own its transaction. One module serves one noun (`stream_tickets.py`,
`projects.py`, `routines.py`). It is NOT a transport (HTTP and WebSocket speak live in `api/`),
not a query layer (queries are fat-model methods in `db/models/`), and not a home for rules
another module needs to ask (those go to a leaf).
- `turn/`: the Lead's turn (ADR-092). `intake.py` reads a turn's facts from the rows once and decides
  purely which turns a code-made plan answers and which endings the Lead may take; `commit.py` stages
  each ending's rows in the step's completion transaction; `replan.py` runs the Lead's replan seat at
  a frontier boundary after a steer.
- `grounding.py`: a document receipt's claims, read once by `claim_extract` against the stored sources
  and checked by code (`ai/verification/grounding.py`); fail-soft.
- `deadline.py`: a leaf; a model call's hard deadline, cancelled and abandoned at the wall.

## Do
- Own the transaction; enqueue after commit with a deterministic job id from a `*_job_key`
  builder in `dispatch.py` (add the builder there for a new job).
- Bind the unit of work's workspace (`tenant.bind` / `bind_platform`); tenant reads ask
  `tenant.workspace_of`, which fails closed (ADR-061).
- Fetch a Project only through `Project.get_for(min_role)` (ADR-062).
- Get a sandbox only via `machines.acquire`, `machines.paper` or `new_frame`; only `operations/machines/` calls a
  lifecycle verb on a Machine's frame (ADR-063).
- Lock order is the Machine row, then the task row. A hand-back on its own connection
  (`release`, `widen`, `release_parked`, the reconciler's fixes) never takes the task row.
- Use Postgres `now()` as the time authority where the root CLAUDE.md says so (routines, leases).
- Every provider call emits a `UsageEvent`; a move never rewrites the ledger.
- Outcome words are a `Literal` or a `StrEnum`; where `schemas/` already has the enum, use it.
- A rule another module needs (authorization, an actor's display name, a row lock) moves to a
  leaf first instead of being imported from a verb module in an import cycle.
- A helper's docstring opens with what it commits and which row lock it may leave held.

## Never
- Import `starlette` or `fastapi`: the screen's socket lives in `api/screen_socket.py`, and
  `operations/screen.py` hands it the checks and verbs it runs.
- Call `select()` here; add a model method.
- Let an ORM row reach an agent, a prompt or a response; hand `ai/` values from `schemas/`.
- Write to `task_events`, `plan_edits` or `usage_events` except by appending.
- Add a function-local `from prismoid.operations import ...` to dodge a cycle without a comment that
  says so; a new one is a sign the rule belongs in a leaf.
- Grow `steering.py` with non-steering verbs, or `screen.py` / `gates.py` / `planning.py` with a
  new job: those are baselined hubs.

## Checked by machine
- `tests/laws/test_law_transports_in_api.py`: no server framework under `operations/`.
- `tests/laws/test_law_keyset_only.py`: no `.offset(`.
- `tests/laws/test_tenancy_map.py`: every table has one tenancy class.
- `backend/tools/audit.sh`: "no select() outside db/models/", "dbos is imported only by
  jobs/durable.py", "no model strings outside the provider files", "no URL literals outside
  conf.py / providers / links.py".
- `.importlinter`: the layers contract; `operations` sits under `api` and `jobs`, above `ai`. Inside
  this folder `operations-layers` puts the leaves under the nouns and the nouns under the engine
  (`execution`, `planning`, `gates`, `steering`, `thread`, `tasks`, `routines`); its ignore list is
  today's upward edges and may only shrink. Place a new module in one of its three layers.
- `quality:py.local_imports`: function-local `prismoid` imports per file, baselined; a new one fails.
- `quality:py.complexity`, `quality:py.statements`, `quality:py.nesting`,
  `quality:py.positional`, `quality:py.file_lines`, `quality:comments.process_refs`. Still over
  the 800-line cap and baselined here: `execution/__init__.py` (938 lines) and `steering.py` (855). New code meets the caps; the baseline only shrinks.

## Write it like this
- `stream_tickets.py`: one resource in 104 lines; the invariant (single-use, thirty seconds,
  bound to the credential row, never the token) at the top; the caller passes the session.
- `machines/reconcile.py` with `schemas/machines.sweep_actions`: facts read in one platform
  session, a pure function decides, the shell dispatches with deterministic ids and per-row
  error isolation.
- `routine_fire._fire_gate` over `routine_gate.py`, and `turn/route.route_turn` over `turn/intake.py`:
  a thin driver reads the rows once; each decision is a pure function over them, unit-tested.
- `dispatch.py`: the `Dispatcher` protocol and the job-key builders, one home.

## Review focus
- Accretion: a new feature that adds a branch to `gates.resolve`, a verb to `steering.py` or a
  job to `screen.py` instead of its own module. A new gate kind is a handler row.
- Copy instead of extract: the event envelope (28 copies), the short Machine-row transaction (11),
  the actor-name fallback (10). A third copy in the diff gets a helper in the same PR.
- Transaction visibility: an enqueue before commit, a lock taken in a helper and held longer
  than before, a Machine handed back while the task row is held.
- Routines (ADR-048): a fire or non-fire that does not journal a `routine_runs` row with its
  reason; `next_fire_at` moved anywhere but the fire gate's compare-and-advance; fire math
  outside `schemas/routines.py`.
- Tenancy: a reference that could cross workspaces, a tenant read that does not fail closed.
- Comments that cite a ruling, review item or plan task instead of stating the reason.
