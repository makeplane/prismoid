# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A gate answered, as data: what a person (or the clock) does when a turn stops to ask.

A turn that opens a gate parks its job on the durable engine's mailbox. In a scenario the answer is
an act at that park (`people.Edge.PARK`): the real operation, from the task's owner, and the real
wake the engine then sends is what ends the wait.

    accepts(q1="blue")   the owner answers the question (or approves, or raises the budget)
    declines()           the owner says proceed without me (or deny, or keep the cap)
    cancels_gate()       the owner cancels from the gate's own card
    lapses()             nobody comes: the sweep runs at a time one second past the gate's expiry,
                         so a day's wait is a parameter and not a wait

`at_park(*acts)` is the stage: the nth park gets the nth act.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import gates
from prismoid.schemas.plan import GateAction
from tests.contract.harness.people import Act, Edge, Room, Stage


def at_park(*acts: Act) -> Stage:
    return Stage(at={Edge.PARK: acts})


async def _open_gate(room: Room) -> tuple[uuid.UUID, dt.datetime | None]:
    """The one gate the task is parked on: its id and its clock."""
    async with row_scope(Task, room.task_id) as session:
        (gate,) = await Gate.open_for_task(session, room.task_id)
        return gate.id, gate.expires_at


def _owner_acts(action: GateAction, content: dict[str, Any] | None = None) -> Act:
    async def act(room: Room) -> None:
        gate_id, _ = await _open_gate(room)
        async with row_scope(Task, room.task_id) as session:
            task = await session.get(Task, room.task_id)
            assert task is not None
            owner = await session.get(Actor, task.creator_actor_id)
            assert owner is not None
            await gates.resolve(session, owner, gate_id, action, content)

    return act


def accepts(**answers: Any) -> Act:
    return _owner_acts(GateAction.ACCEPT, answers or None)


def declines() -> Act:
    return _owner_acts(GateAction.DECLINE)


def cancels_gate() -> Act:
    return _owner_acts(GateAction.CANCEL)


async def lapses(room: Room) -> None:
    """The clock runs out: the real sweep, given a `now` past the open gate's expiry."""
    _, expires = await _open_gate(room)
    assert expires is not None, "a gate with no clock never lapses"
    async with session_scope(platform=True) as session:
        swept = await gates.sweep(session, expires + dt.timedelta(seconds=1))
    assert swept, "the clock ran out and the sweep closed nothing: the gate's policy is to wait"
