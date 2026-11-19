# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Status and kind columns hold only their enum's words, and `tasks.scale` defaults to `auto` in the
database as it does in the ORM.

The columns stay VARCHAR; a CHECK names the words each one may hold, so a raw INSERT or a typo in a
query can no longer write a status the code cannot read back. The word lists are copied from the
`StrEnum`s in `schemas/` and `db/models/` as they stand today and frozen here, as a migration must
be: a new word is a new revision that replaces its column's constraint, and
`tests/integration/test_vocabulary_checks.py` fails until it lands. `machines.driver` is left open on
purpose: drivers are a registry, and a row may name one this worker does not know.

The tables are live, so the constraints go on the online way (`online.py`): every table locked at
once, children first, with a timeout, and each CHECK added `NOT VALID`, which binds every new row
at once and scans nothing. The old rows are checked by the next revision, `vocabulary_checks_valid`,
outside this window and under a lock readers and writers pass through.

`tasks.scale` had `auto` as its ORM default and `standard` as its server default. Every task the
app creates is `auto` until intake resolves it; the server default only ever filled rows that were
written before the column existed. It now says `auto` too, so a row written outside the ORM enters
intake detection like any other new task.

Revision ID: vocabulary_checks
Revises: auth_throttles
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from prismoid.db.migrations import online

# revision identifiers, used by Alembic.
revision: str = "vocabulary_checks"
down_revision: str | Sequence[str] | None = "auth_throttles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VOCABULARY: dict[tuple[str, str], tuple[str, ...]] = {
    ("tasks", "status"): (
        "created",
        "clarifying",
        "planning",
        "running",
        "waiting",
        "paused",
        "delivered",
        "failed",
        "cancelled",
    ),
    ("plan_steps", "kind"): ("step", "gate", "artifact"),
    ("plan_steps", "state"): (
        "pending",
        "ready",
        "running",
        "verifying",
        "done",
        "retrying",
        "escalated",
        "skipped",
    ),
    ("gates", "kind"): ("approve", "input", "budget", "connect", "pause", "control"),
    ("gates", "state"): ("open", "resolved", "expired"),
    ("machines", "state"): ("waking", "running", "parked", "idle", "sleeping", "asleep", "gone"),
    ("machines", "profile"): ("base", "browser"),
    ("steers", "state"): ("accepted", "applied", "rejected", "retracted"),
    ("routines", "status"): ("active", "paused", "retired"),
    ("connectors", "status"): ("pending_auth", "authorizing", "live", "needs_reauth", "error", "disabled"),
    ("captures", "status"): ("open", "promoted", "archived"),
    ("skills_registry", "status"): ("draft", "published", "archived"),
    ("skills_registry", "origin"): ("builtin", "user"),
    ("bots_registry", "status"): ("draft", "published", "archived"),
    ("bots_registry", "origin"): ("user", "imported"),
    ("actors", "kind"): ("human", "agent", "service"),
    ("actors", "status"): ("active", "suspended"),
    ("workspaces", "kind"): ("personal", "team"),
    ("workspaces", "status"): ("active", "suspended"),
    ("memories", "kind"): ("instruction", "fact", "preference", "note"),
    ("memories", "status"): ("active", "superseded", "retired"),
}


def _name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_vocabulary"


def upgrade() -> None:
    online.lock_tables(dict.fromkeys(sorted({table for table, _ in VOCABULARY}), online.ACCESS_EXCLUSIVE))
    for (table, column), words in VOCABULARY.items():
        listed = ", ".join(f"'{word}'" for word in words)
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {_name(table, column)} CHECK ({column} IN ({listed})) NOT VALID"
        )
    op.alter_column("tasks", "scale", server_default=sa.text("'auto'"))


def downgrade() -> None:
    op.alter_column("tasks", "scale", server_default=sa.text("'standard'"))
    for table, column in reversed(VOCABULARY):
        op.drop_constraint(op.f(_name(table, column)), table, type_="check")
