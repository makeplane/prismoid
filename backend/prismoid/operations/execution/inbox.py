# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The steering inbox one step attempt hands its run (ADR-064): `SteerInbox`, on its own short
sessions - the step's session sits committed and idle across the run (the usage emitter's law), so
nothing here may borrow it. Every read and write is one short unit of work bound to the task's workspace."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from prismoid.db.models.plan import PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import steering
from prismoid.operations.gate_open import pause_slot_free
from prismoid.schemas.plan import TaskStatus
from prismoid.schemas.steering import SafePoint, SteerNote, SteerPending


@dataclass
class StepInbox:
    """What a running step reads at its safe points, and where it says what it took. `told` is the
    newest epoch injected into this run: the capability moves it, a park saves it, a resume restores
    it - so a resumed run is never told the same steer twice."""

    task_id: uuid.UUID
    step_id: uuid.UUID
    turn: int
    told: int
    # May a pause park this run: yes, until a paused safe point re-reads it against the
    # deployment's open pause parks (`pending`) - the cap holds when the pause comes.
    park_pause: bool = field(default=True, init=False)
    _paused_noted: bool = field(default=False, init=False, repr=False)
    _pausing: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def pending(self, after_epoch: int) -> SteerPending:
        """The turn's live steers past `after_epoch`, oldest first (by epoch), and whether the task
        is paused. Applied steers count too: a steer a held call settled is still untold. Paused,
        a run that may park re-reads the slots: equipped under the cap, it must not park once
        other steps took them (the park re-counts under its lock, `gates.pause_slot_free`)."""
        async with row_scope(Task, self.task_id) as session:
            task = await session.get(Task, self.task_id)
            if task is None:
                return SteerPending()
            rows = await Steer.newer_than(session, self.task_id, turn=self.turn, epoch=after_epoch)
            paused = task.state is TaskStatus.PAUSED
            if paused:
                self.park_pause = await pause_slot_free(session, lock=False)
            return SteerPending(
                paused=paused, steers=await steering.notes(session, rows), ended=task.is_terminal()
            )

    async def cancelled(self) -> bool:
        """Was the task cancelled under the run: the cancel poll's read (`ai/agents/cancellation`)."""
        async with row_scope(Task, self.task_id) as session:
            task = await session.get(Task, self.task_id)
            return task is not None and task.state is TaskStatus.CANCELLED

    async def applied(self, steers: Sequence[SteerNote], point: SafePoint) -> None:
        """The run took these steers here. Idempotent: `steering.apply` settles by compare-and-set,
        so a second report of the same steers (a held call, then the request after it) changes
        nothing and the first point stands. The step is read under the task row lock: parallel
        calls report concurrently, and an epoch read before the lock could move the step back."""
        async with row_scope(Task, self.task_id) as session:
            task = await Task.held(session, self.task_id)
            step = await session.get(PlanStep, self.step_id)
            await steering.apply(session, task, step, steers, point)
            await session.commit()

    async def paused_here(self, point: SafePoint) -> None:
        """The pause took hold in this run. Parallel calls in one batch each report it: one fact,
        noted only once it is committed - a write that failed is tried again by the next call."""
        async with self._pausing:  # the batch's calls report at once: one of them writes
            if self._paused_noted:
                return
            async with row_scope(Task, self.task_id) as session:
                task = await session.get(Task, self.task_id)
                if task is None:
                    return
                await steering.pause_applied(session, task, self.step_id, point)
                await session.commit()
            self._paused_noted = True
