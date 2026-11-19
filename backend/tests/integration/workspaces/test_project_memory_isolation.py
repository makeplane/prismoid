# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A reply turn carries what its words recall, and the Lead can search the rest of the person's
memory in any language; a Project turn, a teammate's included, carries no one's personal memory and
has no memory tool. The memory sits after the thread, so the cached prefix holds across turns.

The model is a `FunctionModel`; the ledger, the FTS, the fence and the frozen pack are real.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import build, get
from prismoid.ai.context.thread import render_thread_md
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.memory import MEMORY_SEARCH, SearchMemory
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import MemoryKind, MemoryScope
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import Workspace, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import context as context_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import thread as thread_ops
from prismoid.operations.execution.express import express_ambient, express_prompt
from prismoid.schemas.events import TaskContinued, TaskDelivered
from prismoid.schemas.ids import TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit
from tests.support.cast import member

pytestmark = pytest.mark.integration

TABLES = "Ada wants every comparison as a table."
HINDI = "रिपोर्ट हमेशा हिंदी में लिखें"
CODEWORD = "My codeword is PINEAPPLE-42"
TURN_TWO = "Now compare the two invoice trackers"


async def _turn_two(s: AsyncSession, task: Task) -> str:
    """Deliver turn 1, open turn 2, and freeze turn 2's pack as planning does; the Lead's tail."""
    for event in (
        TaskDelivered(seq=await task.next_seq(s), task_id=TaskId(task.id), at=utcnow(), summary="Done."),
        TaskContinued(
            seq=await task.next_seq(s), task_id=TaskId(task.id), at=utcnow(), message=TURN_TWO, turn=2
        ),
    ):
        await TaskEvent.append(s, event)
    task.turn = 2
    await s.flush()
    transcript = await thread_ops.render_thread(s, task, budget_tokens=thread_ops.EXPRESS_THREAD_TOKENS)
    await context_ops.record(s, task, await context_ops.assemble(s, task, thread=transcript))
    return await express_ambient(s, task)


async def _ada(s: AsyncSession) -> tuple[Actor, Workspace]:
    ada, workspace = await member(s, "Ada")
    for kind, text in (
        (MemoryKind.PREFERENCE, TABLES),
        (MemoryKind.PREFERENCE, HINDI),
        (MemoryKind.FACT, CODEWORD),
    ):
        await memory_ops.create(s, ada, scope=MemoryScope.USER, kind=kind, text=text)
    return ada, workspace


def _row() -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get("lead").model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )


async def _answer(search: SearchMemory | None, query: str) -> tuple[set[str], str | None]:
    """The Lead's tools, and what its one `memory_search` call returned (None: no tool to call)."""
    seen: list[AgentInfo] = []
    returned: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        returns = [
            str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
        if returns:
            returned.extend(returns)
            return ModelResponse(parts=[TextPart("ok")])
        if any(t.name == MEMORY_SEARCH for t in info.function_tools):
            return ModelResponse(parts=[ToolCallPart(MEMORY_SEARCH, {"query": query}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("ok")])

    runner = build("lead", TaskDeps(memory_search=search), [_row()])
    with runner.agent.override(model=FunctionModel(model)):
        await runner.run("go")
    return {t.name for t in seen[0].function_tools}, (returned[0] if returned else None)


async def test_a_personal_reply_turn_carries_its_recall_and_searches_in_any_language(
    silent_dispatcher: None,
) -> None:
    async with session_scope() as s:
        ada, _ = await _ada(s)
        task = await task_ops.create_task(s, ada, goal="compare three trackers")
        tail = await _turn_two(s, task)
        await s.commit()

    assert TABLES in tail, "turn two's words recall the table preference"
    search = context_ops.memory_searcher(task)
    tools, found = await _answer(search, "हिंदी रिपोर्ट")
    assert MEMORY_SEARCH in tools
    assert found is not None and HINDI in found and TABLES not in found
    _, found = await _answer(search, "comparison format")
    assert found is not None and TABLES in found and HINDI not in found


async def test_a_teammates_project_turn_has_no_personal_memory_and_no_tool(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, workspace = await _ada(s)
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"iso-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, ada, goal="compare three trackers", project_id=project.id)
        tail = await _turn_two(s, task)
        await s.commit()

    assert not any(note in tail for note in (TABLES, HINDI, CODEWORD))
    assert context_ops.memory_searcher(task) is None
    tools, found = await _answer(None, "codeword")
    assert MEMORY_SEARCH not in tools and found is None


async def test_a_search_after_a_move_into_a_project_finds_nothing(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await _ada(s)
        task = await task_ops.create_task(s, ada, goal="compare three trackers")
        await s.commit()
        search = context_ops.memory_searcher(task)
        assert search is not None and CODEWORD in await search("codeword")
        task.project_id = (await project_ops.create(s, ada, name=f"mv-{uuid.uuid4().hex[:6]}")).project.id
        await s.commit()

    assert await search("codeword") == [], "the fence is re-read at every search"


async def test_the_recall_rides_after_the_thread_so_the_cached_prefix_holds(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _ = await _ada(s)
        task = await task_ops.create_task(s, ada, goal="compare three trackers")
        tail = await _turn_two(s, task)
        transcript = await thread_ops.render_thread(s, task, budget_tokens=thread_ops.EXPRESS_THREAD_TOKENS)

    thread = render_thread_md(transcript).rstrip()
    prompt = express_prompt(TURN_TWO, transcript, ambient=tail)
    assert prompt.startswith(thread)
    assert prompt.index(TABLES) > len(thread), "the recall rides after the cached thread"
    assert prompt.endswith(tail)
