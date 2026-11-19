# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the Machine's pure decisions: a clock, the sweep's limits, and a `MachineView` made by
hand with the fields a test names. A plain module, not a test file."""

import datetime as dt
import uuid

from prismoid.schemas.machines import (
    MachineState,
    MachineView,
    ReconcileLimits,
    SweepLimits,
)

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)


LIMITS = SweepLimits(idle_s=600, park_hold_s=1800, retention_days=14, stuck_s=600)


RLIMITS = ReconcileLimits(
    throwaway_max_s=2400, verify_max_s=1200, legacy_grace_s=3600, stuck_s=600, fresh_s=30, adopt_legacy=True
)


def ago(seconds: float) -> dt.datetime:
    return NOW - dt.timedelta(seconds=seconds)


def view(
    state: MachineState,
    *,
    since: float = 0,
    active: float = 0,
    epoch: int = 3,
    held_by: str | None = None,
    has_checkpoint: bool = False,
    has_objects: bool = False,
    mid: uuid.UUID | None = None,
    profile: str = "base",
    lease_until: float | None = None,
    lease_since: float = 120,
) -> MachineView:
    """`lease_until` is seconds after NOW (negative: already past); None = nobody holds a lease."""
    return MachineView(
        id=mid or uuid.uuid4(),
        state=state,
        state_since=ago(since),
        epoch=epoch,
        held_by=held_by,
        last_active_at=ago(active),
        has_checkpoint=has_checkpoint,
        has_objects=has_objects,
        profile=profile,
        control_since=ago(lease_since) if lease_until is not None else None,
        control_until=NOW + dt.timedelta(seconds=lease_until) if lease_until is not None else None,
    )
