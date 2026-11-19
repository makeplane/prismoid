# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The vocabulary CHECKs that `vocabulary_checks` added `NOT VALID` are validated: the rows written
before them are checked too.

`VALIDATE CONSTRAINT` scans the table under SHARE UPDATE EXCLUSIVE, which readers and writers pass
straight through, so it runs outside the chain's transaction (`autocommit_block`), never inside the
window that still holds the previous revision's ACCESS EXCLUSIVE locks. Every statement is
idempotent: validating a constraint that is already valid does nothing, so a run cut short is
simply run again. A row that breaks a constraint fails the upgrade loudly and names it; the
constraint keeps binding new rows meanwhile.

Revision ID: vocabulary_checks_valid
Revises: vocabulary_checks
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "vocabulary_checks_valid"
down_revision: str | Sequence[str] | None = "vocabulary_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as a migration must be: the constraints `vocabulary_checks` added, by table.
CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("tasks", "status"),
    ("plan_steps", "kind"),
    ("plan_steps", "state"),
    ("gates", "kind"),
    ("gates", "state"),
    ("machines", "state"),
    ("machines", "profile"),
    ("steers", "state"),
    ("routines", "status"),
    ("connectors", "status"),
    ("captures", "status"),
    ("skills_registry", "status"),
    ("skills_registry", "origin"),
    ("bots_registry", "status"),
    ("bots_registry", "origin"),
    ("actors", "kind"),
    ("actors", "status"),
    ("workspaces", "kind"),
    ("workspaces", "status"),
    ("memories", "kind"),
    ("memories", "status"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for table, column in CONSTRAINTS:
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT ck_{table}_{column}_vocabulary")


def downgrade() -> None:
    """Nothing to undo: Postgres has no way back to NOT VALID, and a validated CHECK binds the
    same rows. Downgrading past `vocabulary_checks` drops the constraints themselves."""
