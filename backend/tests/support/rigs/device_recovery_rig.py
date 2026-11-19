# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the device-recovery tests, shared by the
`test_device_recovery*` files: a device step run by the real `execute_step` against real rows, the real
`build()` with a scripted model, and a worker death (`WorkerDied`) in place of a crash. A plain module,
not a test file."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import prismoid
import prismoid.ai.agents.base as agents_base
from prismoid.ai.tools import frame_tools
from prismoid.conf import settings
from prismoid.db import engine as engine_module
from prismoid.db import session as session_module
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import PolicyDenied
from prismoid.operations import execution, gates, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import device as device_ops
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.schemas.contract import Contract
from prismoid.schemas.device import InterruptedOut
from prismoid.schemas.ids import AgentName
from prismoid.schemas.machines import FrameDriverName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import DEVICE, NoneSleepFakeFrame

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]


APPEND = "echo line >> /work/log.txt && sleep 30"


WRITE = ("write_file", {"path": "/work/memo.md", "content": "done"})


PUBLISH = ("publish", {"path": "/work/memo.md", "type": "document", "title": "Memo"})


NOTE = (
    f"Your last command was cut off: `{APPEND}`. Check the folder (git status, the files you touched) "
    "before you act."
)


class WorkerDied(BaseException):
    """The worker process going down mid-run: nothing in the step handles it, nothing after the
    last commit is recorded."""


def recovery_spec(title: str) -> dict[str, Any]:
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


async def device_step(
    driver: FrameDriverName | None = DEVICE,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ada's Project task on her device (or a cloud one), Bob an editor, one READY coder step.
    (task, step, ada, bob)"""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"p-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(
            s, ada, goal="fix the build", project_id=project.id, clarify="never"
        )
        task.machine_driver = driver
        task.transition(TaskStatus.PLANNING)
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="fix",
            spec=recovery_spec("fix"),
            attempt=0,
            epoch=task.plan_epoch,
            turn_epoch=task.turn_epoch,
        )
        s.add(step)
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        return task.id, step.id, ada.id, bob.id


async def recovery_run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _resume(actor_id: uuid.UUID, task_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        await steering.resume(s, actor, task_id)


async def recovery_wake(task_id: uuid.UUID, step_id: uuid.UUID, wake: dict[str, Any]) -> None:
    """`run_step`'s half after its recv returns: the wake goes where the resume finds it."""
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, wake)
        await session.commit()


def step_wakes(sent: RecordingDispatcher, step_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], p) for k, p, t in sent.sends if k == f"step-{step_id}" and t == gates.GATE_TOPIC
    ]


async def resume_and_wake(
    sent: RecordingDispatcher, actor_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID
) -> None:
    await _resume(actor_id, task_id)
    await recovery_wake(task_id, step_id, step_wakes(sent, step_id)[-1])


async def recovery_row[R](task_id: uuid.UUID, kind: type[R], row_id: uuid.UUID) -> R:
    async with row_scope(Task, task_id) as s:
        row = await s.get(kind, row_id)
        assert row is not None
        s.expunge(row)
        return row


async def gates_of(task_id: uuid.UUID) -> list[Gate]:
    async with row_scope(Task, task_id) as s:
        rows = await Gate.for_task(s, task_id)
        for row in rows:
            s.expunge(row)
        return rows


async def recovery_events[E](task_id: uuid.UUID, kind: type[E]) -> list[E]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, kind)]


def recovery_told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def call(name: str, args: dict[str, Any], call_id: str) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


@pytest.fixture(autouse=True, scope="module")
async def private_database() -> AsyncIterator[None]:
    """This file's own database, migrated to this checkout's head: the dev DB is shared with every
    other branch, and a run here must neither depend on what it was last migrated to nor touch it."""
    url = settings.api.database_url
    root, _, name = url.replace("+asyncpg", "").rpartition("/")
    private = f"{name}_recovery_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(f"{root}/postgres")
    await admin.execute(f'create database "{private}"')
    await engine_module.dispose_engine()
    engine_module.get_engine.cache_clear()
    session_module.get_sessionmaker.cache_clear()
    moved = f"{url.rpartition('/')[0]}/{private}"
    try:
        migrated = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(prismoid.__file__).parents[1],
            env={
                **os.environ,
                "PRISMOID_API_DATABASE_URL": moved,
                "PRISMOID_WORKER_DATABASE_URL": f"{root}/{private}",
            },
            capture_output=True,
            text=True,
        )
        assert migrated.returncode == 0, migrated.stderr
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settings.api, "database_url", moved)
            yield
            await engine_module.dispose_engine()
            engine_module.get_engine.cache_clear()
            session_module.get_sessionmaker.cache_clear()
    finally:
        await admin.execute(f'drop database if exists "{private}" with (force)')
        await admin.close()


