# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The asleep cap (ADR-076): which natively asleep computers a workspace gives up. Pure, as the sweep
is: `operations/machines/reconcile` hands it the rows and dispatches what it answers."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence

from prismoid.schemas.machines import MachineState, MachineView, SweepAction, SweepArm


def past_the_asleep_cap(
    rows: Sequence[MachineView], cap: int, *, retiring: Collection[uuid.UUID] = ()
) -> list[SweepAction]:
    """Each workspace's oldest natively asleep computers past `cap`. Only a row with a checkpoint:
    it wakes by rebuilding, where one without would lose its work. `retiring` is what this sweep
    already ends another way."""
    held: dict[uuid.UUID | None, list[MachineView]] = {}
    for row in rows:
        if (
            row.state == MachineState.ASLEEP
            and row.frame_ref
            and row.has_checkpoint
            and row.id not in retiring
        ):
            held.setdefault(row.workspace_id, []).append(row)
    return [
        SweepAction(SweepArm.RELEASE, row.id, row.epoch)
        for asleep in held.values()
        for row in sorted(asleep, key=lambda r: r.last_active_at, reverse=True)[cap:]
    ]
