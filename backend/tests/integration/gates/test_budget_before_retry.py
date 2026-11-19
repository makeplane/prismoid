# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Bursar before every new attempt, not only before every wave: a cap is a cap.

Found live: a navigator step's first attempt spent its whole request budget, and the ladder
started a second attempt on the next seat with nobody asked - one task spent $13.61 against a
$3 cap, because the budget was consulted only between steps. Now an attempt that leaves the task
at or over its cap parks the step on the budget gate before the next attempt starts; accepting it
resumes the SAME step with its next attempt, and declining it ends the task, exactly as the
between-steps gate does.

`_attempt` is stubbed (the model- and Docker-bound half); the REAL `execute_step` runs the ladder
against real rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import ModelIncapable
from prismoid.operations import execution, gates
from prismoid.operations.execution.record import owed_run
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import (
    BudgetFigures,
    GateAction,
    GateKind,
    GateSpec,
    StepKind,
    StepSpec,
    StepState,
    TaskStatus,
)
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration

# A dead guarded attempt's conversation (agent loop spec 3.4): not ASCII, so the park's latin-1
# round trip through JSONB is proven byte for byte.


async def _task_with_a_cap(cap_usd: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        actor, _ = await member(session, "Owner")
        task = Task(
            id=uuid.uuid4(),
            goal="Update the records at https://admin.example.test/orders",
            creator_actor_id=actor.id,
            criteria={},
            inputs=[],
            budget={"usd": cap_usd, "tokens": None},
        )
        session.add(task)
        await session.flush()
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="a memo",
            spec=StepSpec(
                kind=StepKind.STEP,
                agent=AgentName("writer"),
                title="a memo",
                contract=Contract(
                    goal="Write a memo on Q3.",
                    acceptance=["every figure comes from the brief"],
                    output=OutputRef(type=ArtifactType.DOCUMENT, title="memo"),
                ),
            ).model_dump(mode="json"),
            attempt=0,
        )
        session.add(step)
        await session.commit()
        return task.id, step.id, actor.id


class _Ladder:
    """Attempt 1 spends past the cap and fails (a run that hit its request budget); any later
    attempt publishes. Records what each attempt was told. `raise_cap_to`: the owner raises the
    task's cap from another session while attempt 1 runs."""

    def __init__(self, raise_cap_to: str | None = None) -> None:
        self.told: list[list[str]] = []
        self.raise_cap_to = raise_cap_to
        self.carried: list[str | None] = []  # how each attempt takes up the run before it (spec 3.4)

    async def __call__(
        self,
        session: Any,
        task: Any,
        step: Any,
        spec: Any,
        gotchas: Any,
        upstream: Any = (),
        resume: Any = None,
        *,
        seat: bool = False,
        cut: Any = None,
        paper: bool = False,
        fresh: bool = True,
    ) -> tuple[str, list[object], None]:
        self.told.append(list(gotchas))
        owed = owed_run(step)
        self.carried.append(owed.pickup if owed is not None else None)
        if len(self.told) == 1:
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
            if self.raise_cap_to is not None:
                async with row_scope(Task, task.id) as owner_session:
                    row = await owner_session.get(Task, task.id)
                    assert row is not None
                    row.budget = {"usd": self.raise_cap_to, "tokens": None}
                    await owner_session.commit()
            raise ModelIncapable(
                "the run exceeded its request budget (request_limit of 50)", pickup="continue"
            )
        return "Updated.", [SimpleNamespace(id=uuid.uuid4(), role="deliverable")], None


async def _run(ladder: _Ladder, task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution, "_attempt", ladder)
        async with row_scope(Task, task_id) as session:
            return await execution.execute_step(session, task_id, step_id)


async def _parked_on_the_budget(recording_dispatcher: RecordingDispatcher) -> tuple[Any, ...]:
    task_id, step_id, owner_id = await _task_with_a_cap("1.00")
    ladder = _Ladder()
    outcome = await _run(ladder, task_id, step_id)
    assert outcome.startswith(execution.ASKING)
    return task_id, step_id, owner_id, ladder, uuid.UUID(outcome.removeprefix(execution.ASKING))


