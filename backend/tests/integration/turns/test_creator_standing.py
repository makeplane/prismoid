# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker refuses work whose creator is suspended or gone.

The request side cancels a suspended user's tasks and a departing member's tasks; the worker rebuilds
every step and turn from the task row, so this is the backstop for work queued, parked or minted in the
gap. Suspension and removal are written straight to the rows, so nothing here leans on the doors.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls
from sqlalchemy import func, select

from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import HANDBACKS, PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.models.workspace import Workspace, WorkspaceMember
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import effects
from prismoid.operations import connectors as connector_ops
from prismoid.operations import execution, standing
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import equip
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult
from prismoid.schemas.events import TaskStatusChanged
from prismoid.schemas.plan import GateKind, GateSpec, GateState, StepState, TaskStatus
from prismoid.schemas.standing import Standing
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.rigs.budget_park_rig import SITE, Stream, rows, run_step, tool
from tests.support.rigs.budget_park_rig import computer_task as _task
from tests.support.rigs.budget_park_rig import computer_use_on as computer_use_on
from tests.support.rigs.budget_park_rig import real_agent as real_agent
from tests.support.rigs.budget_park_rig import sidecar as sidecar

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

MCP = "https://mcp.example.com/mcp"


async def _suspend(task_id: uuid.UUID) -> None:
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        who = await s.get(Actor, task.creator_actor_id)
        assert who is not None
        who.status = "suspended"
        await s.commit()


async def _remove(task_id: uuid.UUID) -> None:
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        await WorkspaceMember.remove(s, task.workspace_id, task.creator_actor_id)
        await s.commit()


def _counting(real_agent: Callable[[Stream], None]) -> list[int]:
    calls: list[int] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        calls.append(1)
        yield tool(
            "done",
            "d1",
            status="done",
            summary="Summarized 30 stories.",
            proof={"url": SITE, "page_text": "Save"},
        )

    real_agent(stream)
    return calls


async def _ended(task_id: uuid.UUID) -> tuple[TaskStatus, list[Any]]:
    task: Task = await rows(task_id, lambda s: s.get(Task, task_id))
    events: list[TaskEvent] = await rows(task_id, lambda s: TaskEvent.replay(s, task_id))
    return task.state, events


async def test_a_suspended_creators_queued_step_never_runs(
    fake_frames: Any, sidecar: None, real_agent: Callable[[Stream], None]
) -> None:
    calls = _counting(real_agent)
    task_id, step_id, _ = await _task("5")
    await _suspend(task_id)

    said = await run_step(task_id, step_id)

    state, events = await _ended(task_id)
    assert state is TaskStatus.CANCELLED and calls == [], "no model call was made"
    assert said.startswith("(the task ended")
    cancelled = [e for e in events if isinstance(e, TaskStatusChanged) and e.status is TaskStatus.CANCELLED]
    assert cancelled and all(e.actor is None for e in cancelled), "the cancel names nobody"


async def test_a_removed_members_parked_task_does_not_resume(
    fake_frames: Any, sidecar: None, real_agent: Callable[[Stream], None]
) -> None:
    calls = _counting(real_agent)
    task_id, step_id, _ = await _task("5")
    async with session_scope() as s:  # parked on an open question, as a person left it
        task = await s.get(Task, task_id)
        assert task is not None
        task.transition(TaskStatus.WAITING)
        s.add(
            Gate(
                task_id=task_id,
                kind=GateKind.INPUT.value,
                state=GateState.OPEN.value,
                spec=GateSpec(kind=GateKind.INPUT, title="Which site?", reason="asked").model_dump(
                    mode="json"
                ),
                expires_at=None,
            )
        )
        await s.commit()
    await _remove(task_id)

    # the park's resume: the intake answer comes in after they left
    assert await effects.apply_clarification(str(task_id), None) == "cancelled"

    state, _ = await _ended(task_id)
    assert state is TaskStatus.CANCELLED and calls == []
    open_gates = await rows(task_id, lambda s: Gate.open_for_task(s, task_id))
    assert not open_gates, "the park's gate closed with the cancel"
    assert await run_step(task_id, step_id) and calls == [], "and its step ejects at the door"


