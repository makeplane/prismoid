# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the control-gate tests, shared by `test_control_gate.py` and
`test_device_handoff.py`: the real `execute_step` against real rows, a sidecar that answers a page, and
a scripted model over the REAL browser toolset. A plain module, not a test file."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import PrismoidDurability
from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, browser_toolset
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, gates
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.plan import GateAction, StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import bound_scope, member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.runners import runner_for

Turn = Callable[[list[ToolReturnPart]], Awaitable[str | DeltaToolCalls]]
SITE = "https://admin.example.com/users"
_D = r"\d+[hms]( \d+[hms])?"  # held_text: "45s", "1m 40s", "2h 3m"
HANDED_BACK = re.compile(
    rf"^Ada took over and handed back after {_D}\. Take a new snapshot to see where things stand\.$"
)


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)  # navigator steps need the switch


@pytest.fixture
def sidecar(fake_frames: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every verb the Machine's sidecar received, in order: a snapshot answers a page, anything
    else (hold, finalize) answers ok."""
    verbs: list[str] = []

    async def browser(self: Any, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        verbs.append(str(command.get("verb")))
        if command.get("verb") == "snapshot":
            return {"ok": True, "epoch": 1, "text": '[e1.1] button "Save"', "url": SITE, "title": "Users"}
        if command.get("verb") == "inspect":
            return {"ok": True, "role": "", "name": "", "host": "accounts.example.org", "path": "/sign-in"}
        return {"ok": True}

    monkeypatch.setattr(fake_frames, "browser", browser)
    return verbs


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Callable[[Turn], None]:
    """`build()` stubbed to a streaming `FunctionModel` over the REAL browser toolset; each model
    turn asks `turn` what to do, given every tool result so far."""

    async def no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    monkeypatch.setattr(ex_harvest, "extract_previews", no_previews)

    def use(turn: Turn) -> None:
        async def stream(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | DeltaToolCalls]:
            yield await turn(tool_returns(messages))

        def fake_build(name: str, deps: TaskDeps, catalog: object, **_: object) -> AgentRunner:
            assert deps.browser is not None and deps.browser.control is not None, (
                "the Machine's hook is wired"
            )
            toolset = browser_toolset(
                BROWSER_TOOLS, deps.journal_tool, deps.browser, Approvals(tainted=tainted)
            )
            assert toolset is not None
            agent = Agent(
                FunctionModel(stream_function=stream),
                deps_type=TaskDeps,
                output_type=[str, DeferredToolRequests],
                toolsets=[toolset],
                name=name,
                capabilities=[PrismoidDurability()],  # as every built agent: a resume replays its call log
            )
            return runner_for(agent, deps)

        monkeypatch.setattr(agents_base, "build", fake_build)

    return use


def tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def tool_call(tool: str, call_id: str, **args: Any) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id=call_id)}


async def navigator_task() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A personal task - its creator, Ada, may take control - with one READY navigator step.
    Returns (task, step, Ada, workspace)."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = Task(
            id=uuid.uuid4(),
            goal="Set Mary Jackson's status to open",
            creator_actor_id=ada.id,
            criteria={},
            inputs=[],
        )
        task.status = TaskStatus.RUNNING.value  # past planning, as dispatch would leave it
        s.add(task)
        await s.flush()
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="update the record",
            spec=StepSpec(
                kind=StepKind.STEP,
                agent=AgentName("operator"),
                browser=True,
                title="update the record",
                contract=Contract(goal="Set Mary Jackson's status to open"),
            ).model_dump(mode="json"),
            attempt=0,
        )
        s.add(step)
        await s.commit()
        return task.id, step.id, ada.id, workspace.id


async def run_step(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as s:
        return await execution.execute_step(s, task_id, step_id)


async def resolve_gate(
    workspace_id: uuid.UUID, actor_id: uuid.UUID, gate_id: uuid.UUID, action: GateAction
) -> Any:
    async with bound_scope(workspace_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        return await gates.resolve(s, actor, gate_id, action)


async def gate_row(task_id: uuid.UUID, gate_id: uuid.UUID) -> Gate:
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, gate_id)
        assert gate is not None
        s.expunge(gate)
        return gate


async def machine_row(task_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        return row.id, row.state


def wake_of(dispatcher: RecordingDispatcher, gate_id: uuid.UUID) -> dict[str, Any]:
    found = [
        payload
        for _, payload, topic in dispatcher.sends
        if topic == gates.GATE_TOPIC and isinstance(payload, dict) and payload.get("gate_id") == str(gate_id)
    ]
    assert len(found) == 1, dispatcher.sends
    return found[0]


async def resume_step(task_id: uuid.UUID, step_id: uuid.UUID, wake: dict[str, Any]) -> str:
    """What `run_step` does after its `recv`: store the answer, run the ladder again."""
    async with row_scope(Task, task_id) as s:
        await execution.store_answer(s, step_id, wake)
        await s.commit()
    return await run_step(task_id, step_id)


def parked_on(said: str) -> uuid.UUID:
    assert said.startswith(execution.ASKING), said
    return uuid.UUID(said.removeprefix(execution.ASKING))