async def test_an_attempt_that_spends_the_cap_parks_before_the_next_one(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    task_id, step_id, _owner, ladder, gate_id = await _parked_on_the_budget(recording_dispatcher)

    assert len(ladder.told) == 1, "the second attempt never started"
    async with row_scope(Task, task_id) as session:
        gate = await session.get(Gate, gate_id)
        step = await session.get(PlanStep, step_id)
        task = await session.get(Task, task_id)
        assert gate is not None and step is not None and task is not None
        assert gate.kind == GateKind.BUDGET.value and gate.is_open()
        assert gate.step_id == step_id, "the answer wakes the parked step, not the task loop"
        assert gate.spec["reason"].startswith("the task budget of $1.00 is spent ($1.50")
        # The step's park wears the same figures as the frontier's: the dock's prefill and the
        # raise that resumes it read them, whichever caller opened the gate.
        spec = GateSpec.model_validate(gate.spec)
        assert spec.budget_scope == "task"
        assert spec.budget_figures == BudgetFigures(ceiling_usd=Decimal("1.00"), spent_usd=Decimal("1.50"))
        assert task.status == TaskStatus.WAITING.value
        assert step.step_state is StepState.RUNNING and step.attempt == 1 and step.infra_attempts == 0


async def test_a_cap_raised_while_an_attempt_runs_buys_the_next_attempt(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    """The rung top reads the task once, for the cancel and the Bursar alike: a cap the owner
    raised while attempt 1 spent past the old one is the cap attempt 2 is checked against."""
    task_id, step_id, _owner = await _task_with_a_cap("1.00")
    ladder = _Ladder(raise_cap_to="5.00")

    assert await _run(ladder, task_id, step_id) == "Updated."
    assert len(ladder.told) == 2, "no gate: $1.50 is inside the raised $5.00 cap"
    async with row_scope(Task, task_id) as session:
        assert not await Gate.open_budget_exists(session, task_id)


async def test_accepting_the_budget_gate_resumes_the_same_step_with_its_next_attempt(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    task_id, step_id, owner_id, ladder, gate_id = await _parked_on_the_budget(recording_dispatcher)

    async with row_scope(Task, task_id) as session:
        owner = await session.get(Actor, owner_id)
        assert owner is not None
        resolution = await gates.resolve(session, owner, gate_id, GateAction.ACCEPT)
    assert recording_dispatcher.sends[-1][0] == f"step-{step_id}"
    async with row_scope(Task, task_id) as session:  # the run_step workflow's wake, as it stores it
        await execution.store_answer(session, step_id, resolution.model_dump(mode="json"))
        await session.commit()

    assert await _run(ladder, task_id, step_id) == "Updated."
    assert len(ladder.told) == 2
    assert any("request budget" in g for g in ladder.told[1]), "attempt 2 is told what went wrong"
    assert ladder.carried == [None, "continue"], (
        "how the dead run is taken up rides the record through the park"
    )
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        task = await session.get(Task, task_id)
        assert step is not None and task is not None
        assert step.step_state is StepState.DONE
        assert (step.attempt, step.infra_attempts) == (2, 0), "the park spent no rung"
        assert task.status == TaskStatus.RUNNING.value


async def test_declining_the_budget_gate_ends_the_task(recording_dispatcher: RecordingDispatcher) -> None:
    task_id, step_id, owner_id, ladder, gate_id = await _parked_on_the_budget(recording_dispatcher)

    async with row_scope(Task, task_id) as session:
        owner = await session.get(Actor, owner_id)
        assert owner is not None
        resolution = await gates.resolve(session, owner, gate_id, GateAction.DECLINE)
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, resolution.model_dump(mode="json"))
        await session.commit()

    await _run(ladder, task_id, step_id)
    assert len(ladder.told) == 1, "a budget gate has no safe default: no attempt past a declined cap"
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None and task.status == TaskStatus.CANCELLED.value