async def test_a_replayed_park_is_handed_back_before_the_refusal(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher
) -> None:
    """A step parked on a question, then its person left. The workflow's replay re-issues the park
    first, because its journal holds the recv there: the refusal and the cancel's wake wait until the
    replay has caught up, since a send at a replayed position breaks the journal."""
    task_id, step_id, _ = await _task("5")
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        step = await s.get(PlanStep, step_id)
        assert task is not None and step is not None
        task.transition(TaskStatus.WAITING)
        gate = Gate(
            task_id=task_id,
            step_id=step_id,
            kind=GateKind.INPUT.value,
            state=GateState.OPEN.value,
            spec=GateSpec(kind=GateKind.INPUT, title="Which site?", reason="asked").model_dump(mode="json"),
            expires_at=None,
        )
        s.add(gate)
        await s.flush()
        gate_id = gate.id
        step.state = StepState.RUNNING.value
        step.resume_state = {HANDBACKS: [f"park:{gate_id}"]}
        await s.commit()
    await _remove(task_id)

    async with row_scope(Task, task_id) as s:
        replayed = await execution.execute_step(s, task_id, step_id, handed_back=0)
    state, _ = await _ended(task_id)
    assert replayed == f"{execution.ASKING}{gate_id}", "the replay re-issues the recorded park"
    assert state is TaskStatus.WAITING and recording_dispatcher.sends == [], "nothing sent mid-replay"

    async with row_scope(Task, task_id) as s:
        caught_up = await execution.execute_step(s, task_id, step_id, handed_back=1)
    state, _ = await _ended(task_id)
    assert caught_up.startswith("(the task ended") and state is TaskStatus.CANCELLED
    assert f"step-{step_id}" in [k for k, _, _ in recording_dispatcher.sends], "the cancel wakes the park"


async def _noop_fact(*a: Any, **k: Any) -> None:
    return None


