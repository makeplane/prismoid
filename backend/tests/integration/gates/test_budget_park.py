# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The budget brake inside a run: a computer step that reaches its task's
cap mid-run parks on the BUDGET gate with its conversation saved; raising the cap and accepting
resumes the SAME run; keeping the cap ends the step partial with no further model call.

The real `execute_step` against real rows, FakeFrames, the real `build()` (so the real `RunGuard`
and the real spend closure) with a scripted model; spend lands as ledger rows mid-stream."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls
from sqlalchemy import select

from prismoid.ai.tools.ask import ASK_USER
from prismoid.ai.tools.browser import PAGE_RESET
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import StepFailed
from prismoid.operations import execution, gates, machines, steering
from prismoid.operations import usage as usage_ops
from prismoid.operations.execution import giveback as ex_giveback
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.guard import BUDGET_ASK_AGAIN, BUDGET_CONTINUE, BUDGET_RESUMED
from prismoid.schemas.machines import MachineState, SleepReason
from prismoid.schemas.park import ParkKind, park_of
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, StepState, TaskStatus
from tests.support.dispatchers import RecordingDispatcher
from tests.support.rigs.budget_park_rig import SITE, Stream
from tests.support.rigs.budget_park_rig import computer_task as _capped_task
from tests.support.rigs.budget_park_rig import computer_use_on as computer_use_on
from tests.support.rigs.budget_park_rig import real_agent as real_agent
from tests.support.rigs.budget_park_rig import returns as _returns
from tests.support.rigs.budget_park_rig import rows as _rows
from tests.support.rigs.budget_park_rig import run_step as _run
from tests.support.rigs.budget_park_rig import sidecar as sidecar
from tests.support.rigs.budget_park_rig import spend as _spend
from tests.support.rigs.budget_park_rig import tool as _tool
from tests.support.rigs.budget_park_rig import wake as _wake
from tests.support.rigs.budget_park_rig import wakes as _wakes

pytestmark = pytest.mark.integration


async def _answer(
    actor_id: uuid.UUID, task_id: uuid.UUID, gate_id: str, action: GateAction, *, usd: str | None = None
) -> None:
    async with row_scope(Task, task_id) as s:
        ada = await s.get(Actor, actor_id)
        assert ada is not None
        if usd is not None:
            await steering.set_budget(s, ada, task_id, Decimal(usd))
        await gates.resolve(s, ada, uuid.UUID(gate_id), action)


