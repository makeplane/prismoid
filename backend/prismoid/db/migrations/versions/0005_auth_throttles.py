# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The auth doors' burst buckets in Postgres: login, signup and the reset pair count one window
across every replica and across a restart, and a run of failed logins earns a backoff.

Revision ID: auth_throttles
Revises: device_grants_path_key
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "auth_throttles"
down_revision: str | Sequence[str] | None = "device_grants_path_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_throttles",
        sa.Column("bucket", sa.String(length=160), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("bucket", name=op.f("pk_auth_throttles")),
    )


def downgrade() -> None:
    op.drop_table("auth_throttles")
