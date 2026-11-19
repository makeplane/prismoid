# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The budget-park rig: the real `execute_step` against real rows, FakeFrames, the real `build()` (so
the real `RunGuard` and the real spend closure) with a scripted model; spend lands as ledger rows
mid-stream. A plain module, so a test imports it and never another test module; a test module
re-exports the fixtures it uses (`from tests.support.rigs.budget_park_rig import sidecar as sidecar`).
Its family: `tests/integration/gates/test_budget_park.py`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any, Protocol, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, gates
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.plan import StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import member

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]
SITE = "https://admin.example.com/users"


class Sends(Protocol):
    """A dispatcher double that keeps what it sent (`RecordingDispatcher`)."""

    sends: list[tuple[str, object, str]]


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)


@pytest.fixture
def sidecar(fake_frames: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def browser(self: Any, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        if command.get("verb") == "snapshot":
            tree, page = '[e1.1] button "Save"', {"url": SITE, "title": "Users"}
            return {"ok": True, "epoch": 1, "text": tree, "written": tree, "reading": "Save", **page}
        return {"ok": True}

    monkeypatch.setattr(fake_frames, "browser", browser)


@pytest.fixture
def real_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[Stream], None]:
    async def no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    monkeypatch.setattr(ex_harvest, "extract_previews", no_previews)

    def use(stream: Stream) -> None:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))

    return use


async def computer_task(cap: str | None) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A running task with one ready browser step (the operator), capped at `cap` dollars (None: no cap), in a
    fresh workspace: (task id, step id, its creator's actor id)."""
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = Task(
            id=uuid.uuid4(),
            goal="Summarize the top stories",
            creator_actor_id=ada.id,
            criteria={},
            inputs=[],
            budget={"usd": cap, "tokens": None},
        )
        task.status = TaskStatus.RUNNING.value
        s.add(task)
        await s.flush()
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="browse the stories",
            spec=StepSpec(
                kind=StepKind.STEP,
                agent=AgentName("operator"),
                browser=True,
                title="browse the stories",
                contract=Contract(goal="Summarize the top stories"),
            ).model_dump(mode="json"),
            attempt=0,
        )
        s.add(step)
        await s.commit()
        return task.id, step.id, ada.id


async def spend(task_id: uuid.UUID, step_id: uuid.UUID, usd: str) -> None:
    """A model request's bill, stamped with the task's workspace as the real writer stamps it, so
    a workspace's allowance counts it too."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        s.add(
            UsageEvent(
                workspace_id=task.workspace_id,
                task_id=task_id,
                step_id=step_id,
                provider="anthropic",
                model="claude-sonnet-5",
                family="anthropic",
                units={"request": 1},
                cost_usd=Decimal(usd),
                at=utcnow(),
            )
        )
        await s.commit()


async def run_step(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as s:
        return await execution.execute_step(s, task_id, step_id)


async def wake(task_id: uuid.UUID, step_id: uuid.UUID, woken: dict[str, Any]) -> None:
    async with row_scope(Task, task_id) as s:
        await execution.store_answer(s, step_id, woken)
        await s.commit()


def wakes(sent: Sends, step_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], p) for k, p, t in sent.sends if k == f"step-{step_id}" and t == gates.GATE_TOPIC
    ]


async def rows(task_id: uuid.UUID, get: Callable[[Any], Any]) -> Any:
    """What `get` read, detached and loaded: the session's exit expires its rows, so a row read
    after it closes would refresh against no session."""
    async with row_scope(Task, task_id) as s:
        got = await get(s)
        s.expunge_all()
        return got


def returns(messages: list[ModelMessage]) -> dict[str, str]:
    return {
        p.tool_call_id: str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }


def tool(name: str, call_id: str, **args: Any) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}
