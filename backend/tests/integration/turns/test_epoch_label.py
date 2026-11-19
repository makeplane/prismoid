# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The spend label follows the epoch - the step's, else the task's, else the turn's."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from prismoid.db.base import utcnow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.usage import UsageEvent
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import planning_steps
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import usage as usage_ops
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import StepSpec, TaskStatus
from prismoid.schemas.usage import Unit, UsageRecord
from tests.support.cast import member

pytestmark = pytest.mark.integration


def _spend(task_id: uuid.UUID, cost: str, step_id: uuid.UUID | None = None) -> UsageRecord:
    return UsageRecord(
        task_id=TaskId(task_id),
        step_id=StepId(step_id) if step_id else None,
        provider="local",
        model="m",
        family=Family("qwen"),
        units={Unit.REQUEST: 1},
        cost_usd=Decimal(cost),
        price_version=1,
        at=utcnow() + dt.timedelta(seconds=1),
    )


async def _label(s, row: UsageEvent) -> uuid.UUID | None:  # pyright: ignore[reportMissingParameterType]
    return await s.scalar(select(UsageEvent.actor_id).where(UsageEvent.id == row.id))


async def test_a_turn_opens_an_epoch_and_the_step_label_beats_the_task_label(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"e-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="label", project_id=project.id)
        assert (task.plan_epoch, task.turn_epoch, task.epoch_actor_id) == (1, 1, ada.id)

        step = PlanStep(task_id=task.id, ordinal=0, title="s", epoch=1, epoch_actor_id=bob.id)
        s.add(step)
        await s.flush()
        on_step = await usage_ops.record(s, _spend(task.id, "0.10", step.id))
        on_task = await usage_ops.record(s, _spend(task.id, "0.20"))
        await s.flush()
        assert await _label(s, on_step) == bob.id, "a step's spend wears the epoch it applied"
        assert await _label(s, on_task) == ada.id, "no step: the task's epoch actor"

        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        task.transition(TaskStatus.DELIVERED)
        await s.commit()
        await task_ops.continue_task(s, bob, task.id, goal="more")
        assert (task.plan_epoch, task.turn_epoch, task.epoch_actor_id) == (2, 2, bob.id)

        written = await planning_steps.write_steps(
            s, task, [(StepId(uuid.uuid4()), StepSpec(title="t"))], [], base_ordinal=5
        )
        assert (written[0].epoch, written[0].turn_epoch) == (2, 2)


async def test_by_person_splits_the_task_spend(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        task = await task_ops.create_task(s, ada, goal="split")
        step = PlanStep(task_id=task.id, ordinal=0, title="s", epoch=2, epoch_actor_id=bob.id)
        s.add(step)
        await s.flush()
        await usage_ops.record(s, _spend(task.id, "0.25"))
        await usage_ops.record(s, _spend(task.id, "0.75", step.id))
        await s.flush()
        summary = await usage_ops.for_task(s, task.id)
        assert {(p.name, p.usd) for p in summary.by_person} == {
            ("Ada", Decimal("0.25")),
            ("Bob", Decimal("0.75")),
        }
