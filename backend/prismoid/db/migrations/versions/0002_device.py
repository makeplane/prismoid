# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device Machine (ADR-078): the driver a task's Machine runs on, and
the device a Machine's frame is on.

`tasks.machine_driver` is the task's own choice of driver (`device` in local mode), which wins over
the worker's `frame_driver`; NULL is the worker's, as every task before it. `machines.device_name`
is the name of the computer a device Machine runs on, as its helper reports it; NULL on every cloud
driver.

Revision ID: device
Revises: prismoid_baseline
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "device"
down_revision: str | Sequence[str] | None = "prismoid_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("machine_driver", sa.String(length=20), nullable=True))
    op.add_column("machines", sa.Column("device_name", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("machines", "device_name")
    op.drop_column("tasks", "machine_driver")