async def _parked(
    real_agent: Callable[[Stream], None], requests: list[list[ModelMessage]]
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """The second request spends $0.60 against a $0.50 cap; its own tool call parks at dispatch (the
    spend is read again there, so the call after the cap is crossed never runs)."""
    task_id, step_id, ada_id = await _capped_task("0.50")

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            yield _tool("browser_snapshot", "s1")
        elif n == 2:
            await _spend(task_id, step_id, "0.60")  # this request's bill lands past the cap
            yield _tool("browser_snapshot", "s2")  # read at its dispatch: this call parks the step
        else:
            yield _tool(
                "done",
                f"d{n}",
                status="done",
                summary="Summarized 30 stories.",
                proof={"url": SITE, "page_text": "Save"},
            )

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert said.startswith(execution.ASKING)
    return task_id, step_id, ada_id, said.removeprefix(execution.ASKING)


async def test_reaching_the_cap_mid_run_parks_on_the_budget_gate_with_the_conversation_saved(
    fake_frames: Any, sidecar: None, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, gate_id = await _parked(real_agent, requests)

    gate: Gate = await _rows(task_id, lambda s: s.get(Gate, uuid.UUID(gate_id)))
    spec = GateSpec.model_validate(gate.spec)
    assert gate.kind == GateKind.BUDGET.value and gate.step_id == step_id and gate.expires_at is None
    assert spec.title == "Budget reached" and spec.budget_figures is not None
    # To the cent: the ledger also carries the Machine's warm time (`Unit.SECOND`, $0.000012 a
    # second), and a step that runs past a whole second before it parks bills one - timing, not
    # the request this test is about.
    assert spec.budget_figures.spent_usd.quantize(Decimal("0.01")) == Decimal("0.60")
    assert spec.budget_figures.ceiling_usd == Decimal("0.50")
    task: Task = await _rows(task_id, lambda s: s.get(Task, task_id))
    assert task.state is TaskStatus.WAITING
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.resume_state is not None and park_of(step.resume_state).kind is ParkKind.BUDGET
    machine: Machine = await _rows(task_id, lambda s: Machine.for_task(s, task_id))
    assert machine.state == MachineState.PARKED
    assert len(requests) == 2, "nothing more was bought"


async def test_raising_the_cap_and_accepting_resumes_the_same_run(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, requests)

    await _answer(ada_id, task_id, gate_id, GateAction.ACCEPT, usd="2.00")
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    assert await _run(task_id, step_id) == "Summarized 30 stories."

    assert _returns(requests[2])["s2"] == BUDGET_RESUMED, "the parked call is told it never ran"
    assert len(requests[2]) > len(requests[1]), "the same conversation continued"
    assert len(fake_frames.created) == 1, "the resume bound the parked Machine"
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.DONE and step.resume_state is None


async def test_keeping_the_cap_ends_the_step_partial_with_no_more_spend(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, requests)

    await _answer(ada_id, task_id, gate_id, GateAction.DECLINE)
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    said = await _run(task_id, step_id)

    assert said.startswith("**Partly done.** Stopped at the task's budget")
    assert len(requests) == 2, "a kept cap buys no model call"
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.DONE
    events = await _rows(task_id, lambda s: TaskEvent.replay(s, task_id, since=0))
    done = [e for e in events if isinstance(e, StepStateChanged) and e.state is StepState.DONE]
    assert [e.outcome for e in done] == ["partial"]
    task: Task = await _rows(task_id, lambda s: s.get(Task, task_id))
    assert task.state is TaskStatus.RUNNING, "the step stops; the task is not cancelled"
    machine: Machine = await _rows(task_id, lambda s: Machine.for_task(s, task_id))
    assert machine.state == MachineState.IDLE and machine.held_by is None


# ── the park's other doors (review focus): cancel, the clock, a lost wake, the model's own stop ──


def _told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


async def test_cancelling_the_budget_gate_closes_the_park_and_ends_the_task(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, requests)

    await _answer(ada_id, task_id, gate_id, GateAction.CANCEL)
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    woken: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert woken.resume_state is not None
    assert woken.resume_state["budget_answer"] == "cancel" and woken.resume_state["cancelled"] is True
    async with row_scope(Task, task_id) as s:  # the workflow's cancel exit: "(cancelled)"
        await execution.close_cancelled_park(s, task_id, step_id)

    task: Task = await _rows(task_id, lambda s: s.get(Task, task_id))
    assert task.state is TaskStatus.CANCELLED, "a cancelled budget park ends the task, as between attempts"
    machine: Machine = await _rows(task_id, lambda s: Machine.for_task(s, task_id))
    assert machine.state == MachineState.IDLE and machine.held_by is None
    assert len(requests) == 2


async def test_the_sweep_never_times_out_a_budget_park(
    fake_frames: Any, sidecar: None, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """Read-only (the sweep itself would time out every open gate in the shared database): a budget
    gate has no clock, so nothing ever declines it - or accepts it - into spend."""
    task_id, _, _, gate_id = await _parked(real_agent, [])

    due: list[Gate] = await _rows(task_id, lambda s: Gate.expired(s, utcnow() + dt.timedelta(days=30)))
    assert uuid.UUID(gate_id) not in [g.id for g in due]


async def test_a_lost_wake_is_read_from_the_gate_and_resumes_the_same_run(
    fake_frames: Any, sidecar: None, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """The worker died while parked and the wake was never stored: the gate row is the truth, and
    the door hydrates the budget channel from it - the same run, not a fresh attempt."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, requests)

    await _answer(ada_id, task_id, gate_id, GateAction.ACCEPT, usd="2.00")
    assert await _run(task_id, step_id) == "Summarized 30 stories."

    assert _returns(requests[2])["s2"] == BUDGET_RESUMED
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.DONE and step.attempt == 1, "the same attempt, resumed"


async def test_a_lost_cancel_wake_ends_the_task_with_no_model_call(
    fake_frames: Any, sidecar: None, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, requests)

    await _answer(ada_id, task_id, gate_id, GateAction.CANCEL)
    said = await _run(task_id, step_id)

    assert "budget" in said and len(requests) == 2
    task: Task = await _rows(task_id, lambda s: s.get(Task, task_id))
    assert task.state is TaskStatus.CANCELLED
    machine: Machine = await _rows(task_id, lambda s: Machine.for_task(s, task_id))
    assert machine.state == MachineState.IDLE and machine.held_by is None


async def test_a_cap_raised_while_the_run_parks_continues_in_place(
    fake_frames: Any,
    sidecar: None,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard armed, then the owner raised the cap before the gate opened: the Bursar no longer
    sees the task over, so no gate opens and the held call is told it never ran - now."""
    task_id, step_id, _ = await _capped_task("0.50")
    requests: list[list[ModelMessage]] = []
    reader = usage_ops.spend_reader

    def raised_after_arming(tid: uuid.UUID) -> Any:
        read = reader(tid)

        async def spend() -> Any:
            figures = await read()
            if figures is not None and figures.spent_usd >= figures.ceiling_usd:  # the guard arms here
                async with row_scope(Task, task_id) as s:  # and the owner raises before the gate opens
                    task = await s.get(Task, task_id)
                    assert task is not None
                    task.budget = {"usd": "5.00", "tokens": None}
                    await s.commit()
            return figures

        return spend

    monkeypatch.setattr(usage_ops, "spend_reader", raised_after_arming)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            await _spend(task_id, step_id, "0.60")
            yield _tool("browser_snapshot", "s1")  # read at its dispatch: $0.60 of $0.50 arms the guard
        else:
            yield _tool(
                "done",
                f"d{n}",
                status="done",
                summary="Summarized 30 stories.",
                proof={"url": SITE, "page_text": "Save"},
            )

    real_agent(stream)
    assert await _run(task_id, step_id) == "Summarized 30 stories."

    assert _returns(requests[1])["s1"] == BUDGET_RESUMED
    gates_of: list[Gate] = await _rows(task_id, lambda s: Gate.for_task(s, task_id))
    assert gates_of == [], "nothing stops the spend any more: no gate"


async def test_the_model_stopping_at_the_cap_parks_and_a_raise_continues_the_conversation(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """An Outcome `blocked_by="budget"` is the same park with no call to answer (never a
    retry): accept continues the SAME conversation with one more line."""
    task_id, step_id, ada_id = await _capped_task("0.50")
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            await _spend(task_id, step_id, "0.60")
            yield _tool("done", "b1", status="blocked", blocked_by="budget", summary="The budget is spent.")
        else:
            yield _tool(
                "done",
                "d2",
                status="done",
                summary="Summarized 30 stories.",
                proof={"url": SITE, "page_text": "Save"},
            )

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert said.startswith(execution.ASKING)
    gate: Gate = await _rows(task_id, lambda s: s.get(Gate, uuid.UUID(said.removeprefix(execution.ASKING))))
    assert gate.kind == GateKind.BUDGET.value and gate.step_id == step_id
    parked: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert parked.resume_state is not None and park_of(parked.resume_state).kind is ParkKind.BUDGET

    await _answer(ada_id, task_id, str(gate.id), GateAction.ACCEPT, usd="2.00")
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    assert await _run(task_id, step_id) == "Summarized 30 stories."

    assert len(requests) == 2 and len(requests[1]) > len(requests[0]), "the same conversation"
    assert BUDGET_CONTINUE in _told(requests[1])
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.DONE and step.attempt == 1


async def test_a_budget_stop_under_the_cap_is_the_blocked_arm_never_a_loop(
    fake_frames: Any, sidecar: None, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """The model says the budget stops it, but the Bursar sees nothing over: there is no gate to
    park on, and no raise to tell it about - the step fails blocked with its summary, once, as C3's
    blocked arm does. (Continuing in place would let the model say it again, forever.)"""
    task_id, step_id, _ = await _capped_task("5.00")
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool("done", "b1", status="blocked", blocked_by="budget", summary="The budget is spent.")

    real_agent(stream)
    with pytest.raises(StepFailed, match="budget reached"):
        await _run(task_id, step_id)

    assert len(requests) == 1
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.ESCALATED and step.resume_state is None
    gates_of: list[Gate] = await _rows(task_id, lambda s: Gate.for_task(s, task_id))
    assert gates_of == []
    machine: Machine = await _rows(task_id, lambda s: Machine.for_task(s, task_id))
    assert machine.state == MachineState.IDLE and machine.held_by is None


@pytest.fixture
def give_back_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_discard_parked_frame`'s commit-failure path: the session rolled back (every row it holds
    expired), and the park given back on ids alone."""

    async def rolled_back(session: Any, task: Task, step: PlanStep, *, profile: str = "base") -> None:
        ids = (task.id, step.id, step.attempt)
        await session.execute(select(1))  # the transaction the failed commit had open
        await session.rollback()
        await ex_giveback.give_park_back(*ids, profile=profile)

    monkeypatch.setattr(ex_giveback, "discard_parked_frame", rolled_back)


async def test_a_cancel_whose_give_back_rolled_back_still_ends_the_task(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    give_back_rolls_back: None,
) -> None:
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, [])

    await _answer(ada_id, task_id, gate_id, GateAction.CANCEL)
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    async with row_scope(Task, task_id) as s:
        await execution.close_cancelled_park(s, task_id, step_id)

    task: Task = await _rows(task_id, lambda s: s.get(Task, task_id))
    assert task.state is TaskStatus.CANCELLED, "the expired task is read again, never silently skipped"


async def test_a_decline_whose_give_back_rolled_back_still_settles_partial(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    give_back_rolls_back: None,
) -> None:
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, gate_id = await _parked(real_agent, requests)

    await _answer(ada_id, task_id, gate_id, GateAction.DECLINE)
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    said = await _run(task_id, step_id)

    assert said.startswith("**Partly done.**") and len(requests) == 2
    task: Task = await _rows(task_id, lambda s: s.get(Task, task_id))
    assert task.state is TaskStatus.RUNNING
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.DONE


def _turn(*calls: tuple[str, str, dict[str, Any]]) -> DeltaToolCalls:
    """Several tool calls in ONE model turn: (tool, call id, args) each."""
    return {
        i: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id=call_id)
        for i, (tool, call_id, args) in enumerate(calls)
    }


async def test_a_question_that_shared_the_held_turn_is_told_to_ask_again(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """A question parks as itself (the guard never holds it), so it is not one of the calls the
    budget held: on resume it is told it never reached a person and to ask again - not that it was
    "not run" for the budget."""
    task_id, step_id, ada_id = await _capped_task("0.50")
    requests: list[list[ModelMessage]] = []
    question = {"question": "Which week?", "options": ["this", "last"]}

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n == 1:
            yield _tool("browser_snapshot", "s1")
        elif n == 2:
            await _spend(task_id, step_id, "0.60")  # read at the next dispatch: the snapshot is held
            yield _turn(("browser_snapshot", "s2", {}), (ASK_USER, "q2", {"questions": [question]}))
        else:
            yield _tool(
                "done",
                f"d{n}",
                status="done",
                summary="Summarized 30 stories.",
                proof={"url": SITE, "page_text": "Save"},
            )

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert said.startswith(execution.ASKING)
    parked: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert parked.resume_state is not None
    assert park_of(parked.resume_state).kind is ParkKind.BUDGET

    await _answer(ada_id, task_id, said.removeprefix(execution.ASKING), GateAction.ACCEPT, usd="2.00")
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    assert await _run(task_id, step_id) == "Summarized 30 stories."

    returns = _returns(requests[2])
    assert returns["s2"] == BUDGET_RESUMED and returns["q2"] == BUDGET_ASK_AGAIN


async def test_a_budget_stop_resumed_on_a_slept_machine_is_told_the_page_was_reset(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """No call to answer (the model stopped at the cap itself): the one line the run continues with
    still says the page is gone when the Machine slept while parked."""
    task_id, step_id, ada_id = await _capped_task("0.50")
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            await _spend(task_id, step_id, "0.60")
            yield _tool("done", "b1", status="blocked", blocked_by="budget", summary="The budget is spent.")
        else:
            yield _tool(
                "done",
                "d2",
                status="done",
                summary="Summarized 30 stories.",
                proof={"url": SITE, "page_text": "Save"},
            )

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert said.startswith(execution.ASKING)
    parked: Machine = await _rows(task_id, lambda s: Machine.for_task(s, task_id))
    assert await machines.sleep(parked.id, parked.epoch, SleepReason.PARK_HOLD) == "slept"

    await _answer(ada_id, task_id, said.removeprefix(execution.ASKING), GateAction.ACCEPT, usd="2.00")
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    assert await _run(task_id, step_id) == "Summarized 30 stories."

    assert f"{PAGE_RESET}\n\n{BUDGET_CONTINUE}" in _told(requests[1])


# ── a partial ending at the cap is a budget stop ──────────────────────


def _short_at(task_id: uuid.UUID, step_id: uuid.UUID, requests: list[list[ModelMessage]], usd: str) -> Stream:
    """The first request spends `usd` and ends `partial` blaming the budget; any later one is done."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            await _spend(task_id, step_id, usd)
            yield _tool("done", "p1", status="partial", summary="Read 2 of 15 stories; the budget ran out.")
        else:
            yield _tool(
                "done",
                "d2",
                status="done",
                summary="Summarized 30 stories.",
                proof={"url": SITE, "page_text": "Save"},
            )

    return stream


async def test_a_partial_at_the_cap_parks_and_a_raise_continues_the_conversation(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """The computer's last request pushed the spend past the cap and it ended `partial` - so the
    gate opened after the step had quit and a raise resumed nothing. Now the cap pauses it: the
    person raises it and the SAME conversation goes on."""
    task_id, step_id, ada_id = await _capped_task("0.50")
    requests: list[list[ModelMessage]] = []
    real_agent(_short_at(task_id, step_id, requests, "0.60"))
    said = await _run(task_id, step_id)
    assert said.startswith(execution.ASKING)
    gate: Gate = await _rows(task_id, lambda s: s.get(Gate, uuid.UUID(said.removeprefix(execution.ASKING))))
    assert gate.kind == GateKind.BUDGET.value and gate.step_id == step_id

    await _answer(ada_id, task_id, str(gate.id), GateAction.ACCEPT, usd="2.00")
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    assert await _run(task_id, step_id) == "Summarized 30 stories."
    assert len(requests) == 2 and len(requests[1]) > len(requests[0]), "the same conversation"
    assert BUDGET_CONTINUE in _told(requests[1])


async def test_keeping_the_cap_on_a_partial_keeps_the_models_own_account(
    fake_frames: Any,
    sidecar: None,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    task_id, step_id, ada_id = await _capped_task("0.50")
    requests: list[list[ModelMessage]] = []
    real_agent(_short_at(task_id, step_id, requests, "0.60"))
    said = await _run(task_id, step_id)

    await _answer(ada_id, task_id, said.removeprefix(execution.ASKING), GateAction.DECLINE)
    (wake,) = _wakes(recording_dispatcher, step_id)
    await _wake(task_id, step_id, wake)
    said = await _run(task_id, step_id)
    assert "Read 2 of 15 stories" in said
    assert len(requests) == 1, "a kept cap buys no model call"
    step: PlanStep = await _rows(task_id, lambda s: s.get(PlanStep, step_id))
    assert step.step_state is StepState.DONE


async def test_a_partial_under_the_cap_ends_as_itself(
    fake_frames: Any, sidecar: None, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """A page that never responds ends `partial` with money left: no gate, no second request."""
    task_id, step_id, _ = await _capped_task("5.00")
    requests: list[list[ModelMessage]] = []
    real_agent(_short_at(task_id, step_id, requests, "0.60"))
    said = await _run(task_id, step_id)
    assert "Read 2 of 15 stories" in said and len(requests) == 1
    gates_of: list[Gate] = await _rows(task_id, lambda s: Gate.for_task(s, task_id))
    assert gates_of == []
