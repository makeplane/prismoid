# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Infra failures: the infra deaths a step's ladder caught (a provider that failed, a sandbox that
stopped answering, a wall-clock stop), counted apart so a cap can stop them.

One integer column with a constant server default: Postgres adds it without rewriting the table.
The table is live, so its lock is taken the online way (`online.py`): with a timeout, retried, and
failing the upgrade loudly rather than queueing every step read and write behind it.

Revision ID: infra_failures
Revises: stream_tickets
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from prismoid.db.migrations import online

# revision identifiers, used by Alembic.
revision: str = "infra_failures"
down_revision: str | Sequence[str] | None = "stream_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    online.lock_tables({"plan_steps": online.ACCESS_EXCLUSIVE})
    op.add_column("plan_steps", sa.Column("infra_failures", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("plan_steps", "infra_failures")
