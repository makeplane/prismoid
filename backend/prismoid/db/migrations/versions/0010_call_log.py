# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Call log: a step attempt's logged model and tool calls, so a crash or a park replays them instead
of buying them again. A tenant child of `plan_steps`; payloads are ciphertext.

A new, empty table, so the chain's transaction holds its lock for no time at all.

Revision ID: call_log
Revises: infra_failures
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "call_log"
down_revision: str | Sequence[str] | None = "infra_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_log",
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(length=300), nullable=False),
        sa.Column("phase", sa.String(length=8), nullable=False),
        sa.Column("log_version", sa.SmallInteger(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=True),
        sa.Column("payload_ref", sa.CHAR(length=64), nullable=True),
        sa.Column("blob_ref", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.CHAR(length=16), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.CheckConstraint("phase IN ('intent', 'result')", name=op.f("ck_call_log_phase_vocabulary")),
        sa.ForeignKeyConstraint(
            ["step_id"], ["plan_steps.id"], name=op.f("fk_call_log_step_id_plan_steps"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("step_id", "generation", "unit", "phase", name=op.f("pk_call_log")),
    )


def downgrade() -> None:
    op.drop_table("call_log")
