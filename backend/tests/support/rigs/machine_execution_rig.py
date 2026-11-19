# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the Machine-execution tests , shared by the `test_machine_execution*` files:
a task with steps, the real `execute_step` against real rows with the model stubbed, and every frame a
`FakeFrame`. A plain module, not a test file."""

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import member
from tests.support.runners import scripted

Run = Callable[..., Awaitable[Any]]


def _spec(title: str, agent: str = "maker", *, browser: bool = False) -> dict[str, Any]:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        browser=browser,
        title=title,
        contract=Contract(
            goal=title,
            acceptance=["it exists"],
            inputs=[],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=title),
        ),
    ).model_dump(mode="json")


async def task_with_steps(
    n: int, *, agent: str = "maker", browser: bool = False
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with session_scope() as s:
        actor, _ = await member(s, "Author")
        task = Task(
            id=uuid.uuid4(),
            goal="two memos",
            creator_actor_id=actor.id,
            criteria={},
            inputs=[],
            mode="standard",
        )
        s.add(task)
        await s.flush()
        task.transition(TaskStatus.PLANNING)
        ids = []
        for i in range(n):
            step = PlanStep(
                id=uuid.uuid4(),
                task_id=task.id,
                ordinal=i,
                kind=StepKind.STEP.value,
                state=StepState.READY.value,
                title=f"memo {i}",
                spec=_spec(f"memo {i}", agent, browser=browser),
                attempt=0,
            )
            s.add(step)
            ids.append(step.id)
        await s.commit()
    async with row_scope(Task, task.id) as session:
        await execution.advance(session, task.id, TaskStatus.RUNNING)
        await session.commit()
    return task.id, ids


async def step_run(
    task_id: uuid.UUID, step_id: uuid.UUID, *, seat: bool = False, handed_back: int | None = None
) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id, seat=seat, handed_back=handed_back)


async def step_machine(task_id: uuid.UUID) -> Machine | None:
    async with row_scope(Task, task_id) as session:
        row = await Machine.for_task(session, task_id)
        if row is not None:
            session.expunge(row)
        return row


def _work(frame: Any) -> str:
    return ",".join(sorted(p for p in getattr(frame, "files", {}) if p.startswith("/work/")))


async def step_publish(frame: Any, n: int) -> str:
    name = f"/work/step-{n}.md"
    await frame.write_bytes(name, b"# memo")
    await frame.publish(name, "document", f"memo {n}", ())
    return f"wrote {name}"


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Callable[[Run], None]:
    """Stub the model: the agent's build (no catalog model needed) and its run - and the preview
    renderer, which needs a real frame."""
    import prismoid.ai.agents.base as agents_base

    async def no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    monkeypatch.setattr(ex_harvest, "extract_previews", no_previews)

    def use(run: Run) -> None:
        monkeypatch.setattr(agents_base, "build", scripted(run))

    return use


@pytest.fixture
def canned(model: Callable[[Run], None]) -> list[str]:
    """The model writes /work/step-<n>.md, publishes it, and reports what /work held when it began."""
    seen: list[str] = []

    async def canned_run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        seen.append(_work(deps.frame))
        return SimpleNamespace(output=await step_publish(deps.frame, len(seen)))

    model(canned_run)
    return seen