@pytest.fixture
def real_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[Stream], None]:
    """The real `build()` with a scripted model (a step agent streams, so `stream_function`)."""
    monkeypatch.setattr(ex_harvest, "extract_previews", _no_previews)

    def use(stream: Stream) -> None:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))

    return use


@pytest.fixture
def quick_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The device's grace in tenths of a second, asked again every 50 ms."""
    monkeypatch.setattr(device_ops, "DEVICE_GRACE_S", 0.6)
    monkeypatch.setattr(device_ops, "DEVICE_RETRY_S", 0.05)
    monkeypatch.setattr(settings.machines, "wait_s", 5)


# What a resumed attempt hears about a crash's command (`brief.interruption_note`, either wording).
TOLD_TO_LOOK = (
    "Your last command was cut off:",
    "Your last command was still running when Prismoid recovered:",
    "Your last command finished while Prismoid was away,",
)


def working(requests: list[list[ModelMessage]], *, dies: bool, command: str = APPEND) -> Stream:
    """The model's script. Every fresh conversation starts by running its command - so a blind
    re-run, the defect this file guards, runs it again - unless it was told what a crash cut off:
    then it looks instead. Then it writes the memo, publishes it and finishes. `dies`: the worker
    dies while the first conversation's command runs. `command`: on a real helper, the real path the
    model would have been told."""
    died = False

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal died
        requests.append(list(messages))
        told = any(words in recovery_told(messages) for words in TOLD_TO_LOOK)
        script = [WRITE, PUBLISH] if told else [("shell", {"command": command}), WRITE, PUBLISH]
        returns = sum(
            isinstance(p, ToolReturnPart) for m in messages if isinstance(m, ModelRequest) for p in m.parts
        )
        if dies and not died and not told and returns == 1:
            died = True
            raise WorkerDied  # the command's turn came back to a worker that is gone
        if returns < len(script):
            yield call(*script[returns], f"c{len(requests)}")
        else:
            yield "done"

    return stream


def journal_names(fake_frames: Any, *, still_running: bool = False, exit_code: int | None = None) -> None:
    """The helper's journal: the command the worker died under stays named on the device's frame."""
    for frame in fake_frames.world.values():
        if isinstance(frame, NoneSleepFakeFrame):
            frame.cut = InterruptedOut(
                exec_id="e1",
                command_head=APPEND,
                started_at=utcnow(),
                still_running=still_running,
                exit_code=exit_code,
            )


def recovery_ran(fake_frames: Any, command: str) -> int:
    return sum(f.execs.count(command) for f in fake_frames.world.values())


async def crashed_on_the_device(
    fake_frames: Any, real_agent: Callable[[Stream], None], requests: list[list[ModelMessage]]
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A device step whose worker died while its command ran; the helper's journal still names it."""
    task_id, step_id, ada_id, bob_id = await device_step()
    real_agent(working(requests, dies=True))
    with pytest.raises(WorkerDied):
        await recovery_run(task_id, step_id)
    journal_names(fake_frames)
    return task_id, step_id, ada_id, bob_id


def _batch(*calls: tuple[str, dict[str, Any]]) -> DeltaToolCalls:
    """One model turn of parallel calls."""
    return {
        i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"b{i}")
        for i, (name, args) in enumerate(calls)
    }


def beside_a_refusal(requests: list[list[ModelMessage]], *, command: str = APPEND) -> Stream:
    """The model's script for a parallel batch: the first request runs the command
    beside a read that is refused. Any later one writes the memo, publishes it and finishes."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            yield _batch(("shell", {"command": command}), ("read_file", {"path": "/work/refused.md"}))
            return
        ran = {
            p.tool_name
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        }
        if WRITE[0] not in ran:
            yield call(*WRITE, f"c{len(requests)}")
        elif PUBLISH[0] not in ran:
            yield call(*PUBLISH, f"c{len(requests)}")
        else:
            yield "done"

    return stream


def refuse_reads(monkeypatch: pytest.MonkeyPatch, *, once: Callable[[], Awaitable[None]]) -> None:
    """`read_file` is refused by policy, and the refusal ends the run: `PolicyDenied` out of a tool
    cancels the batch's siblings. It raises only after `once`, so the command beside it is running
    by then, and only the first time."""
    real = vars(frame_tools)["_read_text_confined"]
    refused = False

    async def read(frame: Any, path: str) -> str:
        nonlocal refused
        if not refused:
            refused = True  # the frame is never read
            await once()
            raise PolicyDenied("frame.read_file was refused for this step")
        return await real(frame, path)

    monkeypatch.setattr(frame_tools, "_read_text_confined", read)
