# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The cast of a conversation test: an owner, a published bot, and a chat-born task finished without
running it (`settle`)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import bots as bot_ops
from prismoid.operations import chat as chat_ops
from prismoid.operations import execution
from prismoid.schemas.bots import BotSource, SelectionCaseResult, SelectionReport
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.cast import member

DESCRIPTION = (
    "Writes release notes from shipped work. Load when: a release needs notes. Not for: drafting contracts."
)
BOT_MD = """---
name: {name}
description: >
  {description}
capability: fast-executor
produces: document
tools: [frame.read_file, web.search]
---
Write plainly. Corrections in this conversation are teaching: follow them.
"""


async def an_owner(session: AsyncSession) -> Actor:
    row, _ = await member(session, "Owner")
    await session.flush()
    return row


async def published_bot(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID | None = None, *, md: str = BOT_MD
) -> str:
    name = f"scribe-{uuid.uuid4().hex[:8]}"
    source = BotSource(
        bot_md=md.format(name=name, description=DESCRIPTION),
        cases_yaml=f"selection:\n  - goal: Write the release notes\n    expect: {name}\n",
    )
    row = await bot_ops.push(session, actor=actor, name=name, source=source, project_id=project_id)
    await bot_ops.apply_eval(
        session,
        actor=actor,
        name=name,
        project_id=project_id,
        version=row.version,
        report=SelectionReport(
            bot=name,
            version=row.version,
            passed=1,
            total=1,
            cases=(SelectionCaseResult(goal="Write the release notes", expected=name, chosen=name, ok=True),),
        ),
    )
    await bot_ops.publish(session, actor=actor, name=name, project_id=project_id)
    return name


async def settle(task_id: uuid.UUID, summary: str) -> None:
    """Finish the turn without running it: every step done, then the delivery."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        if task.state is TaskStatus.CREATED:
            task.transition(TaskStatus.PLANNING)  # what run_task's intake effect sets
            await session.flush()
        if task.state is TaskStatus.PLANNING:
            await execution.advance(session, task_id, TaskStatus.RUNNING)
        for step in await PlanStep.for_task(session, task_id):
            if step.step_state not in (StepState.DONE, StepState.SKIPPED):
                step.state = StepState.DONE.value
        await session.flush()
        await execution.deliver(session, task_id, summary)
        await session.commit()
        # what the `conversation_report` effect does after the settle is durable
        task = await session.get(Task, task_id)
        assert task is not None
        await chat_ops.on_task_settled(session, task, outcome="delivered", summary=summary)
        await session.commit()
