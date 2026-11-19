# tests/integration

Tests that need the compose stack (Postgres and Silo, sometimes Docker frames) or a real machine: the
local runtime, or macOS for most of `device/`. Run them with the stack up:

    just test-integration            # integration + conformance, in parallel
    uv run pytest tests/integration/gates -n 4 --dist loadgroup

## Where a new test goes

The folders group tests by product domain, not by module. Pick the folder whose behaviour the test proves:

| Folder | What its tests prove |
| --- | --- |
| `api/` | the HTTP doors: auth, streams, tickets, lists, the inbox, CORS, the SPA mount |
| `workspaces/` | workspaces, projects, members, invites, tenant isolation |
| `turns/` | a turn end to end: intake, the Lead, lanes, follow-ups, endings |
| `gates/` | gates and parks: questions, approvals, budgets, control, pause |
| `steering/` | steers: the door, boundaries, fences, the step inbox |
| `jobs/` | the job layer: beats, retries, the call log store, write-once effects |
| `machines/` | the Machine: lifecycle, leases, sleep, reconcile, the screen |
| `frames/` | real frames and runtimes (Docker, the local runtime, the browser) |
| `device/` | the device Machine on a Mac: Chrome, grants, recovery, services, stop |
| `verification/` | artifacts, sources and the verifier suites |
| `connectors/` | connectors: attach, OAuth, unavailable servers |
| `bots/` | bots and skills: registry, publish, routing, teaching |
| `chat/` | conversations: the chat lane, cards, attachments |
| `memory/` | memories: extract, recall, fences |
| `routines/` | routines: fires, owners, the contract |
| `billing/` | usage, prices, the model catalog |
| `db/` | migrations, schema drift, row locks, query counts |
| `local/` | `prismoid up` and the local Postgres |

A test that needs no service belongs in `tests/unit/`. A clause every frame driver must pass belongs in
`tests/conformance/`.

## Rules

- Mark a file that needs the stack `pytestmark = pytest.mark.integration`.
- Use the `db`, `actor` and `client` fixtures from `integration/conftest.py`; never define them again.
- A file that spins real frames joins an `xdist_group` (`frames-a` to `frames-d`); the root conftest
  marks it `frames`, and CI runs frame tests apart (`-m frames`).
- Clean up only what the test made; the root `conftest.py` sweeps by time window.
- Name the file `test_<what it tests>.py` with a basename no other test file uses; add `__init__.py`
  to a new folder.
- Shared rigs and fakes live in `tests/support/`, never in a test module.
- Most `device/` files need macOS and skip elsewhere; `just device-proof` runs the Chrome and stop proofs
  and fails what this Mac cannot run.

[tests/README.md](../README.md) maps every tier; `tests/CLAUDE.md` holds the full rules for the suite.
