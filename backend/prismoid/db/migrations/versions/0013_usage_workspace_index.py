# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One workspace's usage over time gets its own index: `usage_events` is the biggest table in the
database, and a workspace's spend is read by `workspace_id` and `at`.

`ix_usage_events_workspace_at` on `(workspace_id, at)` is built CONCURRENTLY (`online.py`, rule 3):
writers keep writing while it builds. That needs autocommit, so this revision holds nothing else, and
every statement is idempotent - a run cut short is run again. The composite's leading column serves
every read the single-column `ix_usage_events_workspace_id` served (a foreign-key check on the
workspace row included), so that index goes in the same revision: one index per read, not two on the
hottest insert path.


Revision ID: usage_workspace_index
Revises: signup_guards
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

from prismoid.db.migrations import online

# revision identifiers, used by Alembic.
revision: str = "usage_workspace_index"
down_revision: str | Sequence[str] | None = "signup_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        online.create_index_concurrently("ix_usage_events_workspace_at", "usage_events", "workspace_id, at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_usage_events_workspace_id")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        online.create_index_concurrently("ix_usage_events_workspace_id", "usage_events", "workspace_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_usage_events_workspace_at")
