# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The express rung: one frameless run of the turn's express step, the Lead's (ADR-092).

No frame, no mounts, no publish, no harvest: the Lead, the thread inline, the ambient facts, one
breath. It ends in one of its endings (`lead_rung.py`), which the completion path commits.

A deliberate sibling of `_attempt` rather than a conditional through it: the standard rung is frame
choreography. What IS shared is the ladder around both (attempts, gotchas, timeouts cost a rung never
the task), the prompt snapshot, the metering and the message-lane streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from pydantic_ai import capture_run_messages
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.ai.context.brief import prompt_for
from prismoid.ai.deps import TaskDeps
from prismoid.conf import settings
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import connectors, context, live, routines, skills
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import sources as sources_ops
from prismoid.operations import thread as thread_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.execution import lead_rung, run
from prismoid.operations.execution.deferrals import AttemptResult
from prismoid.operations.execution.errors import IncompleteStep
from prismoid.operations.execution.express import express_ambient, express_prompt
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.execution.taps import record_prompt, tool_journal
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.plan import StepSpec


async def attempt_express(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    gotchas: list[str],
    *,
    fresh: bool,
    resumed: bool = False,
) -> AttemptResult:
    """One express run, produced or (the Lead's ask) parked; `resumed`: the step woke from a park."""
    if spec.agent is None or spec.contract is None:
        raise IncompleteStep(step.id, "agent" if spec.agent is None else "contract")
    await lead_rung.think(task)  # the first delta, before any read
    wire = live.Wire(
        TaskId(task.id), StepId(step.id), ProjectId(task.project_id) if task.project_id else None
    )
    wire.anchor(task.event_seq)
    pulse = asyncio.create_task(live.heartbeat(wire))
    try:
        # The conversation, inline - the lane's whole priming: the thread IS the gathered context.
        transcript = await thread_ops.render_thread(
            session, task, budget_tokens=thread_ops.EXPRESS_THREAD_TOKENS
        )
        journal = tool_journal(session, task, step)
        reads = await connectors.resolve_for_task(session, task, journal=journal, reads_only=True)
        # What the turn recalled, the author's clock, its services and what a step can do there.
        ambient = await express_ambient(session, task, reach=reads.reach)
        inbox = StepInbox(task.id, step.id, task.turn, told=step.epoch)
        deps = TaskDeps(
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            frame=None,  # the lane's whole thesis
            emit_event=live.emitter(wire),
            emit_usage=usage_ops.emitter(session, own_transaction=True),
            journal_tool=journal,  # the lookup tier's web calls journal like any tool
            keep_page=sources_ops.keeper(task.id),  # its pages are kept for the receipt, as a step's are
            connectors=reads.specs,  # what the task's services serve to read, unasked
            # A steer reaches the run at its safe points; the run starts told up to the step's epoch.
            steering=inbox,
            # The run guard's eyes on the task's cap: the Lead searches and reads, and those cost.
            spend=usage_ops.spend_reader(task.id),
            skill_menu=await skills.menu(session, lead_rung.LEAD),  # what it may read, frozen for the run
            memory_search=context.memory_searcher(task),  # the person's own notes; none in a Project
            # `routine_create`'s hands, on their own session: a routine saved from chat is born paused.
            propose_routine=routines.proposer(task.id, task.creator_actor_id, task.project_id),
        )
        catalog = await model_ops.catalog(session, enabled_only=True)
        order = await routing_ops.order_for(session)
        asked = await routing_ops.settings_for(session)
        turn = await lead_rung.brief(  # the Lead's facts, plan facts, bots and the turn's attachments
            session, task, deps, asked=resumed, catalog=catalog, order=order, settings=asked, reads=True
        )
        runner = agents_base.build(
            lead_rung.LEAD, deps, catalog, order=order, settings=asked, quality=task.quality
        )
        prompt = express_prompt(prompt_for(spec, gotchas), transcript, ambient=lead_rung.tail(turn, ambient))
        await record_prompt(session, task, step, lead_rung.LEAD, prompt)
        await session.commit()  # durable before the run + the connection released (the _attempt law)

        # The express wall bounds the Lead's deciding: its web tools stop there. The run, a reply it is
        # writing included, ends by a reply's wall; cut there, it ends in what it had (`lead_rung.walled`).
        wall = runner.envelope.narrowed(
            max(settings.worker.express_timeout_s, settings.worker.chat_timeout_s)
        )
        log = await run.call_log_for(task, step, fresh=fresh)  # re-entered after a crash unless fresh
        deps.deadline = time.monotonic() + settings.worker.express_timeout_s
        with capture_run_messages() as had:
            try:
                ran = runner.run(prompt, log=log, cancelled=inbox.cancelled)
                result = await asyncio.wait_for(ran, timeout=wall.wall_s)
            except TimeoutError:
                return lead_rung.walled(turn, deps, had)
        return await lead_rung.ended(session, task, step, result.output)
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse
