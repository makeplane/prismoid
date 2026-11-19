# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What `acquire` hands a step, and what a claim hands the bind or the wake that follows it.

The values every phase of a step's acquire shares (ADR-063): the claim's decision (`BindPlan`,
`WakePlan`, `Evict`), the hold a step runs under (`Acquired`), and the one rule for which driver a
task's frames are made on (`driver_of`). Nothing here locks a row or calls a frame."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations.machines.screen import has_screen
from prismoid.operations.machines.sleep import SleepClaim
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.machines import MachineProfile, SleepReason

# A Machine is never slept under its driver: the two hand-backs that would
# sleep one - a profile upgrade, an airtight re-create - wait for the lease to end (bounded by
# `wait_s`). This is what the step fails with if the wait runs out.
DRIVING = "Waiting: someone is driving the browser"


class AcquiredKind(StrEnum):
    """What a step's frame is: the task's Machine, a seat's or a rolled-back step's throwaway
    frame, a park from before Machines by its old name, or paper (no computer)."""

    MACHINE = "machine"
    SEAT = "seat"
    THROWAWAY = "throwaway"
    LEGACY = "legacy"
    PAPER = "paper"


@dataclass(slots=True)
class Acquired:
    frame: frames.FrameDriver
    kind: AcquiredKind
    label: str  # the progress line the step shows
    machine_id: uuid.UUID | None = None
    epoch: int = 0
    holder: str | None = None
    woke: bool = False
    woke_from_park: bool = False
    changed: list[str] | None = None  # a retry's listing; None unless it is one
    started: float = field(default_factory=time.monotonic)  # a throwaway frame's meter
    task_id: uuid.UUID | None = None
    profile: str = MachineProfile.BASE

    @property
    def screen(self) -> bool:
        """People can watch this frame: only the task's Machine, and only on a
        driver whose screen door is open (`has_screen`). The frame names its driver - the row's, for
        a Machine (ADR-063). Without one a step's browser has no lease to yield to
        and nobody to hand it to, and publishes no actions."""
        return self.kind == AcquiredKind.MACHINE and has_screen(self.profile, self.frame.driver)

    @property
    def device_window(self) -> bool:
        """The task's browser is a window on the user's own computer:
        the task's Machine, on the device, with the browser profile. It has no screen and no lease -
        the person is looking at the real window - so a hand-off waits for their Done instead."""
        return (
            self.kind == AcquiredKind.MACHINE
            and self.profile == MachineProfile.BROWSER
            and self.frame.driver == DEVICE_DRIVER
        )

    @property
    def hands_off(self) -> bool:
        """A person can be handed this browser: they watch its screen, or it is their own window."""
        return self.screen or self.device_window


@dataclass(frozen=True, slots=True)
class BindPlan:
    """A claim found the Machine warm and took its hold: bind its frame."""

    machine_id: uuid.UUID
    epoch: int
    ref: str | None
    profile: str
    egress_version: int
    egress_hosts: list[str]
    was_parked: bool
    driver: str  # the row's: the driver that made this frame (ADR-063)


@dataclass(frozen=True, slots=True)
class WakePlan:
    """A claim moved the Machine to `waking` at a new epoch under its hold: start its frame."""

    machine_id: uuid.UUID
    epoch: int
    profile: str
    checkpoint_object: str | None
    checkpoint_sha256: str | None
    browser_object: str | None
    egress_version: int
    egress_hosts: list[str]
    gone_reason: str | None
    created: bool
    from_park: bool
    # A natively asleep row's computer (ADR-063). `resume` it when nothing must
    # change; otherwise the wake rebuilds from the checkpoint and destroys it once the row names the
    # new frame. A wake that fails before then, without the provider saying it is gone, puts the row
    # back naming it with the profile it was made with (ADR-063).
    native_ref: str | None = None
    native_driver: str | None = None
    resume: bool = False
    native_profile: str | None = None


@dataclass(frozen=True, slots=True)
class Evict:
    """A Machine whose sleep was claimed in the committed transaction: enqueue its job."""

    machine_id: uuid.UUID
    epoch: int
    reason: SleepReason
    claim: SleepClaim


class Retry(Exception):
    """acquire's loop should claim again: the Machine was handed back to be re-created, or it
    changed under a bind. The message is what the step fails with if the wait runs out."""


def recording_tag(step: PlanStep) -> str:
    """Recording folders are per step and attempt on a Machine."""
    return f"{step.id.hex[:8]}-{step.attempt}"


def driver_of(task: Task) -> str:
    """The driver a task's new frames are made on: its own (`device` in local mode),
    else this worker's - so a task never moves between the device and a cloud Machine."""
    return driver_named(task.machine_driver)


def driver_named(machine_driver: str | None) -> str:
    """`driver_of` for a task's recorded `machine_driver`, where there is no row to hand (a create
    deciding, a query over many tasks): the one rule, in one place."""
    return machine_driver or settings.worker.frame_driver