async def _mount(
    task_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> tuple[tuple[Any, ...], list[str], list[tuple[str, str]]]:
    """What a step mounts for the task, which resolvers the mount even asked, and the connectors it
    journaled as dropped (name, why)."""
    asked: list[str] = []
    dropped: list[tuple[str, str]] = []

    async def journal(name: str, *a: Any, ok: bool = True, denied_reason: str = "", **k: Any) -> None:
        if not ok:
            dropped.append((name, denied_reason))

    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(tools=(ConnectorToolInfo(name="search"),))

    real_task, real_bot = connector_ops.resolve_for_task, connector_ops.resolve_for_bot

    async def for_task(*a: Any, **k: Any) -> Any:
        asked.append("task")
        return await real_task(*a, **k)

    async def for_bot(*a: Any, **k: Any) -> Any:
        asked.append("bot")
        return await real_bot(*a, **k)

    monkeypatch.setattr(connector_ops, "probe_server", probe)
    monkeypatch.setattr(connector_ops, "resolve_for_task", for_task)
    monkeypatch.setattr(connector_ops, "resolve_for_bot", for_bot)
    mount: Any = equip.mount_connectors  # the one door both resolvers sit behind
    bot: Any = SimpleNamespace(
        manifest=SimpleNamespace(connectors=["gmail"]), owner_actor_id=None, name="bot"
    )
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        specs = await mount(
            s,
            task,
            (bot,),
            journal=journal,
            fact=_noop_fact,
        )
    return tuple(specs), asked, dropped


async def _with_connector(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID]:
    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(tools=(ConnectorToolInfo(name="search"),))

    monkeypatch.setattr(connector_ops, "probe_server", probe)
    async with session_scope() as db:
        ada, _ = await member(db, "Ada")
        await db.commit()
        await connector_ops.create_connector(db, ada, server_url=MCP, name="Gmail")
        task = await task_ops.create_task(db, ada, goal="mail", connectors=["gmail"])
        assert task.connector_ids
        return task.id, ada.id


async def test_connectors_resolve_to_nothing_for_a_suspended_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, ada_id = await _with_connector(monkeypatch)
    specs, asked, _ = await _mount(task_id, monkeypatch)
    assert [s.slug for s in specs] == ["gmail"] and asked  # the control: an active creator mounts

    async with session_scope() as db:
        ada = await db.get(Actor, ada_id)
        assert ada is not None
        ada.status = "suspended"
        await db.commit()
    specs, asked, dropped = await _mount(task_id, monkeypatch)
    assert specs == () and asked == []
    assert dropped == [
        ("connector.gmail", "the task's creator is suspended, and a connector is their credential")
    ]


async def test_a_left_creators_connectors_and_bot_connectors_do_not_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, ada_id = await _with_connector(monkeypatch)
    async with session_scope() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        await WorkspaceMember.remove(db, task.workspace_id, ada_id)
        await db.commit()
    specs, asked, dropped = await _mount(task_id, monkeypatch)
    assert specs == () and asked == [], "neither the task's attaches nor the bot's declared slugs resolve"
    assert dropped == [
        ("connector.gmail", "the task's creator left this workspace, and a connector is their credential")
    ], "each a journaled drop naming why, the attach and the bot's slug once"


async def test_a_fresh_turn_of_a_gone_person_makes_no_model_call_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dequeue mark refuses first: no clarifier, no namer, no plan - and no gate to knock an inbox."""
    import prismoid.ai.agents.base as agents_base

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a model was resolved for a gone person's turn")

    monkeypatch.setattr(agents_base, "resolve", never)
    async with session_scope() as db:
        ada, _ = await member(db, "Ada")
        await db.commit()
        task = await task_ops.create_task(db, ada, goal="a task queued before they were suspended")
        task_id = task.id
        ada.status = "suspended"
        await db.commit()

    assert await effects.mark_dequeued(str(task_id), 1) is True

    state, _ = await _ended(task_id)
    assert state is TaskStatus.CANCELLED
    async with session_scope() as db:
        assert (
            await db.scalar(select(func.count()).select_from(UsageEvent).where(UsageEvent.task_id == task_id))
            == 0
        )
        assert await db.scalar(select(func.count()).select_from(Gate).where(Gate.task_id == task_id)) == 0


async def test_a_teammates_follow_up_on_a_task_whose_creator_left_runs_without_their_connectors(
    fake_frames: Any, sidecar: None, real_agent: Callable[[Stream], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting(real_agent)
    task_id, step_id, ada_id = await _task("5")
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        ws = await s.get(Workspace, task.workspace_id)
        bea, _ = await member(s, "Bea", workspace=ws)
        task.turn_actor_id = bea.id  # her follow-up is the current turn
        task.connector_ids = [str(uuid.uuid4())]
        await WorkspaceMember.remove(s, task.workspace_id, ada_id)
        await s.commit()

    await run_step(task_id, step_id)

    state, _ = await _ended(task_id)
    assert state is not TaskStatus.CANCELLED and calls == [1], "the turn runs for the actor it spends for"
    specs, asked, _ = await _mount(task_id, monkeypatch)
    assert specs == () and asked == [], "the left creator's credentials stay unmounted"


async def test_a_suspended_creators_own_queued_turn_is_refused(
    fake_frames: Any, sidecar: None, real_agent: Callable[[Stream], None]
) -> None:
    calls = _counting(real_agent)
    task_id, step_id, ada_id = await _task("5")
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.turn_actor_id = ada_id
        await s.commit()
    await _suspend(task_id)

    await run_step(task_id, step_id)

    state, _ = await _ended(task_id)
    assert state is TaskStatus.CANCELLED and calls == []


async def test_an_active_member_runs_as_before(
    fake_frames: Any, sidecar: None, real_agent: Callable[[Stream], None]
) -> None:
    calls = _counting(real_agent)
    task_id, step_id, _ = await _task("5")
    assert await effects.mark_dequeued(str(task_id), 1) is None

    await run_step(task_id, step_id)

    state, _ = await _ended(task_id)
    assert state is not TaskStatus.CANCELLED and calls == [1]
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        assert await standing.turn_standing(s, task) is Standing.ACTIVE


async def test_standing_names_why(fake_frames: Any) -> None:
    async with session_scope() as s:
        ada, ws = await member(s, "Ada")
        await s.commit()
        task = await task_ops.create_task(s, ada, goal="x")
        assert await standing.creator_standing(s, task) is Standing.ACTIVE
        await WorkspaceMember.remove(s, ws.id, ada.id)
        await s.commit()
        assert (
            await standing.turn_standing(s, task) is Standing.LEFT
            and await standing.creator_standing(s, task) is Standing.LEFT
        )
        ada.status = "suspended"
        await s.commit()
        assert await standing.creator_standing(s, task) is Standing.SUSPENDED
