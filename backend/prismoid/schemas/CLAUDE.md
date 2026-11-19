# schemas - folder rules
Scope: `backend/prismoid/schemas/`. Read with the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The values: domain shapes and the pure decisions over them that two or more layers read
(`api`, `operations`, `jobs`, `ai`). Every vocabulary's `StrEnum` is declared here once. It sits at
the bottom of the layers beside `conf`, so it imports no `db`, `operations`, `jobs`, `api` or `ai`.
It is NOT for rows (`db/models/`), request and response shapes (`api/serializers/`), mutable
state, provider-response parsers or env-grammar parsers.

## Do
- Declare each vocabulary once as a `StrEnum` here; the column, the serializer field and the
  operation parameter all use it.
- Keep decisions pure and clock-free at the edges: take `now` as an argument, return a frozen
  value or an enum, raise a plain `ValueError` with the user-facing sentence where the module's
  convention says so (operations turn it into the 422).
- Fence a library to the one module that needs it: fire math is `cronsim` in `routines.py`
  ONLY, never `croniter` (ADR-048); egress parse-time vetting lives in `egress.py`, shared by the
  web door and the connectors door (ADR-046, ADR-020).
- Keep catalogs as data: `connector_catalog.py` is rows, its URLs allowed by the audit's named
  exclusion, every seed probed live by `just connectors-catalog-probe` before it lands (ADR-054).
  A row is measured from the site's pages and holds only the operator's names; its test keeps the rules.
- Normalize collections at harvest through `collection_repair`, with repairs recorded on the
  provenance (ADR-019).
- Redact private event fields where events leave the server through `events.redact_for`
  (ADR-062).
- Split a module when it needs a section banner for a second job; one job per file.

## Never
- Put a pure decision with a single caller here; it lives beside its I/O shell (for example an
  `operations/<x>/judge.py`). `schemas/machines.py` (`sweep_actions`, `reconcile`) and
  `schemas/screen.py` (wire codec plus lease math) are the catch-alls a planned split divides
 ; do not add a third job to either.
- Import `conf`; pass the settings value in (`schemas | conf` are independent in `.importlinter`,
  so an import fails `just layers`).
- Import `starlette` or `fastapi`.
- Type a vocabulary field `str`, or write `Field(pattern="^(a|b)$")` for a closed set.
- Let a dict with known keys cross a layer as `dict[str, Any]`; give it a model. `resume_state`
  becomes typed models in `park.py` with the same JSON keys (a planned refactor).

## Checked by machine
- `.importlinter`: `schemas` and `conf` are the bottom layer, independent siblings (`|`); nothing here imports upward
  or imports `conf`.
- `tests/laws/test_law_transports_in_api.py`: no server framework here.
- `backend/tools/audit.sh`: "no model strings outside the provider files", "no URL literals outside
  conf.py / providers / links.py" (with `connector_catalog.py` the named exclusion).
- `tests/laws/test_no_em_dash.py`: copy and docstrings here carry no em dash.
- `quality:py.complexity`, `quality:py.statements`, `quality:py.nesting`,
  `quality:py.file_lines`. Baselined here: `collection._check_view`, `events.py` (999 lines),
  `screen.StreamDecoder.feed`. Planned refactors pay them down.

## Write it like this
- `routines.py`: values and pure decisions that `api`, `operations` and `jobs` all read; a
  `StrEnum` for every vocabulary (`TriggerKind`, `Overlap`, `CatchUp`, `Posture`), `now` passed
  in, no `conf` import, the one library fenced here, the reason for the library choice in the
  docstring.
- `model_class.py`: `ModelClass`, the one vocabulary every agent and the gateway speak.

## Review focus
- A new vocabulary that is not an enum, or an enum declared a second time somewhere else.
- A decision that reads the clock, the environment or a setting instead of taking it as an
  argument.
- A module growing a second job, or a single-caller decision parked here for convenience.
- A dispatch over field or event types with more than six arms written as an `if`/`elif` or
  `match` chain: a table of small coercers reads better (`collection_repair` is the case).
- A change to a wire value (an enum string, a JSON key) that old rows, journals or clients hold.
