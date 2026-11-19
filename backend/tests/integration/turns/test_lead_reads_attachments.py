# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead reads the turn's attachments itself: "what does this file say" about a text file past 4 KB is a
reply from the file's own words, fenced as data in the Lead's prompt, never a blind reply or a forced plan."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import prismoid.ai.agents.base as agents_base
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, planning
from prismoid.operations import tasks as task_ops
from prismoid.schemas.egress import FENCE_MARK
from prismoid.schemas.inputs import RawInput
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

CLAUSE = "Clause 4: either party may end this agreement on 30 days notice."
CONTRACT = RawInput(filename="contract.txt", data=(CLAUSE + "\n").encode() * 120, media_type="text/plain")


async def test_the_lead_replies_from_an_attachment_past_4_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(CONTRACT.data) > 4096
    async with session_scope() as session:
        actor, _ = await member(session, "Reader")
        await session.flush()
        task = await task_ops.create_task(
            session, actor, goal="What is the notice period in this contract?", inputs=[CONTRACT]
        )
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        view = await planning.draft_plan(session, task.id)
        step = await session.get(PlanStep, uuid.UUID(str(view.steps[0].id)))
        assert step is not None
        await session.commit()
        task_id, step_id = task.id, step.id
    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()

    seen: list[tuple[str, TaskDeps]] = []

    async def run(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        seen.append((prompt, deps))
        return SimpleNamespace(output="The notice period is 30 days (clause 4).")

    monkeypatch.setattr(agents_base, "build", scripted(run))
    async with row_scope(Task, task_id) as session:
        await execution.execute_step(session, task_id, step_id)

    ((prompt, deps),) = seen
    assert deps.lead_facts is not None and deps.lead_facts.inputs_unread == 0
    assert LeadEndingKind.REPLY in deps.lead_facts.allowed_endings()
    fenced = prompt[prompt.index("contract.txt") :]
    assert CLAUSE in fenced and FENCE_MARK in fenced, "the file's words reach the Lead, as fenced data"
