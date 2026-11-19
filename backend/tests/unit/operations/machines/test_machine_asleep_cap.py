# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The asleep cap (ADR-076): a workspace's oldest natively asleep computers give up their room."""

from __future__ import annotations

import uuid
from dataclasses import replace

from prismoid.schemas.machine_caps import past_the_asleep_cap
from prismoid.schemas.machines import MachineState, MachineView, SweepAction, SweepArm
from tests.support.rigs.machine_decisions_rig import view


def asleep(
    workspace: uuid.UUID, active: float, *, frame: str | None = "cmp", checkpoint: bool = True
) -> MachineView:
    row = view(MachineState.ASLEEP, active=active, has_checkpoint=checkpoint, has_objects=checkpoint)
    return replace(row, workspace_id=workspace, frame_ref=frame)


def test_past_the_cap_each_workspace_releases_its_oldest() -> None:
    ours, theirs = uuid.uuid4(), uuid.uuid4()
    oldest, older, newer, newest = (asleep(ours, a) for a in (400, 300, 200, 100))
    alone = asleep(theirs, 900)
    actions = past_the_asleep_cap([newest, oldest, alone, newer, older], 2)
    assert {a.machine_id for a in actions} == {oldest.id, older.id}
    assert {a.arm for a in actions} == {SweepArm.RELEASE}


def test_the_cap_never_touches_a_row_it_cannot_rebuild_or_has_nothing_to_free() -> None:
    ours = uuid.uuid4()
    rows = [asleep(ours, 100), asleep(ours, 200)]
    archived = asleep(ours, 300, frame=None)  # holds no computer
    unkept = asleep(ours, 400, checkpoint=False)  # releasing it would lose its work
    assert past_the_asleep_cap([*rows, archived, unkept], 2) == []


def test_a_row_the_sweep_is_already_ending_is_not_counted() -> None:
    ours = uuid.uuid4()
    kept = [asleep(ours, 100), asleep(ours, 200)]
    expired, spare = asleep(ours, 900), asleep(ours, 800)
    assert past_the_asleep_cap([*kept, expired, spare], 2, retiring={expired.id}) == [
        SweepAction(SweepArm.RELEASE, spare.id, spare.epoch)
    ]
