# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step its task ended under settles `skipped`, never `retrying`.

Cancelled mid-step, the safe point stops the run (§9); the ladder must not read the stop as an
attempt that published nothing (`step.state{retrying, "**nothing was published.** the task ended
while this step ran - stopped"}`), or the task view shows the step retrying for ever on a
cancelled task."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from prismoid.db.models.plan import PlanStep
from prismoid.operations import call_log
from prismoid.operations.execution.handlers import produce
from prismoid.operations.execution.handlers.state import Ran
from prismoid.schemas.plan import StepState, TaskStatus

if TYPE_CHECKING:
    from prismoid.operations.execution.handlers.state import Ladder


def _running() -> PlanStep:
    step = PlanStep(
        id=uuid.uuid4(), task_id=uuid.uuid4(), ordinal=0, kind="step", title="Write the essay", spec={}
    )
    step.state = StepState.RUNNING.value
    step.attempt = 1
    return step


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def refresh(self, obj: Any, attribute_names: Any = None) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.parametrize("status", [TaskStatus.CANCELLED, TaskStatus.FAILED])
async def test_a_step_its_task_ended_under_settles_skipped(
    status: TaskStatus, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[tuple[StepState, str]] = []

    async def step_event(session: Any, task: Any, step: Any, state: StepState, *, detail: str = "") -> None:
        said.append((state, detail))

    forgotten: list[uuid.UUID] = []

    async def forget(session: Any, task_id: uuid.UUID) -> None:
        forgotten.append(task_id)

    monkeypatch.setattr(produce, "step_event", step_event)
    monkeypatch.setattr(call_log, "forget", forget)
    step, session = _running(), _Session()
    task = SimpleNamespace(id=step.task_id, status=status.value, is_terminal=lambda: True)
    ladder = SimpleNamespace(session=session, task=task, step=step, spec=None, gotchas=[])
    stopped = "the task ended while this step ran - stopped"

    final = await produce.nothing_published(cast("Ladder", ladder), Ran("", [], None, undelivered=stopped))

    assert step.step_state is StepState.SKIPPED
    want = "stopped: the task failed" if status is TaskStatus.FAILED else "stopped: the task was cancelled"
    assert said == [(StepState.SKIPPED, want)]
    assert final == "(the task ended while step 'Write the essay' was in flight)"
    assert session.commits == 1
    assert forgotten == [step.task_id], "the rows the run logged after the end go with the step"


def test_running_and_verifying_may_settle_skipped_when_the_task_ends() -> None:
    for state in (StepState.RUNNING, StepState.VERIFYING):
        step = _running()
        step.state = state.value
        step.transition(StepState.SKIPPED)
        assert step.step_state is StepState.SKIPPED
