# runner - folder rules
Scope: `backend/prismoid/runner/`. Read with the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The process entrypoints and the CLI. `main.py` is `prismoid` (HANDOFF §5.6): a thin client of the
routes in `api/urls.py`, never of the database, and the instrument every feature is demonstrated
through. `main.py` only assembles the app; each command group is one module in `cli/` (the older
`*_cli.py` files beside it are groups too, and move into `cli/` when next touched); `client.py`,
`stream.py` and `questions.py` are the CLI's plumbing; `worker.py` boots the worker; `dev.py` holds the dev
doctor and the model ping drill. It is NOT a second API: a command that needs a new capability
gets a route first, then calls it.

## Do
- Every command supports `--json`, and exit codes mean things: 0 ok, 1 error, 2 denied (Click
  uses 2 for a usage error too: an unknown option, a value outside its choices), 3 timeout,
  4 conflict (a stale steer, `client.EXIT_CONFLICT`).
- Talk to the API over HTTP through `client.py`; read the event stream through `stream.py`.
- `prismoid tasks events --follow` stays the terminal flight recorder: a new event type renders there,
  as one row in `stream._BODIES`, never a new branch in `_render`.
- A new command group gets its own `cli/<group>.py` module and one `add_typer` line in `main.py`;
  it never lands inline in `main.py`. A top-level command (`prismoid do`, `prismoid agents`) is a plain
  function in its group's module, registered with one `app.command()` line.
- A closed set of option values is a `StrEnum` in `cli/choices.py`, so `--help` lists it; the
  request field it fills holds the same words (`tests/unit/runner/test_cli_choices.py`).
- Group a command's many options into a value, or take them as keyword-only options, instead of
  passing a long positional list through helpers.
- Keep the worker's boot order and say why in its docstring when it changes (logfire, `jobs.all`,
  the dispatcher, the agent actors, the frame reconcile).
- Prove a user-visible change with one real run: `just smoke` or the relevant CLI flow.

## Never
- Reach the database from a command a user runs; the CLI is a client of `api/urls.py`. The
  worker, `dev.py` and a few operator commands (in `bots_cli.py`, `channels_cli.py`,
  `plugins_cli.py`) open a session through an operation; a new exception is a review question.
- Print a plaintext token, or put one in a URL or a log line.
- Add a command or a helper to `main.py`: it registers groups and nothing else.
- Hand-roll a protocol client: the screen's `--shot` speaks WebSocket through `websockets`'
  sync client (`cli/machine.py`), HTTP goes through `client.py`.
- Construct a pydantic-ai `Agent(` outside `dev.py`'s ping drill.

## Checked by machine
- `tests/laws/test_law_transports_in_api.py`: `runner/` is one of the two places a server
  framework may be imported (the `run api` entrypoint).
- `tests/laws/test_law_agent_construction.py`: `runner/dev.py` is an allowed constructor, for the
  ping drill only.
- `.importlinter`: `runner` is the top layer; nothing imports it.
- `tests/laws/test_law_plain_words.py`: no `--help` text (a command's docstring included) cites
  `§n`, `ADR-n`, a milestone or `Rule n`; say it in words.
- `backend/tools/audit.sh`: "no URL literals outside conf.py / providers / links.py" (the dev doctor's
  two egress probes are the named exception), "no hardcoded provider keys".
- `quality:py.positional`, `quality:py.nesting`, `quality:py.complexity`,
  `quality:py.file_lines`, `quality:comments.process_refs`. Baselined here: `cli/machine.py::_screen_shot`
  (nesting 4), `routines_cli.routines_create` (21) and `routines_set` (16),
  `models_cli.models_add`. New commands meet the caps.

## Write it like this
- `routines_cli.py`: one group per file, a docstring that shows the commands as a user types
  them and says what the noun does (a routine executes nothing).
- `worker.py`: the boot order as a numbered docstring, each step with its reason.
- `client.py`: the one HTTP client every command goes through.

## Review focus
- A new command that reaches the database or an operation directly instead of a route.
- A command without `--json`, or an exit code that does not mean what the table says.
- A new group or helper added to `main.py` instead of its own file.
- A user-visible change whose PR has no real run as proof.
- A change to the worker's boot order or reconcile that could reap a live peer's Machines.
