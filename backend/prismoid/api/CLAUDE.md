# api - folder rules
Scope: `backend/prismoid/api/`. Read with the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The door: routes (`urls.py`, SCREEN 1), views (`endpoints/<resource>.py`), request and response
shapes (`serializers/<resource>.py`), the auth dependencies (`deps.py`), the stream lanes and the
hub. A view authenticates, validates, makes one call and serializes. It is NOT where business
rules, transactions, queries or agent calls live; those belong to `operations/` and `db/models/`.

## Do
- One file per resource in `endpoints/` and in `serializers/`; named functions (`create_task`,
  `list_tasks`, `resolve_gate`); every route registered in `urls.py`.
- Signatures use the `ActorDep` / `SessionDep` / `PageDep` aliases from `deps.py`.
- Keep a view to auth, validate, one operation or fat-model call, serialize: 20 lines at most.
- `*In` is a request body, `*Out` a response. Type a vocabulary field with its `StrEnum` from
  `schemas/`, so the wire carries the same strings plus an OpenAPI enum.
- Hand an operation a value built from the body, not the body's fields one by one; a view that
  passes many `=body.<attr>` arguments is shuttling.
- Speak user, agent and API key at the boundary; "actor" only inside event or audit payloads.
- Every route requires an Actor, except the doors the root CLAUDE.md lists: healthz, login,
  signup, the public config read (`GET /v1/auth/config`, ADR-086), the password-reset pair, the connectors OAuth callback, the routine fire door's `rt_`
  token, ADR-056's three machine doors, the screen WebSocket (its `st_` ticket, ADR-065;
  minting the ticket requires an Actor), and the event streams' browser path (a single-use `et_`
  ticket in place of the Authorization header, ADR-082; minting it requires an Actor). No route
  reads a credential from `?token=`. A new exception is an ADR plus a root CLAUDE.md edit in
  the same PR.
- Streams: only Lane-1 frames set `id:`; stream routes are never compressed or buffered; a live
  stream re-proves its credential row and task authorization every `stream_reauth_seconds` and
  severs on the first failed proof (ADR-031), whether it opened with a header or a ticket.
- A WebSocket's life belongs here, not in `operations/`; new socket code lands in `api/`
  (`screen_socket.py` is the screen's: accept, the clocks, the goodbye; the checks are
  `operations/screen.py`'s).

## Never
- Import `prismoid.ai`, start a transaction, call `select()`, or let an ORM row reach a response.
- A raw `Depends(...)` in a view signature.
- `dict[str, Any]` into a serializer; pass a typed value (HANDOFF §2 rule 1).
- A plaintext token in a log, a span or a URL; `ticket=`/`token=` query values stay redacted.
- `except Exception` that turns an outage into a 404 or a close code meaning "not found".
- An OFFSET page: list views are keyset only.

## Checked by machine
- `quality:py.endpoint_lines` (20): six views are baselined over it (`tasks.list_tasks`,
  `create_task`, `follow_up_task`, `admin_tasks.list_tasks`, `memories.list_memories`,
  `bots.set_face`); a planned refactor brings them under. A new view meets the cap.
- `quality:py.complexity`, `quality:py.nesting`, `quality:py.positional`, `quality:py.file_lines`,
  `quality:comments.process_refs`, all through `just quality`.
- `backend/tools/audit.sh`: "api/ never imports ai/", "no raw Depends(...) in endpoint signatures",
  "no transactions started in api/", "no select() outside db/models/", "no ActorOut defined or
  returned by a serializer", "no URL literals outside conf.py / providers / links.py".
- `.importlinter`: "Views never touch agents" and the layers contract (`just layers`).
- `tests/laws/test_api_surface.py`: no `501` left in `api/`; every route is in the OpenAPI spec
  except `/watch`.
- `tests/laws/test_law_keyset_only.py`: no `.offset(` anywhere.
- `tests/laws/test_law_transports_in_api.py`: `starlette` and `fastapi` only under `api/` and
  `runner/`.
- `just export-contract` regenerates the OpenAPI and event schemas the web types are built from.

## Write it like this
- `endpoints/screen.py`: five views, each auth, one operation, serialize in one to three lines;
  the docstring says why the socket takes no Actor dependency; the hub is handed in.
- `hub.py`: a deep module behind `register` / `unregister` / `publish`; its docstrings explain
  the one LISTEN, the poison pill and drop-not-backpressure.
- `deps.py` `authenticate` / `require_principal`: one door body reused by the request path and
  the stream re-proof, dispatch by token prefix, the security reasoning in the comments.

## Review focus
- A view that grew a branch, a loop or a second operation call: the logic belongs in the
  operation. Quote "auth, validate, one call, serialize".
- A new unauthenticated route or a new token prefix: is it in the root CLAUDE.md door list, with
  its ADR? A missing entry is Critical.
- A stream change: does every path still re-prove the credential and the task each interval, and
  end in a durable final or snapshot?
- A list view that copies the cursor rule or filter mapping again (16 copies today): flag
  it and point at the planned `page_out` helper.
- A serializer field named `status`, `state`, `kind`, `role`, `mode`, `driver`, `source` or
  `verdict` typed `str`, or a `Field(pattern="^(a|b)$")`: use the enum.
- An error mapping that changes a status code clients see: say so in the PR.
