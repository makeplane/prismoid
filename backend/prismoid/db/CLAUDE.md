# db - folder rules
Scope: `backend/prismoid/db/`. Read with the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The rows. `models/<table>.py` holds one table each, with its queries as fat-model methods (the only
place `select()` is written); `base.py` the declarative base and shared column helpers
(`uuid_pk`, `created_at`, the keyset cursor pair); `session.py` sessions and scopes; `engine.py`
the pool; `migrations/` the Alembic history. It is NOT where values shared across layers live
(`schemas/`), where request and response shapes live (`api/serializers/`), or where a unit of
work is composed (`operations/`).

## Do
- Open every model with its glossary line as the docstring (HANDOFF §2 rule 1, §12).
- Classify every new table in `models/tenancy.py` (tenant root, ledger stamp, platform or
  workspace, tenant child, platform, identity, workspace) in the same PR (ADR-061).
- Reference across tables with composite `(project_id, workspace_id)` foreign keys so a
  reference never crosses workspaces.
- Owner and creator foreign keys target `actors`, with the human invariant in the model.
- Back a vocabulary column with its one `StrEnum` from `schemas/`, never a `Mapped[str]` with the
  values in a comment; a planned `db/base.StrEnumColumn` makes the column read
  `Mapped[WorkMode]` and stays VARCHAR.
- Hold a status or kind column to its words with `vocabulary(column, Enum)` from `base.py` in
  `__table_args__`; a new enum word ships with a migration that replaces that column's constraint.
- Keep the ORM `default` and the `server_default` the same value.
- Paginate by keyset only, through the helpers in `base.py`.
- Change the schema only with an Alembic migration; applied migrations are frozen, so a fix is a
  new revision.
- Read the database clock in one place. Postgres `now()` is the time authority where the root
  CLAUDE.md says so; a planned refactor moves the copies (`Machine.db_now`, `Routine.clock`) to
  one `db_now(session)` in `session.py`.

## Never
- `create_all`, or an edit to an applied migration.
- An `UPDATE` or `DELETE` on `task_events`, `plan_edits` or `usage_events`;
  they are append-only.
- A table without a tenancy class, or a workspace-scoped table without `workspace_id`.
- An `.offset(` page.
- A plaintext token or secret at rest: tokens are stored hashed, connector secrets as Fernet
  ciphertext through `operations/cipher`. The one exception is `push_devices.token`, a phone's FCM
  registration token: an address Prismoid sends to, not a credential to Prismoid, and the upsert
  and every send need it as is (ADR-100).
- A module that is not a table under `models/` in new code (`tenant.py` and `tenancy.py` sit
  there today and are planned to move to `db/`).
- A process citation in a docstring (`models/steer.py` has the one baselined case).

## Checked by machine
- `tests/laws/test_tenancy_map.py`: every table has exactly one tenancy class.
- `tests/laws/test_law_keyset_only.py`: no `.offset(` call, migrations included.
- `backend/tools/audit.sh`: "no select() outside db/models/", the `create_all` check (Alembic only),
  "no hardcoded provider keys".
- `.importlinter`: `db`, `storage` and `mailer` are independent siblings (`|`, enforced: none imports another) above `schemas` and `conf`; `ai`
  never imports `db`.
- `tests/integration/db/test_vocabulary_checks.py`: every `ck_<table>_<column>_vocabulary` in the
  database names exactly its enum's words.
- `quality:py.file_lines`, `quality:py.complexity`, `quality:comments.process_refs`;
  `db/migrations/` is excluded from the ratchet.

## Write it like this
- `models/usage.py`: the docstring states the ledger's invariant (append-only, native units plus
  US dollars, attributed at the time of spend and never rewritten by a move).
- `models/tenancy.py`: a `StrEnum` per class and one classification per table, checked by a law.
- `session.py`: the scopes (`session_scope`, `row_scope`) every caller shares.

## Review focus
- A new column or table: tenancy class, composite foreign key, glossary docstring, enum type,
  matching defaults, and an Alembic revision in the same PR.
- A write path to an append-only table that is not an insert.
- A query method that could return another workspace's rows, or a tenant read that does not
  fail closed.
- A second spelling of the clock or the lock timeout.
- A migration that rewrites history or changes a default without a data plan.
