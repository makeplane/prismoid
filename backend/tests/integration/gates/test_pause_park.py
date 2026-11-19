# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A paused task's running step parks at its next tool dispatch (ADR-064): a question-less PAUSE
gate that never times out, the task still `paused`, the Machine still `parked`. Resume continues
the same run and tells every deferred call it did not run; cancel closes it. A step that cannot
park (the Lead's express step) pauses at the frontier instead.

The real `execute_step` against real rows, every frame a `FakeFrame`, and the real `build()` with
a scripted model, so the real `Steering` capability and the real inbox decide the park. The step
workflow (`run_step`) is DBOS; its loop is walked here by hand: the recorded wake, `store_answer`,
and `execute_step` again (or `close_cancelled_park` on a cancel)."""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.tool_manager import ToolManager
from sqlalchemy import text

import prismoid.ai.agents.base as agents_base
from prismoid.ai.tools.ask import ASK_USER
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, ModelIncapable, ValidationFailed
from prismoid.jobs import durable, effects
from prismoid.operations import execution, gates, machines, planning, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution.inbox import StepInbox
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import (
    GateOpened,
    GateResolved,
    PauseApplied,
    StepStateChanged,
    TaskDelivered,
    TaskStatusChanged,
)
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.machines import MachineState
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.park import ParkKind, park_of
from prismoid.schemas.plan import GateAction, GateKind, GateState, StepKind, StepSpec, StepState, TaskStatus
from prismoid.schemas.steering import REJECT_FAILED, SafePoint
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("on_the_machine")]

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]

ADA_PAUSED_BOB_RESUMED = (
    "Paused before this ran (Ada paused, Bob resumed). It did not run; re-issue it if it still fits."
)
ADA_PAUSED = "Paused before this ran (Ada paused). It did not run; re-issue it if it still fits."


def _spec(title: str) -> dict[str, Any]:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("maker"),
        title=title,
        contract=Contract(
            goal=title,
            acceptance=["it exists"],
            inputs=[],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=title),
        ),
    ).model_dump(mode="json")


async def _running_step() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ada's Project task, Bob an editor there, one READY coder step. (task, step, ada, bob)"""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"p-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, ada, goal="a memo", project_id=project.id, clarify="never")
        task.transition(TaskStatus.PLANNING)
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="memo",
            spec=_spec("memo"),
            attempt=0,
            epoch=task.plan_epoch,
            turn_epoch=task.turn_epoch,
        )
        s.add(step)
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        return task.id, step.id, ada.id, bob.id


async def _as(actor_id: uuid.UUID, task_id: uuid.UUID, verb: Callable[..., Any]) -> Any:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        return await verb(s, actor, task_id)


async def _pause(actor_id: uuid.UUID, task_id: uuid.UUID) -> None:
    await _as(actor_id, task_id, steering.pause)


async def _resume(actor_id: uuid.UUID, task_id: uuid.UUID) -> None:
    await _as(actor_id, task_id, steering.resume)


async def _run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _wake(task_id: uuid.UUID, step_id: uuid.UUID, wake: dict[str, Any]) -> None:
    """`run_step`'s half after its recv returns: the wake goes where the resume finds it."""
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, wake)
        await session.commit()


def _step_wakes(sent: RecordingDispatcher, step_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], p) for k, p, t in sent.sends if k == f"step-{step_id}" and t == gates.GATE_TOPIC
    ]


async def _gate(task_id: uuid.UUID, gate_id: str) -> Gate:
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, uuid.UUID(gate_id))
        assert gate is not None
        s.expunge(gate)
        return gate


async def _task(task_id: uuid.UUID) -> Task:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        s.expunge(task)
        return task


async def _step(task_id: uuid.UUID, step_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        s.expunge(step)
        return step


async def _machine(task_id: uuid.UUID) -> Machine:
    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        s.expunge(row)
        return row


async def _events[E](task_id: uuid.UUID, kind: type[E]) -> list[E]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, kind)]


async def _gates_of(task_id: uuid.UUID) -> list[Gate]:
    async with row_scope(Task, task_id) as s:
        rows = await Gate.for_task(s, task_id)
        for row in rows:
            s.expunge(row)
        return rows


def _returns(messages: list[ModelMessage]) -> dict[str, str]:
    """What each tool call came back with, by call id."""
    return {
        p.tool_call_id: str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }


def _told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


def _calls(*calls: tuple[str, dict[str, Any], str]) -> DeltaToolCalls:
    return {
        i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)
        for i, (name, args, call_id) in enumerate(calls)
    }


WRITE = ("write_file", {"path": "/work/memo.md", "content": "EUR 10"})
PUBLISH = ("publish", {"path": "/work/memo.md", "type": "document", "title": "Memo"})


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


@pytest.fixture
def real_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[Stream], None]:
    """The real `build()` with a scripted model (a step agent streams, so `stream_function`)."""
    monkeypatch.setattr("prismoid.operations.execution.harvest.extract_previews", _no_previews)

    def use(stream: Stream) -> None:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))

    return use


def _wrote_memo(fake_frames: Any) -> bool:
    return any("/work/memo.md" in f.files for f in fake_frames.world.values())


