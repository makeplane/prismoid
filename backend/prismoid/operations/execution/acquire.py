# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The attempt's first stage: get the step its frame. `acquire` binds the task's Machine (or a
seat's throwaway frame, a step's paper frame, or a park again by its old name), narrates its phases on the progress
tree one row at a time, and turns a frame that cannot be had into the ladder's kind of failure.
Nothing here holds the frame afterwards: the caller's `try/finally` gives it back.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import ModelIncapable
from prismoid.operations import live, machines
from prismoid.operations.execution.device import through_grace


class Phases:
    """The frame's preparation as progress rows: each new phase closes the last, so no row is
    left pending (the tree pairs an `end` with the first pending row). Called inside `acquire`'s
    loop, so it never touches the step's session."""

    def __init__(self, fact: live.ActivityFact, first: str, *, paper: bool = False) -> None:
        self.fact = fact
        self.label = first
        self.at = time.monotonic()
        self.paper = paper  # the step runs on paper (`rung.on_paper`): there is no Machine to prepare

    async def open(self) -> None:
        await self.fact("start", self.label)

    async def on_phase(self, label: str) -> None:
        if label != self.label:
            await self.fact("end", self.label, started=self.at)
            self.label, self.at = label, time.monotonic()
            await self.fact("start", label)

    async def close(self, label: str | None = None) -> None:
        """End the open row, under `label` when the frame names what it became."""
        await self.fact("end", label or self.label, started=self.at)


def phases_for(wire: live.Wire, engine_fact: live.ActivityFact, *, seat: bool, paper: bool = False) -> Phases:
    """A Machine's phases name it (`tool="machine"`): binding a warm one moves it idle -> running
    with no machine.* event, so their `end` is what tells a client to read the chip again. A seat's
    throwaway frame and a step on paper are not the Machine; their phases stay the engine's."""
    if paper:
        return Phases(engine_fact, machines.PAPER_LABEL, paper=True)
    fact = (
        engine_fact if seat else live.activity_emitter(wire, tool="machine", wait_s=settings.machines.wait_s)
    )
    return Phases(fact, "Starting a sandbox" if seat else "Preparing the Machine")


async def acquire(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    profile: str,
    allowed: set[str],
    resume: dict[str, object] | None,
    seat: bool,
    phases: Phases,
) -> machines.Acquired:
    """The step's frame, its preparation narrated. On a resume it binds the park again and pushes
    the list the caller recomputed (a grant accepted while the step sat parked must reach the live
    proxy, not just the toolset's bookkeeping). A failure closes the open row first, so the tree
    never hangs on it."""
    await phases.open()
    if phases.paper and not await machines.holds(session, task.id, step.id):
        # A step the Machine holds (a park, a run a worker died in after a tool took the Machine)
        # goes on there, and its release gives the Machine back; any other step on paper gets a
        # fresh frame (a resume re-mounts its inputs, as on every frame).
        return machines.paper(task, step)
    try:
        return await acquire_for(
            session,
            task,
            step,
            profile=profile,
            allow_domains=sorted(allowed),
            resume=resume,
            seat=seat,
            on_phase=phases.on_phase,
        )
    except Exception as failed:
        await phases.close()
        if isinstance(failed, RuntimeError) and resume is not None:
            # A pre-1b parked frame is gone (a crash's teardown or an operator sweep): the work is
            # lost and we say so, but it costs a RUNG, not the task. A Machine
            # that cannot be had is already an infra `ModelIncapable` from `acquire`; a fresh
            # throwaway frame that cannot boot fails as it always has.
            raise ModelIncapable(str(failed), infra=True) from failed
        if isinstance(failed, ModelIncapable) and failed.context.get("infra") and not seat:
            # The step could not get its Machine: the ladder paces the next rung.
            raise ModelIncapable(str(failed), infra=True, machine=True) from failed
        raise


async def acquire_for(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    profile: str,
    allow_domains: Sequence[str],
    resume: dict[str, object] | None,
    seat: bool,
    on_phase: Callable[[str], Awaitable[None]] | None = None,
) -> machines.Acquired:
    """The step's frame: the Machine, a seat's throwaway frame, or - for a park no
    Machine holds (one from before 1b, or a seat's parked throwaway frame; both are named
    frame-<task>-<step>-<attempt>) - that frame again, by its name.

    On the device a helper that does not answer is waited for, inside the grace (`DEVICE_GRACE_S`):
    each failed `acquire` hands the Machine back as it found it, so asking again is the same claim.
    This runs in the step's own body, never a durable effect - the waits are `acquire`'s own kind
    (it waits up to `wait_s` for a busy Machine), and a worker that dies inside one recovers the
    step, which parks (`execute_step`'s recovered arm)."""
    legacy_ref = None
    if resume is not None and not await machines.holds(session, task.id, step.id):
        legacy_ref = f"frame-{task.id}-{step.id}-{step.attempt}"
    return await through_grace(
        task,
        step,
        functools.partial(
            machines.acquire,
            session,
            task,
            step,
            profile=profile,
            allow_domains=allow_domains,
            seat=seat,
            resuming=resume is not None,
            legacy_ref=legacy_ref,
            on_phase=on_phase,
        ),
    )
