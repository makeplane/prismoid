# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Device grants (ADR-078): what a person granted a task on the device - a
folder, read-write or read-only, or Docker. Postgres is the authority; the worker tells the helper.

A tenant root, pinned to its task's workspace by the composite `(task_id, workspace_id)` FK, and
gone with its task. Revoke is soft (`revoked_at`). A folder's virtual root (`name`) is minted once
and unique in its task for good; `path_key` is the folder's identity for "is it busy", computed in
Python (NFC, case-folded) so SQL never folds case itself.

Revision ID: device_grants
Revises: device
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "device_grants"
down_revision: str | Sequence[str] | None = "device"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_grants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=True),
        sa.Column("mode", sa.String(length=2), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=True),
        sa.Column("path_key", sa.String(length=2048), nullable=True),
        sa.Column("created_by_actor_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_actor_id"],
            ["actors.id"],
            name=op.f("fk_device_grants_created_by_actor_id_actors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            name="fk_device_grants_task_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_device_grants_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_grants")),
    )
    op.create_index("ix_device_grants_task_id", "device_grants", ["task_id"], unique=False)
    op.create_index("ux_device_grants_task_name", "device_grants", ["task_id", "name"], unique=True)
    op.create_index(
        "ix_device_grants_workspace_path_key", "device_grants", ["workspace_id", "path_key"], unique=False
    )
    op.create_index(op.f("ix_device_grants_workspace_id"), "device_grants", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_device_grants_workspace_id"), table_name="device_grants")
    op.drop_index("ix_device_grants_workspace_path_key", table_name="device_grants")
    op.drop_index("ux_device_grants_task_name", table_name="device_grants")
    op.drop_index("ix_device_grants_task_id", table_name="device_grants")
    op.drop_table("device_grants")
