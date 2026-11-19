# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A turn the Lead plans, for a test that starts from a plan: `draft_plan` seats the Lead, its run is scripted to
end in `submit_plan`, and the real `execute_step` lands the plan in the turn (ADR-092)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import prismoid.ai.agents.base as agents_base
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import execution, planning
from prismoid.schemas.lead import SubmitPlan
from prismoid.schemas.plan import PlanProposal, TaskStatus
from tests.support.runners import scripted

SAY = "Working on it."


async def lead_plans(task_id: uuid.UUID, plan: PlanProposal, *, say: str = SAY) -> uuid.UUID:
    """Plan a PLANNING task's turn through its Lead step, scripted to submit `plan`; returns the Lead's step id."""
    async with row_scope(Task, task_id) as session:
        view = await planning.draft_plan(session, task_id)
        await session.commit()
    lead_id = uuid.UUID(str(view.steps[-1].id))
    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()

    async def run(agent: object, prompt: str, deps: object, **kwargs: object) -> object:
        return SimpleNamespace(output=SubmitPlan(say=say, plan=plan))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(agents_base, "build", scripted(run))
        async with row_scope(Task, task_id) as session:
            await execution.execute_step(session, task_id, lead_id)
    async with row_scope(Task, task_id) as session:
        lead = await session.get(PlanStep, lead_id)
        assert lead is not None and lead.spec["agent"] == "lead", "the turn was not the Lead's"
    return lead_id
