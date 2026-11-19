# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Machine's profile may be `code`: the image with git, ripgrep and node (ADR-097).

`machines.profile` holds `MachineProfile`'s words, and the new word replaces its CHECK, as
`vocabulary_checks` says a new word must. The replacement is a superset, so it goes on `NOT VALID`
inside the chain's short lock (it scans nothing) and is validated after it, outside that window,
under a lock readers and writers pass through. A downgrade touches no row: it puts the old CHECK back
`NOT VALID`, binding new rows only, so a `code` Machine row from before it stays as it was.

Revision ID: machine_profile_code
Revises: call_log
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

from prismoid.db.migrations import online

# revision identifiers, used by Alembic.
revision: str = "machine_profile_code"
down_revision: str | Sequence[str] | None = "call_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_machines_profile_vocabulary"


def _replace(words: tuple[str, ...]) -> None:
    online.lock_tables({"machines": online.ACCESS_EXCLUSIVE})
    listed = ", ".join(f"'{word}'" for word in words)
    op.execute(f"ALTER TABLE machines DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE machines ADD CONSTRAINT {NAME} CHECK (profile IN ({listed})) NOT VALID")


def upgrade() -> None:
    _replace(("base", "code", "browser"))
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE machines VALIDATE CONSTRAINT {NAME}")


def downgrade() -> None:
    _replace(("base", "browser"))
