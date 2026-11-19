# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Stream tickets: the browser opens a task's or a conversation's event stream with a single-use,
thirty-second ticket instead of putting its session token in the URL.

A new, empty table, so the chain's transaction holds its lock for no time at all.

Revision ID: stream_tickets
Revises: vocabulary_checks_valid
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "stream_tickets"
down_revision: str | Sequence[str] | None = "vocabulary_checks_valid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stream_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("credential_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "subject_kind IN ('task', 'conversation')",
            name=op.f("ck_stream_tickets_subject_kind_vocabulary"),
        ),
        sa.CheckConstraint(
            "credential_kind IN ('session', 'api_key')",
            name=op.f("ck_stream_tickets_credential_kind_vocabulary"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["actors.id"], name=op.f("fk_stream_tickets_actor_id_actors"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stream_tickets")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_stream_tickets_token_hash")),
    )
    op.create_index(op.f("ix_stream_tickets_expires_at"), "stream_tickets", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_stream_tickets_expires_at"), table_name="stream_tickets")
    op.drop_table("stream_tickets")
