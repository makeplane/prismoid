# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The control lease on the Machine's row (ADR-065): six columns set and cleared
together, read back as one value, and a Machine that loses its frame loses its lease in the same
transaction (`move` hands the cleared lease to its caller, which records the ending)."""

import datetime as dt
import uuid

from prismoid.db.models.machine import Machine
from prismoid.schemas.machines import MachineState
from prismoid.schemas.screen import Lease

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)


def _lease(*, private: bool = False) -> Lease:
    return Lease(
        actor_id=uuid.uuid4(),
        conn="tab-00000001",
        private=private,
        since=NOW,
        until=NOW + dt.timedelta(seconds=600 if private else 60),
        cap_at=NOW + dt.timedelta(seconds=600) if private else None,
    )


def _row() -> Machine:
    return Machine(driver="docker", profile="browser", state=MachineState.IDLE, epoch=1)


def test_a_row_nobody_holds_has_no_lease() -> None:
    assert _row().lease() is None


def test_set_lease_writes_every_column_and_lease_reads_them_back() -> None:
    row, held = _row(), _lease(private=True)
    row.set_lease(held)
    assert row.lease() == held
    assert (row.control_actor_id, row.control_conn, row.control_private) == (
        held.actor_id,
        "tab-00000001",
        True,
    )
    assert (row.control_since, row.control_until, row.control_cap_at) == (held.since, held.until, held.cap_at)


def test_clear_lease_returns_it_and_empties_all_six_columns() -> None:
    row, held = _row(), _lease()
    row.set_lease(held)
    assert row.clear_lease() == held
    assert row.lease() is None
    assert (
        row.control_actor_id,
        row.control_conn,
        row.control_private,
        row.control_since,
        row.control_until,
        row.control_cap_at,
    ) == (None, None, False, None, None, None)
    assert row.clear_lease() is None  # a second clear finds nothing


def test_a_holder_removed_by_set_null_leaves_no_lease() -> None:
    row = _row()
    row.set_lease(_lease())
    row.control_actor_id = None  # what ON DELETE SET NULL does to the row
    assert row.lease() is None


def test_a_move_between_warm_states_keeps_the_lease() -> None:
    row, held = _row(), _lease()
    row.set_lease(held)
    for state in (
        MachineState.RUNNING,
        MachineState.PARKED,
        MachineState.IDLE,
        MachineState.SLEEPING,
        MachineState.WAKING,
    ):
        assert row.move(state, NOW) is None
        assert row.lease() == held


def test_a_move_to_asleep_or_gone_clears_the_lease_and_returns_it() -> None:
    for state in (MachineState.ASLEEP, MachineState.GONE):
        row, held = _row(), _lease()
        row.set_lease(held)
        assert row.move(state, NOW) == held
        assert row.lease() is None and row.state == state and row.state_since == NOW


# ── what a driven Machine may reach: the last step's list while a person drives ────────────────


def _idle_after_a_step(hosts: list[str], *, version: int = 7) -> Machine:
    """An idle Machine with a frame, its last step's list kept on the row and the frame narrowed."""
    row = _row()
    row.frame_ref, row.held_by = "machine-x-1", None
    row.egress_version, row.egress_hosts, row.step_hosts = version, [], list(hosts)
    return row


def test_a_live_lease_on_an_idle_machine_gets_the_last_steps_list() -> None:
    row = _idle_after_a_step(["docs.example", "login.example"])
    row.set_lease(_lease())
    assert row.stepless_egress(NOW) == 8
    assert (row.egress_version, row.egress_hosts) == (8, ["docs.example", "login.example"])
    assert row.stepless_egress(NOW) is None, "already that list: no new version"


def test_with_no_live_lease_an_idle_machine_reaches_nothing() -> None:
    row = _idle_after_a_step(["docs.example"])
    row.egress_hosts = ["docs.example"]
    assert row.stepless_egress(NOW) == 8 and row.egress_hosts == []
    row.set_lease(_lease())
    row.stepless_egress(NOW)
    run_out = NOW + dt.timedelta(seconds=60)  # `until == now` is over
    assert row.stepless_egress(run_out) == 10 and row.egress_hosts == []
    assert row.step_hosts == ["docs.example"], "the last step's list is kept for the next driver"


def test_a_held_a_moving_or_a_frameless_machine_keeps_its_egress() -> None:
    held = _idle_after_a_step(["docs.example"])
    held.set_lease(_lease())
    held.held_by = "step:abc:1"  # a step's list is the step's
    seeded = _idle_after_a_step(["docs.example"])
    seeded.set_lease(_lease())
    seeded.held_by = "seed:def"
    running = _idle_after_a_step(["docs.example"])
    running.set_lease(_lease())
    running.state = MachineState.RUNNING
    frameless = _idle_after_a_step(["docs.example"])
    frameless.set_lease(_lease())
    frameless.frame_ref = None
    for row in (held, seeded, running, frameless):
        assert row.stepless_egress(NOW) is None
        assert (row.egress_version, row.egress_hosts) == (7, [])


def test_a_steps_hold_ending_keeps_its_list_for_a_driver() -> None:
    row = _idle_after_a_step([])
    row.held_by, row.held_since = "step:abc:1", NOW
    row.egress_hosts = ["docs.example", "once.example"]
    row.set_lease(_lease())
    assert row.let_go(NOW) is None, "someone drives: the frame keeps the list the step ran under"
    assert row.step_hosts == ["docs.example", "once.example"] and row.egress_hosts == row.step_hosts
    assert (row.held_by, row.held_since) == (None, None)


def test_a_steps_hold_ending_with_nobody_driving_narrows_the_frame() -> None:
    row = _idle_after_a_step([])
    row.held_by, row.held_since = "step:abc:1", NOW
    row.egress_hosts = ["docs.example"]
    assert row.let_go(NOW) == 8
    assert (row.egress_hosts, row.step_hosts) == ([], ["docs.example"])


def test_a_seeds_hold_ending_leaves_the_last_steps_list_alone() -> None:
    row = _idle_after_a_step(["docs.example"])
    row.held_by = "seed:def"
    assert row.let_go(NOW) is None
    assert (row.step_hosts, row.held_by) == (["docs.example"], None)
