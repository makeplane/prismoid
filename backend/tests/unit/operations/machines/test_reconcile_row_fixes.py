# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The reconciler's two row fixes, on a row in memory: one that keeps the frame (what the frame is
told after the commit) and one that takes it away (what is billed, lost and ended after the commit).
No database - the row is a plain `Machine` object."""

from __future__ import annotations

import datetime as dt
import uuid

from prismoid.db.models.machine import Machine
from prismoid.operations.machines.reconcile import fix_keeping_frame, fix_taking_frame
from prismoid.schemas.machines import MachineState, RebuildReason, RowFix, step_holder
from tests.support.rigs.machine_decisions_rig import NOW, ago

TASK = uuid.UUID(int=7)
ADA = uuid.UUID(int=9)


def _row(state: MachineState, *, frame: str | None = "frame-1", metered: float | None = None) -> Machine:
    return Machine(
        id=uuid.UUID(int=1),
        task_id=TASK,
        driver="docker",
        profile="base",
        state=state,
        state_since=ago(900),
        frame_ref=frame,
        epoch=3,
        held_by=None,
        egress_version=4,
        egress_hosts=["a.example"],
        step_hosts=[],
        metered_until=ago(metered) if metered is not None else None,
        last_active_at=ago(900),
        control_private=False,
    )


def _fix(
    row: Machine,
    to: MachineState,
    *,
    keep_frame: bool = False,
    release_hold: bool = False,
    thaw: bool = False,
) -> RowFix:
    return RowFix(
        machine_id=row.id,
        epoch=row.epoch,
        seen=MachineState(row.state),
        since=row.state_since,
        held_by=row.held_by,
        to_state=to,
        keep_frame=keep_frame,
        release_hold=release_hold,
        thaw=thaw,
    )


def _lease(row: Machine, *, until: dt.datetime) -> None:
    row.control_actor_id, row.control_conn = ADA, "c1"
    row.control_since, row.control_until = ago(60), until


def test_a_lost_running_frame_bills_its_segment_and_says_work_was_lost() -> None:
    row = _row(MachineState.RUNNING, metered=120)
    taken = fix_taking_frame(row, _fix(row, MachineState.GONE), NOW)
    assert taken.segment is not None and taken.segment.seconds == 120 and taken.segment.at == NOW
    assert taken.lost == TASK and taken.cleared is None
    assert row.frame_ref is None and row.metered_until is None
    assert row.state == MachineState.GONE and row.state_since == NOW and row.gone_reason is None


def test_a_frameless_row_gone_lost_nothing_and_says_why_it_rebuilds() -> None:
    row = _row(MachineState.RUNNING, frame=None)
    taken = fix_taking_frame(row, _fix(row, MachineState.GONE), NOW)
    assert taken.segment is None and taken.lost is None
    assert row.gone_reason == RebuildReason.LOST


def test_a_stuck_wake_held_nothing_unsaved() -> None:
    row = _row(MachineState.WAKING, metered=30)
    taken = fix_taking_frame(row, _fix(row, MachineState.ASLEEP), NOW)
    assert taken.lost is None and taken.segment is not None and row.gone_reason is None


def test_a_move_to_asleep_ends_the_control_lease_after_the_commit() -> None:
    row = _row(MachineState.RUNNING)
    _lease(row, until=NOW + dt.timedelta(minutes=5))
    taken = fix_taking_frame(row, _fix(row, MachineState.ASLEEP), NOW)
    assert taken.cleared is not None
    task_id, lease, at = taken.cleared
    assert (task_id, lease.actor_id, at) == (TASK, ADA, NOW)
    assert row.control_actor_id is None and row.last_active_at == NOW


def test_a_released_dead_hold_narrows_egress_and_restarts_the_idle_clock() -> None:
    row = _row(MachineState.RUNNING)
    row.held_by, row.held_since = step_holder("s1", 1), ago(900)
    after = fix_keeping_frame(row, _fix(row, MachineState.IDLE, keep_frame=True, release_hold=True), NOW)
    assert row.state == MachineState.IDLE and row.held_by is None and row.last_active_at == NOW
    assert row.step_hosts == ["a.example"], "the step's list waits for the next driver"
    assert after is not None and after.egress == (5, []) and not after.thaw and after.ref == "frame-1"


def test_a_kept_frame_with_nothing_to_push_or_thaw_is_told_nothing() -> None:
    row = _row(MachineState.RUNNING)
    assert fix_keeping_frame(row, _fix(row, MachineState.RUNNING, keep_frame=True), NOW) is None
    assert row.state_since == ago(900), "a fix to the state it is in moves nothing"


def test_a_thaw_reaches_a_kept_frame_even_without_a_new_list() -> None:
    row = _row(MachineState.RUNNING)
    after = fix_keeping_frame(row, _fix(row, MachineState.RUNNING, keep_frame=True, thaw=True), NOW)
    assert after is not None and after.thaw and after.egress is None


def test_a_driven_idle_machine_keeps_the_drivers_list() -> None:
    row = _row(MachineState.RUNNING)
    row.step_hosts = ["b.example"]
    _lease(row, until=NOW + dt.timedelta(minutes=5))
    after = fix_keeping_frame(row, _fix(row, MachineState.IDLE, keep_frame=True), NOW)
    assert after is not None and after.egress == (5, ["b.example"])
    assert row.control_actor_id == ADA, "idle keeps the lease with its frame"
