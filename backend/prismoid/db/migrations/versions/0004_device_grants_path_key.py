# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A folder is busy across workspaces (ADR-078): a deployment's device is
one computer, so "is this folder busy" reads `device_grants` by `path_key` alone, and the index that
led with `workspace_id` served no query.

Revision ID: device_grants_path_key
Revises: device_grants
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "device_grants_path_key"
down_revision: str | Sequence[str] | None = "device_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_device_grants_workspace_path_key", table_name="device_grants")
    op.create_index("ix_device_grants_path_key", "device_grants", ["path_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_device_grants_path_key", table_name="device_grants")
    op.create_index(
        "ix_device_grants_workspace_path_key", "device_grants", ["workspace_id", "path_key"], unique=False
    )
