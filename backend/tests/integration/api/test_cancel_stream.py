# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Cancel stops the stream (ADR-064, ADR-089): a step's run polls its task once a second, and the real
cancel door fires its `CancellationToken`. The provider is asked for nothing past the ack and one poll,
the message lane still ends with its `message.end`, and the cut model request leaves no result row in
the call log the cancel forgot."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents import cancellation
from prismoid.ai.agents.run import AgentRunner, TapSlot
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import steering
from prismoid.operations.call_log import PgCallLog
from prismoid.operations.execution.inbox import StepInbox
from prismoid.schemas.call_log import CallPhase
from prismoid.schemas.events import MessageDelta, MessageEnd, MessageStart
from prismoid.schemas.ids import StepId, TaskId
from tests.support.rigs.machine_execution_rig import task_with_steps
from tests.support.runners import PLAIN

pytestmark = pytest.mark.integration

_SLACK_S = 0.5  # the cancel's delivery and the stream's teardown, past one poll
_WORDS = 400  # an 8 s answer at 20 ms a word: far longer than any cancel may take


async def _cancel(task_id: Any) -> None:
    """The real cancel door's machinery, as the task's creator."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        actor = await session.get(Actor, task.creator_actor_id)
        assert actor is not None
        await steering.cancel_task_row(session, task, actor)


@pytest.mark.usefixtures("recording_dispatcher")
async def test_cancel_mid_stream_stops_the_spend_and_ends_the_stream() -> None:
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        store = PgCallLog(task.workspace_id)
    sent: list[float] = []
    streaming = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        for n in range(_WORDS):
            sent.append(time.monotonic())
            if n == 10:
                streaming.set()
            yield f"word{n} "
            await asyncio.sleep(0.02)

    frames: list[Any] = []

    async def emit(frame: Any) -> None:
        frames.append(frame)

    slot = TapSlot()
    agent: Agent[TaskDeps, Any] = Agent(
        FunctionModel(stream_function=stream),
        name="drill",
        deps_type=TaskDeps,
        capabilities=[PrismoidDurability(event_stream_handler=slot)],
    )
    deps = TaskDeps(task_id=TaskId(task_id), step_id=StepId(step_id), emit_event=emit)
    runner = AgentRunner(agent, deps, PLAIN, live=slot)
    scope = await CallLogScope.open(store, StepId(step_id), fresh=False)
    inbox = StepInbox(task_id, step_id, 1, told=0)

    run = asyncio.create_task(runner.run("write the memo", log=scope, cancelled=inbox.cancelled))
    await streaming.wait()
    await _cancel(task_id)
    acked = time.monotonic()
    with pytest.raises(ModelIncapable, match="cancelled"):
        await run

    late = [t - acked for t in sent if t > acked + cancellation.POLL_S + _SLACK_S]
    assert not late, f"the provider was still asked for words {late[:3]} s after the cancel"
    assert len(sent) < _WORDS
    kinds = [type(f) for f in frames if isinstance(f, MessageStart | MessageDelta | MessageEnd)]
    assert kinds[0] is MessageStart and MessageDelta in kinds
    assert kinds[-1] is MessageEnd, "a cut stream still ends with its closing frame"
    rows = (await store.latest(StepId(step_id))).entries
    cut = [unit for unit, phase in rows if "model.request" in unit and phase == CallPhase.RESULT]
    assert not cut, f"the cut request left a result row: {cut}"
