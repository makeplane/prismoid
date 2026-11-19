# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine, as the task page and the CLI see it (ADR-063). Never the frame
reference, an object key, the cipher key id or a cloud driver: only `device` is named."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from prismoid.api.serializers.screen import ScreenControlOut, control_out
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.machines import FrameDriverName, MachineSummary


class MachineOut(BaseModel):
    state: Literal["waking", "running", "parked", "idle", "sleeping", "asleep", "gone"]
    profile: str
    state_since: dt.datetime
    last_active_at: dt.datetime
    checkpoint_at: dt.datetime | None = None
    checkpoint_bytes: int | None = None
    browser_saved: bool = False
    warm_seconds: int = 0
    cost_usd: Decimal = Decimal(0)  # the warm time this Machine has cost so far
    # The live control lease: who drives the browser. Null when nobody does.
    control: ScreenControlOut | None = None
    # The computer a device Machine runs on. Null on a cloud driver.
    device_name: str | None = None
    # `device` for a Machine on the user's own computer: no screen, its browser is a window there:
    # the page shows "Browser on <device name>" instead. Null on a cloud driver.
    driver: Literal[FrameDriverName.DEVICE] | None = None


def machine_out(found: MachineSummary) -> MachineOut:
    return MachineOut(
        state=found.state.value,
        profile=found.profile,
        state_since=found.state_since,
        last_active_at=found.last_active_at,
        checkpoint_at=found.checkpoint_at,
        checkpoint_bytes=found.checkpoint_bytes,
        browser_saved=found.browser_saved,
        warm_seconds=found.warm_seconds,
        cost_usd=found.cost_usd.quantize(Decimal("0.000001")),
        control=control_out(found.control) if found.control is not None else None,
        device_name=found.device_name,
        driver=DEVICE_DRIVER if found.driver == DEVICE_DRIVER else None,
    )


def machine_out_or_none(found: MachineSummary | None) -> MachineOut | None:
    return machine_out(found) if found is not None else None