async def _parked_on_a_pause(
    real_agent: Callable[[Stream], None], requests: list[list[ModelMessage]]
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Ada pauses while the model chooses a write: the step parks on the PAUSE gate. After a
    resume the model re-issues the write, publishes, and finishes."""
    task_id, step_id, ada_id, bob_id = await _running_step()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            await _pause(ada_id, task_id)  # Ada pauses while the model chooses
            yield _call(*WRITE, "w1")
        elif n == 2:
            yield _call(*WRITE, "w2")  # the re-issue
        elif n == 3:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    outcome = await _run(task_id, step_id)
    assert outcome.startswith(execution.ASKING)
    return task_id, step_id, ada_id, bob_id, outcome.removeprefix(execution.ASKING)


# ── the park ───────────────────────────────────────────────────────────────────────────────────


async def test_a_pause_parks_the_step_at_its_next_tool_dispatch(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _, gate_id = await _parked_on_a_pause(real_agent, requests)

    gate = await _gate(task_id, gate_id)
    assert gate.kind == GateKind.PAUSE.value and gate.state == GateState.OPEN.value
    assert gate.expires_at is None and gate.step_id == step_id
    assert gate.spec["title"] == "Paused" and gate.spec["questions"] is None
    (opened,) = await _events(task_id, GateOpened)
    assert opened.kind is GateKind.PAUSE and opened.questions is None and opened.expires_at is None
    assert opened.step_id == step_id, "the record files the pause under the step it parked"

    assert (await _task(task_id)).state is TaskStatus.PAUSED, "a pause park never says waiting"
    statuses = [e.status for e in await _events(task_id, TaskStatusChanged)]
    assert TaskStatus.WAITING not in statuses
    assert (await _machine(task_id)).state == MachineState.PARKED

    (fact,) = await _events(task_id, PauseApplied)
    assert fact.safe_point is SafePoint.BEFORE_TOOL and fact.step_id == step_id
    assert not _wrote_memo(fake_frames), "the write never ran"
    assert len(requests) == 1, "the run parked; nothing more was asked of the model"

    state = (await _step(task_id, step_id)).resume_state
    assert state is not None
    assert park_of(state).kind is ParkKind.PAUSE and park_of(state).paused_by == "Ada"


async def test_the_sweep_never_times_out_a_pause_gate(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """Read-only: the sweep acts on exactly `Gate.expired`, and a pause gate is never in it. (The
    sweep itself is never run here: it would time out every open gate in the shared database.)"""
    task_id, _, _, _, gate_id = await _parked_on_a_pause(real_agent, [])

    async with row_scope(Task, task_id) as db:
        due = [g.id for g in await Gate.expired(db, utcnow() + dt.timedelta(days=30))]

    assert uuid.UUID(gate_id) not in due
    assert (await _gate(task_id, gate_id)).state == GateState.OPEN.value


async def test_a_step_re_entered_while_parked_re_parks_on_the_same_gate(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """The recv's 24 h timeout (or a worker restart) re-runs the ladder while the pause stands: it
    re-parks on the open gate, and the model is asked nothing."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _, gate_id = await _parked_on_a_pause(real_agent, requests)

    assert await _run(task_id, step_id) == f"{execution.ASKING}{gate_id}"

    assert len(requests) == 1 and len(fake_frames.created) == 1
    assert (await _machine(task_id)).state == MachineState.PARKED
    assert len(await _gates_of(task_id)) == 1


async def test_a_pause_gate_is_not_answerable_through_the_gates_api(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    task_id, _, ada_id, _, gate_id = await _parked_on_a_pause(real_agent, [])

    async with row_scope(Task, task_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        with pytest.raises(Conflict, match="a pause lifts when the task resumes"):
            await gates.resolve(s, ada, uuid.UUID(gate_id), GateAction.ACCEPT)
    async with row_scope(Task, task_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        listed = [g.id for g, _ in await gates.open_gates(s, ada)]
    assert uuid.UUID(gate_id) not in listed, "the creator's gate queue never lists a pause"
    assert (await _gate(task_id, gate_id)).state == GateState.OPEN.value


# ── resume and cancel ──────────────────────────────────────────────────────────────────────────


async def test_resume_continues_the_same_run_and_the_call_is_re_issued(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, bob_id, gate_id = await _parked_on_a_pause(real_agent, requests)

    await _resume(bob_id, task_id)

    (wake,) = _step_wakes(recording_dispatcher, step_id)
    assert wake["gate_id"] == gate_id and wake["action"] == "accept"
    assert any(t == steering.STEER_TOPIC for _, _, t in recording_dispatcher.sends), "run_task is poked too"
    gate = await _gate(task_id, gate_id)
    assert gate.state == GateState.RESOLVED.value and gate.resolved_by == bob_id
    (resolved,) = await _events(task_id, GateResolved)
    assert (
        resolved.action is GateAction.ACCEPT and resolved.actor is not None and resolved.actor.name == "Bob"
    )
    assert (await _task(task_id)).state is TaskStatus.RUNNING

    await _wake(task_id, step_id, wake)
    assert (await _run(task_id, step_id)).strip() != ""

    assert _returns(requests[1])["w1"] == ADA_PAUSED_BOB_RESUMED, "the deferred call is told it did not run"
    assert len(requests[1]) > len(requests[0]), "the same run continued: its history came along"
    assert len(fake_frames.created) == 1, "the resume bound the parked Machine"
    assert _wrote_memo(fake_frames), "the re-issued write ran"
    details = [e.detail for e in await _events(task_id, StepStateChanged)]
    assert "resumed after a pause" in details
    done = await _step(task_id, step_id)
    assert done.step_state is StepState.DONE and done.resume_state is None
    machine = await _machine(task_id)
    assert machine.state == MachineState.IDLE and machine.held_by is None


class _WorkerDied(BaseException):
    """The worker process going down mid-run: nothing in the step handles it, nothing after the
    last commit is recorded."""


class _Journal:
    """DBOS's replay of the `run_step` workflow, in miniature. Each journaled call takes the next
    function id (a `recv` two: itself and its timeout's sleep); a recorded id hands its output
    back, and a call of another name at a recorded id is DBOS's own `durable.ReplayMismatch`. A
    worker death leaves the journal as it was; the next run of the body starts at id 1."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        self.ops: dict[int, tuple[str, Any]] = {}
        self.at = 0

    def start(self) -> None:
        self.at = 0

    async def call(self, name: str, live: Callable[[], Awaitable[Any]]) -> Any:
        self.at += 1
        fid = self.at
        if fid in self.ops:
            recorded, output = self.ops[fid]
            if recorded != name:
                raise durable.ReplayMismatch(self.workflow_id, fid, name, recorded)
            return output
        output = await live()
        self.ops[fid] = (name, output)
        return output

    def names(self) -> list[str]:
        return [self.ops[fid][0] for fid in sorted(self.ops)]


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch, recording_dispatcher: RecordingDispatcher) -> Callable[..., Any]:
    """The real `run_step` body over a `_Journal`: `mark_step_dequeued`, the recv (and its timeout's
    sleep), the paced sleep and the verification enqueue are its journaled calls. `on_recv` runs
    when a recv first waits (a person answers the gate), and the wake is the gate's own send."""

    def use(
        task_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        on_recv: Callable[[], Awaitable[None]],
        on_sleep: Callable[[float], Awaitable[None]],
    ) -> tuple[_Journal, Callable[[], Awaitable[str]]]:
        book = _Journal(f"step-{step_id}")

        async def mark(*args: Any) -> None:
            await book.call("mark_step_dequeued", _nothing)

        async def recv(topic: str, timeout_s: float) -> Any:
            async def wake() -> Any:
                book.ops[book.at + 1] = ("DBOS.sleep", None)  # the timeout's deadline, recorded first
                await on_recv()
                return _step_wakes(recording_dispatcher, step_id)[-1]

            got = await book.call("DBOS.recv", wake)
            book.at += 1
            return got

        async def sleep(seconds: float) -> None:
            if seconds <= 0:
                return  # DBOS journals no sleep of no time
            replayed = book.at + 1 in book.ops
            await book.call("DBOS.sleep", _nothing)  # DBOS records the deadline, then waits
            if not replayed:
                await on_sleep(seconds)

        recorded_enqueue = recording_dispatcher.enqueue

        async def enqueue(job: str, key: str, *, delay_s: float = 0.0, **kwargs: object) -> None:
            async def live() -> None:
                await recorded_enqueue(job, key, delay_s=delay_s, **kwargs)

            await book.call(job, live)

        monkeypatch.setattr(effects, "mark_step_dequeued", mark)
        monkeypatch.setattr(durable, "wait", recv)
        monkeypatch.setattr(durable, "sleep", sleep)
        monkeypatch.setattr(recording_dispatcher, "enqueue", enqueue)
        body = inspect.unwrap(effects.execute_step)  # the workflow's body, under DBOS's wrappers

        async def run() -> str:
            book.start()
            return cast(str, await body(str(task_id), str(step_id)))

        return book, run

    return use


async def _nothing() -> None:
    return None


async def test_a_worker_dying_in_the_resumed_run_replays_its_park_then_recovers(
    fake_frames: Any, real_agent: Callable[[Stream], None], journal: Callable[..., Any]
) -> None:
    """The worker died seconds into a resumed
    run. DBOS replays the step workflow by its journal - `mark_step_dequeued`, then the `recv` that
    brought the resume - so the replay's first ladder call must park again on the same gate (the
    recorded recv hands the wake straight back, and it is not stored again), and only the call
    after it runs the step: it re-enters the resumed run on its call log, the same answer replayed.
    It holds through a second death. Before: the first call rebuilt a resume from the gate with no
    history (`KeyError: 'history'`); running a fresh attempt there instead broke the journal
    ("DBOS.recv was recorded when verify_artifact was expected")."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, bob_id = await _running_step()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            await _pause(ada_id, task_id)
            yield _call(*WRITE, "w1")
        elif n in (2, 3):
            raise _WorkerDied  # the resumed run, then the recovered attempt: the worker goes down
        elif n == 4:
            yield _call(*WRITE, "w2")
        elif n == 5:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    book, run = journal(task_id, step_id, on_recv=lambda: _resume(bob_id, task_id), on_sleep=_no_sleep)
    for death in (1, 2):
        with pytest.raises(_WorkerDied):
            await run()
        assert book.names() == ["mark_step_dequeued", "DBOS.recv", "DBOS.sleep"], f"death {death}"
        assert len(requests) == 1 + death
    assert _returns(requests[2]) == _returns(requests[1]) != {}, "the recovered run replays the resume"

    assert (await run()).strip() != ""
    assert book.names() == ["mark_step_dequeued", "DBOS.recv", "DBOS.sleep", "verify_artifact"]
    assert len(requests) == 6 and _wrote_memo(fake_frames)
    done = await _step(task_id, step_id)
    assert done.step_state is StepState.DONE and done.resume_state is None
    assert done.model_attempts() == 1, "a worker death never spends the ladder"
    assert len(await _gates_of(task_id)) == 1, "no second park"


async def _no_sleep(seconds: float) -> None:
    raise AssertionError("nothing paces here")


@pytest.mark.parametrize("dies", ["in the pace", "in the rung after it"])
async def test_a_replay_re_issues_the_runs_hand_backs_in_the_order_it_took_them(
    dies: str,
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    journal: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that parked on a pause, was resumed, and could not get its Machine back at once, so it
    was paced, journals recv, then sleep. A worker that dies after both must replay
    them in that order - the park's gate first, its recorded wake handed back and not stored again,
    then the pace - and only then run: a fresh attempt on the infra rung. Before: the door read the
    row, re-paced first, and DBOS refused the replay ("DBOS.recv was recorded when DBOS.sleep was
    expected")."""
    monkeypatch.setattr(settings.machines, "wait_s", 1)
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, bob_id = await _running_step()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if len(requests) == 1:
            await _pause(ada_id, task_id)
            yield _call(*WRITE, "w1")
        elif len(requests) == 2 and dies == "in the rung after it":
            raise _WorkerDied
        elif not returns:
            yield _call(*WRITE, "w2")
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    real_acquire = machines.acquire
    acquires = 0

    async def acquire(*args: Any, **kwargs: Any) -> Any:
        nonlocal acquires
        acquires += 1
        if acquires == 2:  # the resume's: its Machine fails at once
            raise ModelIncapable("the Machine could not be prepared: boom", infra=True)
        return await real_acquire(*args, **kwargs)

    monkeypatch.setattr(machines, "acquire", acquire)
    sleeps: list[float] = []

    async def paced(seconds: float) -> None:
        sleeps.append(seconds)
        if dies == "in the pace":
            raise _WorkerDied
        await asyncio.sleep(seconds)

    book, run = journal(task_id, step_id, on_recv=lambda: _resume(bob_id, task_id), on_sleep=paced)
    with pytest.raises(_WorkerDied):
        await run()
    assert book.names() == ["mark_step_dequeued", "DBOS.recv", "DBOS.sleep", "DBOS.sleep"]
    assert len(requests) == (1 if dies == "in the pace" else 2)

    outcome = await run()  # the worker is back: DBOS replays the journal, then runs on

    assert outcome.strip() != "" and not outcome.startswith(("\x00",))
    assert book.names() == ["mark_step_dequeued", "DBOS.recv", "DBOS.sleep", "DBOS.sleep", "verify_artifact"]
    assert len(sleeps) == 1, "the replayed pace waited on its recorded deadline, never a second one"
    assert _wrote_memo(fake_frames)
    done = await _step(task_id, step_id)
    assert done.step_state is StepState.DONE and done.resume_state is None
    assert done.model_attempts() == 1, "neither the Machine's failure nor the death spends the ladder"
    assert len(await _gates_of(task_id)) == 1, "no second park"


async def test_a_replay_re_issues_a_budget_park_before_the_attempt_it_bought(
    journal: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Bursar's park before a further attempt is a hand-back like a question's or a
    pace's. Attempt 1 spends past the $1 cap, so the step parks on the budget gate before attempt
    2; Ada raises the cap and accepts, and the worker dies inside attempt 2. The replay must
    re-issue that park first - its recorded recv hands the accept back, not stored again - and only
    then run: a fresh attempt on the infra rung, under the raised cap. Unrecorded, the replay had
    no hand-back to re-issue: it hydrated the spent accept as an answer, resumed a run that has no
    history, and its verify_artifact enqueue met the recorded recv."""
    task_id, step_id, ada_id, _bob = await _running_step()
    await _set_cap(task_id, "1.00")
    calls: list[dict[str, object] | None] = []  # each attempt's resume

    async def attempt(
        session: Any,
        task: Any,
        step: Any,
        spec: Any,
        gotchas: Any,
        upstream: Any = (),
        resume: Any = None,
        **_: Any,
    ) -> tuple[str, list[object], None]:
        calls.append(resume)
        if len(calls) == 1:  # a run that hit its request budget, past the task's cap
            session.add(
                UsageEvent(
                    task_id=task.id,
                    step_id=step.id,
                    provider="anthropic",
                    model="claude-sonnet-5",
                    family="anthropic",
                    units={"request": 50},
                    cost_usd=Decimal("1.50"),
                    at=utcnow(),
                )
            )
            raise ModelIncapable("the run exceeded its request budget (request_limit of 50)")
        if len(calls) == 2:
            raise _WorkerDied  # the attempt the accept bought
        return "Updated.", [SimpleNamespace(id=uuid.uuid4(), role="deliverable")], None

    async def raise_the_cap_and_accept() -> None:
        await _set_cap(task_id, "5.00")
        (gate,) = [g for g in await _gates_of(task_id) if g.is_open()]
        await _as(ada_id, task_id, lambda s, ada, _: gates.resolve(s, ada, gate.id, GateAction.ACCEPT))

    monkeypatch.setattr(execution, "_attempt", attempt)
    book, run = journal(task_id, step_id, on_recv=raise_the_cap_and_accept, on_sleep=_no_sleep)
    with pytest.raises(_WorkerDied):
        await run()
    assert book.names() == ["mark_step_dequeued", "DBOS.recv", "DBOS.sleep"]
    assert len(calls) == 2

    outcome = await run()  # the worker is back: DBOS replays the journal, then runs on

    assert outcome == "Updated."
    assert book.names() == ["mark_step_dequeued", "DBOS.recv", "DBOS.sleep", "verify_artifact"]
    assert len(calls) == 3 and calls[2] is None, "the recovered run is a fresh attempt, not a resume"
    done = await _step(task_id, step_id)
    assert done.step_state is StepState.DONE and done.resume_state is None
    assert done.model_attempts() == 2, "attempt 1 and the one that published: the death nets out"
    (gate,) = await _gates_of(task_id)
    assert gate.kind == GateKind.BUDGET.value and gate.step_id == step_id, "one park, no second ask"


async def _set_cap(task_id: uuid.UUID, usd: str) -> None:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.budget = {"usd": usd, "tokens": None}
        await s.commit()


async def test_cancel_closes_a_pause_park(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    task_id, step_id, ada_id, _, gate_id = await _parked_on_a_pause(real_agent, [])

    await _as(ada_id, task_id, steering.cancel)

    (wake,) = _step_wakes(recording_dispatcher, step_id)
    assert wake["gate_id"] == gate_id and wake["action"] == "cancel"
    closed = (await _gate(task_id, gate_id)).resolution
    assert closed is not None and closed["action"] == "cancel"
    await _wake(task_id, step_id, wake)
    woken = (await _step(task_id, step_id)).resume_state
    assert woken is not None and woken["cancelled"] is True and woken["pause_resumed"] is True
    async with row_scope(Task, task_id) as session:  # the workflow's cancel exit: "(cancelled)"
        await execution.close_cancelled_park(session, task_id, step_id)

    machine = await _machine(task_id)
    assert machine.state == MachineState.IDLE and machine.held_by is None
    assert (await _task(task_id)).state is TaskStatus.CANCELLED
    assert not _wrote_memo(fake_frames)


async def test_a_resume_with_a_question_still_open_is_waiting(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """Ada pauses while the step waits on her answer; Bob's resume puts the task back to
    `waiting`, because the question is still open, and the answer moves it on to `running`."""
    task_id, step_id, ada_id, bob_id = await _running_step()
    question = {"question": "Which colour?", "options": ["blue", "red"]}
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            yield _call(ASK_USER, {"questions": [question]}, "ask-1")
        elif n == 2:
            yield _call(*WRITE, "w1")
        elif n == 3:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    asked = (await _run(task_id, step_id)).removeprefix(execution.ASKING)
    assert (await _task(task_id)).state is TaskStatus.WAITING

    await _pause(ada_id, task_id)
    await _resume(bob_id, task_id)

    assert (await _task(task_id)).state is TaskStatus.WAITING, "still waiting on the answer"
    statuses = [e.status for e in await _events(task_id, TaskStatusChanged)]
    assert statuses[-3:] == [TaskStatus.PAUSED, TaskStatus.RUNNING, TaskStatus.WAITING]
    assert _step_wakes(recording_dispatcher, step_id) == [], "no pause gate to lift"

    async with row_scope(Task, task_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        await gates.resolve(s, ada, uuid.UUID(asked), GateAction.ACCEPT, {"q1": "blue"})
    (answer,) = _step_wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, answer)
    await _run(task_id, step_id)

    assert (await _task(task_id)).state is TaskStatus.RUNNING
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
    assert [g.kind for g in await _gates_of(task_id)] == [GateKind.INPUT.value]


async def test_a_pause_lifted_before_the_park_continues_in_place(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob resumes after the call deferred but before the gate opened: the resume found no gate to
    lift, so the step must not park on one nobody will ever resolve."""
    task_id, step_id, ada_id, bob_id = await _running_step()
    noted = StepInbox.paused_here

    async def paused_then_resumed(self: StepInbox, point: SafePoint) -> None:
        await noted(self, point)
        await _resume(bob_id, task_id)

    monkeypatch.setattr(StepInbox, "paused_here", paused_then_resumed)
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            await _pause(ada_id, task_id)
            yield _call(*WRITE, "w1")
        elif n == 2:
            yield _call(*WRITE, "w2")
        elif n == 3:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    outcome = await _run(task_id, step_id)

    assert not outcome.startswith(execution.ASKING)
    assert _returns(requests[1])["w1"] == ADA_PAUSED_BOB_RESUMED
    assert await _gates_of(task_id) == [], "no gate opened for a pause already over"
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
    assert _wrote_memo(fake_frames)
    assert len(fake_frames.created) == 1, "the run went on in the Machine it parked in"
    machine = await _machine(task_id)
    assert machine.state == MachineState.IDLE and machine.held_by is None


# ── a pause beside the other parks ─────────────────────────────────────────────────────────────


async def test_a_question_and_a_pause_in_one_response(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, real_agent: Callable[[Stream], None]
) -> None:
    """One response asks AND chooses a write while the task is paused. The question
    parks (the task stays paused), the answer resumes the run and tells the write it did not run;
    still paused, the re-issued write parks on the pause; the resume finishes the run."""
    task_id, step_id, ada_id, bob_id = await _running_step()
    requests: list[list[ModelMessage]] = []
    question = {"question": "Which colour?", "options": ["blue", "red"]}

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            await _pause(ada_id, task_id)
            yield _calls((ASK_USER, {"questions": [question]}, "ask-1"), (*WRITE, "w1"))
        elif n == 2:
            yield _call(*WRITE, "w2")  # re-issued while still paused
        elif n == 3:
            yield _call(*WRITE, "w3")  # re-issued after the resume
        elif n == 4:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    asked = (await _run(task_id, step_id)).removeprefix(execution.ASKING)
    ask_gate = await _gate(task_id, asked)
    assert ask_gate.kind == GateKind.INPUT.value
    assert (await _task(task_id)).state is TaskStatus.PAUSED, "a paused task stays paused at a gate"
    parked = (await _step(task_id, step_id)).resume_state
    assert parked is not None and park_of(parked).paused_by == "Ada"

    async with row_scope(Task, task_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        await gates.resolve(s, ada, ask_gate.id, GateAction.ACCEPT, {"q1": "blue"})
    (answer,) = _step_wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, answer)
    paused = (await _run(task_id, step_id)).removeprefix(execution.ASKING)

    back = _returns(requests[1])
    assert back["ask-1"] == "blue" and back["w1"] == ADA_PAUSED
    pause_gate = await _gate(task_id, paused)
    assert pause_gate.kind == GateKind.PAUSE.value, "still paused: the re-issued write parks"
    assert (await _task(task_id)).state is TaskStatus.PAUSED
    assert not _wrote_memo(fake_frames)

    await _resume(bob_id, task_id)
    wake = _step_wakes(recording_dispatcher, step_id)[-1]
    assert wake["gate_id"] == paused
    await _wake(task_id, step_id, wake)
    await _run(task_id, step_id)

    assert _returns(requests[2])["w2"] == ADA_PAUSED_BOB_RESUMED
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
    assert _wrote_memo(fake_frames)
    assert len(fake_frames.created) == 1
    (fact,) = await _events(task_id, PauseApplied)  # two runs met the one pause: one fact
    assert fact.safe_point is SafePoint.BEFORE_TOOL and fact.step_id == step_id


async def test_an_over_budget_question_beside_a_pause_answers_both_in_place(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """The ask budget is spent: the question answers itself in place, and the paused sibling is
    told it did not run - then, still paused, the re-issued write parks on the pause."""
    task_id, step_id, ada_id, _ = await _running_step()
    async with row_scope(Task, task_id) as s:
        for _ in range(execution.MAX_STEP_QUESTION_GATES):
            s.add(
                Gate(task_id=task_id, step_id=step_id, kind=GateKind.INPUT.value, state="resolved", spec={})
            )
        await s.commit()
    requests: list[list[ModelMessage]] = []
    question = {"question": "Which colour?", "options": ["blue", "red"]}

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            await _pause(ada_id, task_id)
            yield _calls((ASK_USER, {"questions": [question]}, "ask-1"), (*WRITE, "w1"))
        else:
            yield _call(*WRITE, "w2")

    real_agent(stream)
    paused = (await _run(task_id, step_id)).removeprefix(execution.ASKING)

    back = _returns(requests[1])
    assert back["ask-1"] == execution.OUT_OF_QUESTIONS and back["w1"] == ADA_PAUSED
    assert (await _gate(task_id, paused)).kind == GateKind.PAUSE.value
    assert not _wrote_memo(fake_frames)


async def test_a_steer_held_before_a_pause_park_is_told_once_after_the_resume(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steer applied at the first call (held, so the step's epoch moved) before the
    second call parked on a pause - the park's `told` is below the step's epoch, and the resumed
    run's first request tells the steer exactly once."""
    task_id, step_id, ada_id, bob_id = await _running_step()
    applied = StepInbox.applied
    pauses: list[bool] = []

    async def applied_then_paused(self: StepInbox, steers: Any, point: SafePoint) -> None:
        await applied(self, steers, point)
        if point is SafePoint.BEFORE_TOOL and not pauses:
            pauses.append(True)
            await _pause(ada_id, task_id)  # between the batch's two calls

    monkeypatch.setattr(StepInbox, "applied", applied_then_paused)
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            async with row_scope(Task, task_id) as s:
                bob = await s.get(Actor, bob_id)
                assert bob is not None
                await steering.steer(s, bob, task_id, "write it in euros")
            yield _calls(("write_file", {"path": "/work/a.md", "content": "$"}, "w1"), (*WRITE, "w2"))
        elif n == 2:
            yield _call(*WRITE, "w3")
        elif n == 3:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    with ToolManager.parallel_execution_mode("sequential"):  # the pause lands between the two calls
        paused = (await _run(task_id, step_id)).removeprefix(execution.ASKING)

    parked = await _step(task_id, step_id)
    assert parked.resume_state is not None
    assert park_of(parked.resume_state).told_epoch < parked.epoch, "applied at the held call, never told"
    assert (await _gate(task_id, paused)).kind == GateKind.PAUSE.value

    await _resume(bob_id, task_id)
    await _wake(task_id, step_id, _step_wakes(recording_dispatcher, step_id)[-1])
    await _run(task_id, step_id)

    resumed = _told(requests[1])
    assert resumed.count("**Bob steered** while you worked: write it in euros") == 1
    assert _returns(requests[1])["w2"] == ADA_PAUSED_BOB_RESUMED
    assert "write it in euros" not in _told(requests[0])
    assert all("/work/a.md" not in f.files for f in fake_frames.world.values()), "the held write never ran"
    assert (await _step(task_id, step_id)).step_state is StepState.DONE


async def test_a_pause_fact_that_failed_to_record_is_written_by_the_next_call(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inbox marks the pause noted only after its write commits."""
    task_id, step_id, ada_id, _ = await _running_step()
    await _pause(ada_id, task_id)
    real = steering.pause_applied
    failures = [RuntimeError("the database blinked")]

    async def flaky(*args: Any, **kwargs: Any) -> None:
        if failures:
            raise failures.pop()
        await real(*args, **kwargs)

    monkeypatch.setattr(steering, "pause_applied", flaky)
    inbox = StepInbox(task_id, step_id, 1, told=1)
    with pytest.raises(RuntimeError):
        await inbox.paused_here(SafePoint.BEFORE_TOOL)
    await inbox.paused_here(SafePoint.BEFORE_TOOL)
    await inbox.paused_here(SafePoint.BEFORE_TOOL)

    (fact,) = await _events(task_id, PauseApplied)
    assert fact.step_id == step_id


# ── a step that cannot park ────────────────────────────────────────────────────────────────────


async def test_past_the_parked_pause_cap_a_paused_step_finishes_and_the_frontier_holds(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R54: every pause park holds a queue slot, so only `max_pause_parks` may be open across the
    deployment. At the cap a paused step does not park: it finishes, and the frontier holds the
    pause (`pause.applied{boundary}`)."""
    other_task, other_step, _, _ = await _running_step()
    async with row_scope(Task, other_task) as s:  # someone else's step already parked on a pause
        other = await s.get(Task, other_task)
        assert other is not None
        await gates.open_pause(s, other, StepId(other_step))
        await s.commit()
    async with session_scope(platform=True) as s:
        parked = await Gate.open_pause_count(s)
    assert parked >= 1
    monkeypatch.setattr(settings.steering, "max_pause_parks", parked)  # the cap is reached

    task_id, step_id, ada_id, _ = await _running_step()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            await _pause(ada_id, task_id)  # Ada pauses while the model chooses
            yield _call(*WRITE, "w1")
        elif n == 2:
            yield _call(*PUBLISH, "p1")
        else:
            yield "done"

    real_agent(stream)
    outcome = await _run(task_id, step_id)

    assert not outcome.startswith(execution.ASKING), "past the cap the step never parks"
    assert _wrote_memo(fake_frames), "the write ran"
    assert await _gates_of(task_id) == []
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
    assert (await _task(task_id)).state is TaskStatus.PAUSED
    assert await _events(task_id, PauseApplied) == [], "nothing took the pause inside the step"
    assert await effects.pause_held(str(task_id)) is True, "the frontier holds"
    (fact,) = await _events(task_id, PauseApplied)
    assert fact.safe_point is SafePoint.BOUNDARY and fact.step_id is None


async def test_review_focus_an_express_turn_pauses_at_the_frontier(
    silent_dispatcher: None, real_agent: Callable[[Stream], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_scope() as s:
        asker, _ = await member(s, "Asker")
        await s.flush()
        task = await task_ops.create_task(s, asker, goal="What's the time?", clarify="never")
        task.transition(TaskStatus.PLANNING)
        await s.flush()
        view = await planning.draft_plan(s, task.id)
        await s.commit()
        task_id, asker_id = task.id, asker.id
    assert view.steps[0].lane_role == "express"
    step_id = uuid.UUID(str(view.steps[0].id))
    async with row_scope(Task, task_id) as s:
        await execution.advance(s, task_id, TaskStatus.RUNNING)
        await s.commit()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        await _pause(asker_id, task_id)  # lands while the answer is written
        yield "It is 14:02 UTC."

    real_agent(stream)
    outcome = await _run(task_id, step_id)

    assert outcome == "It is 14:02 UTC.", "an express step cannot park: its run completes"
    assert await _gates_of(task_id) == []
    assert (await _task(task_id)).state is TaskStatus.PAUSED
    assert await effects.pause_held(str(task_id)) is True, "the frontier holds"
    (fact,) = await _events(task_id, PauseApplied)
    assert fact.safe_point is SafePoint.BOUNDARY and fact.step_id is None


# ── a pause and the status writers: the row as the lock sees it ───────────────────────────────


async def _steer(actor_id: uuid.UUID, task_id: uuid.UUID, text: str) -> None:
    await _as(actor_id, task_id, lambda s, actor, tid: steering.steer(s, actor, tid, text))


async def _finished_step(task_id: uuid.UUID, step_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        step.transition(StepState.RUNNING)
        step.transition(StepState.DONE)
        await s.commit()


async def _until_someone_waits_on_a_row_lock() -> None:
    """Until a backend of this database is queued on a lock it has not been granted."""
    waiting = text(
        "select count(*) from pg_locks l join pg_stat_activity a on a.pid = l.pid "
        "where not l.granted and a.datname = current_database()"
    )
    for _ in range(200):
        async with session_scope(platform=True) as s:
            if await s.scalar(waiting):
                return
        await asyncio.sleep(0.025)
    raise AssertionError("nobody queued on the task's row lock")


async def test_a_step_that_fails_while_paused_fails_the_task_and_rejects_its_steers(
    silent_dispatcher: None,
) -> None:
    """A step that cannot park runs on through a pause, and its failure is a fact: the
    task fails and every steer that never applied says so - nothing wedges `paused`."""
    task_id, _, ada_id, bob_id = await _running_step()
    await _steer(bob_id, task_id, "use euros")
    await _pause(ada_id, task_id)

    async with row_scope(Task, task_id) as s:
        await execution.fail(s, task_id, "PolicyDenied: the section writer gave up", turn=1)
        await s.commit()

    assert (await _task(task_id)).state is TaskStatus.FAILED
    async with row_scope(Task, task_id) as s:
        (steer,) = await Steer.since(s, task_id, 0)
        assert (steer.state, steer.reason) == ("rejected", REJECT_FAILED)


async def test_a_budget_gate_opened_while_paused_keeps_the_pause(silent_dispatcher: None) -> None:
    """The Bursar parks on its gate; the task waits on its resume first - it stays `paused`."""
    task_id, _, ada_id, _ = await _running_step()
    await _pause(ada_id, task_id)

    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        await gates._open_budget_gate(s, task, title="Budget reached", reason="spent")  # pyright: ignore[reportPrivateUsage]

    assert (await _task(task_id)).state is TaskStatus.PAUSED
    (gate,) = await _gates_of(task_id)
    assert (gate.kind, gate.state) == (GateKind.BUDGET.value, GateState.OPEN.value)


async def test_a_park_through_a_copy_read_before_the_pause_leaves_the_task_paused(
    silent_dispatcher: None,
) -> None:
    """A step loaded its task running; the pause lands mid-run; the step then parks on a gate.
    `waiting` must not overwrite `paused` through the old copy - the decision reads the row under
    its lock (any park: a question, a connect block, an approval, a control lease)."""
    task_id, _, ada_id, _ = await _running_step()
    async with row_scope(Task, task_id) as s:
        before = await s.get(Task, task_id)  # the step's copy, read as it began
        assert before is not None and before.state is TaskStatus.RUNNING
        await s.commit()
        await _pause(ada_id, task_id)

        assert await execution.advance(s, task_id, TaskStatus.WAITING) == TaskStatus.PAUSED.value
        await s.commit()

    assert (await _task(task_id)).state is TaskStatus.PAUSED
    moves = [e.status for e in await _events(task_id, TaskStatusChanged)]
    assert moves[-1] is TaskStatus.PAUSED and TaskStatus.WAITING not in moves


@pytest.mark.parametrize("door", ["pause", "cancel"])
async def test_a_delivery_through_a_copy_read_before_a_pause_or_cancel_never_overwrites_it(
    silent_dispatcher: None, door: str
) -> None:
    """The delivery's task copy predates a pause (or a cancel) that committed before its lock: it
    decides on the row under the lock - a paused turn holds (the workflow's hold takes it), a
    cancelled one is skipped - and never writes `delivered` over either."""
    task_id, step_id, ada_id, _ = await _running_step()
    await _finished_step(task_id, step_id)
    want = TaskStatus.PAUSED if door == "pause" else TaskStatus.CANCELLED
    async with row_scope(Task, task_id) as s:
        copy = await s.get(Task, task_id)  # the workflow's copy, read before the lock
        assert copy is not None and copy.state is TaskStatus.RUNNING
        await s.commit()
        await _as(ada_id, task_id, steering.pause if door == "pause" else steering.cancel)

        delivery = await execution.deliver(s, task_id, "the memo", turn=1)
        await s.commit()
        assert copy.state is want, "the session's own copy reads the row as the lock saw it"

    assert delivery.verdict == ("held" if door == "pause" else "skipped")
    assert (await _task(task_id)).state is want
    assert await _events(task_id, TaskDelivered) == []


async def test_a_pause_racing_a_delivery_never_turns_a_delivered_task_paused(silent_dispatcher: None) -> None:
    """Ada presses Pause as the turn delivers: the delivery holds the row lock, her pause queues on
    it, and once the delivery commits the pause reads `delivered` - refused, the task settled."""
    task_id, step_id, ada_id, _ = await _running_step()
    await _finished_step(task_id, step_id)

    async with row_scope(Task, task_id) as s:
        assert (await execution.deliver(s, task_id, "the memo", turn=1)).verdict == "delivered"
        pausing = asyncio.create_task(_pause(ada_id, task_id))
        await _until_someone_waits_on_a_row_lock()
        await s.commit()

    with pytest.raises(ValidationFailed, match="already delivered"):
        await pausing
    assert (await _task(task_id)).state is TaskStatus.DELIVERED


async def test_a_pause_fact_is_never_written_after_the_resume(silent_dispatcher: None) -> None:
    """A run read the pause, then the resume committed before it said so: "Paused by Ada" would
    sit on the record of a running task. The fact is written only while the task is paused."""
    task_id, step_id, ada_id, _ = await _running_step()
    await _pause(ada_id, task_id)
    await _resume(ada_id, task_id)

    await StepInbox(task_id, step_id, 1, told=1).paused_here(SafePoint.BEFORE_TOOL)

    assert await _events(task_id, PauseApplied) == []
