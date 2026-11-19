# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step on paper: which steps run with no computer under them, and the frame they get.

A serial step on the base image starts on paper, the step's files in this process's memory
(`ai/frames/paper.py`), whatever its agent holds: on a remote driver it holds no host slot and starts no
boot until a call needs the computer (`capabilities/machine_acquire.py`). A seat keeps its throwaway
frame, an agent on another image and a task on the device keep the Machine (it costs no slot there),
a step that may need the computer on a task that already has a Machine binds it (its `/work` is the
task's),
and a step whose mounts would crowd the worker's memory keeps the Machine
(`execution.handlers.rung.on_paper` sizes them and remembers an overflow).
Nothing is metered on paper: no computer ran.
"""

from __future__ import annotations

from collections.abc import Sequence

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations.machines.acquired import Acquired, AcquiredKind, driver_of
from prismoid.operations.machines.lifecycle import seat_profile
from prismoid.operations.machines.need import agent_needs_computer, takes_machine
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.machines import MachineProfile
from prismoid.schemas.plan import StepSpec

PAPER_LABEL = "Working without a computer"


def starts_on_paper(task: Task, spec: StepSpec, *, seat: bool, has_machine: bool) -> bool:
    """Whether this step starts on paper: a serial base-image step, not a seat or a device step, both switches
    on; once the task `has_machine`, a step that may need the computer goes there at once."""
    cfg = settings.machines
    if not (cfg.paper and cfg.enabled) or seat or driver_of(task) == DEVICE_DRIVER or not takes_machine(spec):
        return False
    if has_machine and agent_needs_computer(str(spec.agent), browser=spec.browser):
        return False
    return seat_profile(task, spec) == MachineProfile.BASE


def fits_on_paper(task: Task, mounted_bytes: Sequence[int]) -> bool:
    """Do the task's attachments and the artifacts the step mounts (`mounts.mounted_artifacts`) fit
    in half the cap? The rest is headroom for the thread, the skills and the deliverable."""
    mounted = sum(int(i.get("bytes", 0)) for i in task.inputs) + sum(mounted_bytes)
    return mounted <= settings.machines.paper_max_bytes // 2


def paper(task: Task, step: PlanStep) -> Acquired:
    """The step's paper frame, named for its attempt. `release` bills nothing for it."""
    frame = frames.new_paper(
        f"{task.id}-{step.id}-{step.attempt}", max_bytes=settings.machines.paper_max_bytes
    )
    return Acquired(frame, kind=AcquiredKind.PAPER, label=PAPER_LABEL, task_id=task.id)
