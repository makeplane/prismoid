# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Signup guards: an address can be proved by an emailed code, and only a proved one sends invites.

`users.email_verified_at` (null until the emailed code proves the address) is backfilled to
`created_at` for every existing user, who joined under invite-only and has nothing to prove. The
`email_verifications` table is the `password_resets` shape: a hashed single-use code, its TTL and
its trail. The new table is empty and takes no lock; the nullable column add and the backfill touch
the live `users` table, so its lock is taken the online way (`online.py`).

Revision ID: signup_guards
Revises: machine_profile_code
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from prismoid.db.migrations import online

# revision identifiers, used by Alembic.
revision: str = "signup_guards"
down_revision: str | Sequence[str] | None = "machine_profile_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    online.lock_tables({"users": online.ACCESS_EXCLUSIVE})
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET email_verified_at = created_at")
    op.create_table(
        "email_verifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.actor_id"],
            name=op.f("fk_email_verifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verifications")),
    )
    op.create_index(
        op.f("ix_email_verifications_code_hash"), "email_verifications", ["code_hash"], unique=True
    )
    op.create_index(op.f("ix_email_verifications_user_id"), "email_verifications", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_verifications_user_id"), table_name="email_verifications")
    op.drop_index(op.f("ix_email_verifications_code_hash"), table_name="email_verifications")
    op.drop_table("email_verifications")
    op.drop_column("users", "email_verified_at")
