# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task that stops settles the steps it never started, on the record, in its stopping transaction.

Settling only the step that was running would leave the never-started steps `pending` in
plan_steps for ever. A stopped task's plan reads honestly: every step done, escalated, or skipped with why."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.operations import stopped
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.plan import StepState, TaskStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from prismoid.db.models.task import Task


def _step(state: StepState, *, attempt: int = 0, title: str = "") -> PlanStep:
    step = PlanStep(
        id=uuid.uuid4(), task_id=uuid.uuid4(), ordinal=0, kind="step", title=title or state.value, spec={}
    )
    step.state, step.attempt = state.value, attempt
    return step


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[StepStateChanged]:
    events: list[StepStateChanged] = []

    async def append(session: Any, event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(TaskEvent, "append", append)
    return events


def _task(status: TaskStatus) -> Any:
    seqs = iter(range(100, 200))

    async def next_seq(session: Any) -> int:
        return next(seqs)

    return SimpleNamespace(
        id=uuid.uuid4(), project_id=None, status=status.value, next_seq=next_seq, is_terminal=lambda: True
    )


@pytest.mark.parametrize(
    ("status", "why"),
    [
        (TaskStatus.CANCELLED, "not started: the task was cancelled"),
        (TaskStatus.FAILED, "not started: the task failed"),
    ],
)
async def test_never_started_steps_settle_skipped_and_say_why(
    status: TaskStatus, why: str, written: list[StepStateChanged], monkeypatch: pytest.MonkeyPatch
) -> None:
    pending, ready = _step(StepState.PENDING), _step(StepState.READY)
    paced = _step(StepState.READY, attempt=2)  # a rung waiting out its pace: its own ladder settles it
    done, escalated = _step(StepState.DONE), _step(StepState.ESCALATED)
    running = _step(StepState.RUNNING, attempt=1)  # the safe point stops it (§9)
    steps = [pending, ready, paced, done, escalated, running]

    async def for_task(session: Any, task_id: Any) -> list[PlanStep]:
        return steps

    monkeypatch.setattr(PlanStep, "for_task", for_task)
    await stopped.settle_unstarted(cast("AsyncSession", None), cast("Task", _task(status)))

    assert [s.step_state for s in steps] == [
        StepState.SKIPPED,
        StepState.SKIPPED,
        StepState.READY,
        StepState.DONE,
        StepState.ESCALATED,
        StepState.RUNNING,
    ]
    assert [(e.state, e.title, e.detail) for e in written] == [
        (StepState.SKIPPED, "pending", why),
        (StepState.SKIPPED, "ready", why),
    ]


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (TaskStatus.CANCELLED, "the task was cancelled"),
        (TaskStatus.FAILED, "the task failed"),
        (TaskStatus.DELIVERED, "the task was delivered"),
    ],
)
def test_how_a_stopped_task_is_said(status: TaskStatus, said: str) -> None:
    assert stopped.ended_said(status.value) == said
