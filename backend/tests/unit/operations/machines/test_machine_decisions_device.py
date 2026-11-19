# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's pure decisions on the device: a device Machine awaiting
Done is never slept by the park hold, and a driver's profile price wins. No I/O - rows and `now` are
arguments. The rig is `tests/support/rigs/machine_decisions_rig.py`."""

import uuid
from decimal import Decimal

from prismoid.schemas.machines import (
    MachineState,
    SweepAction,
    SweepArm,
    parse_prices,
    segment_cost_usd,
    sweep_actions,
)
from tests.support.rigs.machine_decisions_rig import LIMITS, NOW, view


def test_a_device_awaiting_done_is_never_park_hold_slept() -> None:
    """Ruling b (D2 review): a device Machine whose task has an open CONTROL gate waits on its
    creator's Done or the gate's own TTL (the away limit) - never the park hold. Nowhere else."""
    import dataclasses

    from prismoid.schemas.machines import awaits_done

    over = view(MachineState.PARKED, since=1801, active=5000)
    device = dataclasses.replace(over, driver="device", control_open=True)
    no_gate = dataclasses.replace(over, id=uuid.uuid4(), driver="device")
    cloud = dataclasses.replace(over, id=uuid.uuid4(), driver="docker", control_open=True)
    assert awaits_done(device) and not awaits_done(no_gate) and not awaits_done(cloud)
    assert sweep_actions([device, no_gate, cloud], NOW, LIMITS) == [
        SweepAction(SweepArm.PARK_HOLD, no_gate.id, no_gate.epoch),
        SweepAction(SweepArm.PARK_HOLD, cloud.id, cloud.epoch),
    ]


def test_a_driver_profile_price_wins_and_every_other_driver_keeps_the_profiles() -> None:
    """`driver.profile` prices one driver."""
    prices = parse_prices("base=0.0005,browser=0.00075,device.base=0,device.browser=0")
    assert segment_cost_usd(600, "base", prices, "device") == Decimal("0.000000")
    assert segment_cost_usd(600, "browser", prices, "device") == Decimal("0.000000")
    assert segment_cost_usd(600, "base", prices, "docker") == Decimal("0.005000")
    assert segment_cost_usd(120, "browser", prices, "sandbar") == Decimal("0.001500")
    assert segment_cost_usd(60, "code", prices, "device") == Decimal("0.000500"), "no device.code: base"
