# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Push devices: the phones a login session registered for inbox push notifications (ADR-100).

A new, empty table, so the chain's transaction holds its lock for no time at all. A device goes with
its person (`actors`) and with the session that registered it (`auth_sessions`), both CASCADE.

Revision ID: push_devices
Revises: usage_workspace_index
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "push_devices"
down_revision: str | Sequence[str] | None = "usage_workspace_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=1024), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("app_id", sa.String(length=200), nullable=False),
        sa.Column("app_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "platform IN ('ios', 'android')", name=op.f("ck_push_devices_platform_vocabulary")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["actors.id"], name=op.f("fk_push_devices_actor_id_actors"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_push_devices_session_id_auth_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_devices")),
        sa.UniqueConstraint("token", name=op.f("uq_push_devices_token")),
    )
    op.create_index(op.f("ix_push_devices_actor_id"), "push_devices", ["actor_id"], unique=False)
    op.create_index(op.f("ix_push_devices_session_id"), "push_devices", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_push_devices_session_id"), table_name="push_devices")
    op.drop_index(op.f("ix_push_devices_actor_id"), table_name="push_devices")
    op.drop_table("push_devices")
