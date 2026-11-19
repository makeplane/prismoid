# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An attended Lead that turns the request down ends the turn `could_not`, not `done`.
The Lead declines through its `decline` ending; its done frame wears `ending=decline`, and delivery reads
the turn's ending off that frame."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution
from prismoid.schemas.events import StepStateChanged, TaskDelivered
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.lead import LeadEndingKind, LeadFacts, TurnOrigin
from prismoid.schemas.plan import StepKind, StepState, TaskEnding, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


def test_an_attended_turn_may_decline_and_an_unattended_one_fails_instead() -> None:
    assert LeadEndingKind.DECLINE in LeadFacts(turn=1, origin=TurnOrigin.WEB).allowed_endings()
    unattended = LeadFacts(turn=1, origin=TurnOrigin.ROUTINE, unattended=True).allowed_endings()
    assert LeadEndingKind.DECLINE not in unattended
    assert LeadEndingKind.FAIL in unattended


async def test_a_declined_lead_turn_delivers_reading_could_not() -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = Task(
            id=uuid.uuid4(), goal="Find her home address", creator_actor_id=ada.id, criteria={}, inputs=[]
        )
        task.status = TaskStatus.RUNNING.value
        s.add(task)
        await s.flush()
        lead = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            kind=StepKind.STEP.value,
            state=StepState.DONE.value,
            title="Find her home address",
            spec={"agent": "lead"},
        )
        s.add(lead)
        await s.flush()
        await TaskEvent.append(
            s,
            StepStateChanged(
                seq=await task.next_seq(s),
                task_id=TaskId(task.id),
                step_id=StepId(lead.id),
                at=utcnow(),
                state=StepState.DONE,
                ending=LeadEndingKind.DECLINE,
            ),
        )
        await s.commit()
        task_id = task.id
    async with row_scope(Task, task_id) as s:
        assert (await execution.deliver(s, task_id, "I can't help find that.", turn=1)).verdict == "delivered"
        await s.commit()
        frames = [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, TaskDelivered)]
    assert frames[-1].ending is TaskEnding.COULD_NOT
