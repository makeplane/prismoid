# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The turn driver: one real turn of the engine, in this process, with only the edges faked.

What is real: `run_task`'s own body, every effect it calls, the turn's route and the Lead's commit, the
step executor, publish and harvest, delivery, the verifier's suite, memory_extract and the namer,
Postgres. What is faked: the models (`Voice`s: the resolve seam hands `build` a `_Seat`, and the
one `Agent(...)` call in `build` swaps it for the voice of the agent it names), the durable
engine's queue (a started step runs at once, in this process, and joining it awaits it), and the
Machine (`FakeFrame`, from the `fake_frames` fixture a scenario's test asks for). The verifier's
fresh frame is a `_SuiteFrame`: a FakeFrame whose system Python runs the real suite script.

A scenario is declared in a family file (`scenarios/*.py`); this module runs it and hands back the
turn's durable record. It decides nothing about what the record should look like.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

import prismoid.ai.agents.base as agents_base
from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import ARTIFACTS, BROWSER, INPUTS, SKILLS, TASK, VERIFY, WORK, FramePaths
from prismoid.ai.gateway.meter import EmitUsage, MeterContext
from prismoid.ai.verification.pipeline import EXTRACT_FRAME_PATH
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import durable
from prismoid.jobs import run_task as run_task_job
from prismoid.operations import dispatch, live, verification
from prismoid.operations import tasks as task_ops
from prismoid.operations.live import Frame
from prismoid.schemas.events import DurableEvent
from prismoid.schemas.outputs import MemoryExtract, TaskTitle
from tests.contract.harness.people import Room, Stage, as_workflow
from tests.contract.harness.voices import Reply, Voice, submits
from tests.contract.harness.world import World
from tests.support.cast import member
from tests.support.fakes.fake_frames import FakeFrame

# The machinery agents every turn may consult. A scenario overrides one by naming it in its own
# voices; the rest answer the same way in every scenario, so a shape never depends on them.
MACHINERY: Mapping[str, tuple[Reply, ...]] = {
    "namer": (submits(TaskTitle(title="A title")),),
    "memory_extract": (submits(MemoryExtract()),),
}


# A row the ask names; it may hand the door dials it made (a seat's catalog ref, its Project).
type Setup = Callable[[AsyncSession, Actor], Awaitable[Mapping[str, Any] | None]]
type Move = Callable[[Run], Awaitable[None]]
type Edge = tuple[uuid.UUID, uuid.UUID, str]


@dataclass(frozen=True)
class Scenario:
    """One turn, declared: the ask, its dials, what each agent says, the rows it names (`given`)
    and the moves after the first turn (`then`); voices are consumed across all of them."""

    name: str
    goal: str
    voices: Mapping[str, tuple[Reply, ...]]
    dials: Mapping[str, Any] = field(default_factory=dict)
    given: tuple[Setup, ...] = ()
    then: tuple[Move, ...] = ()
    stage: Stage = field(default_factory=Stage)  # the people acting on the turn while it runs (`people.py`)
    world: World = field(default_factory=World)  # what a request costs, the service it reaches (`world.py`)


@dataclass
class Turn:
    """What a run of a scenario left behind."""

    outcome: str  # the workflow's own word: delivered | failed | cancelled
    events: list[DurableEvent]
    edges: list[Edge]  # the plan's dependency edges, from `plan_edges` (`task.planned` carries none)
    facts: dict[str, Any]  # what the scenario's moves chose to hold beside the record (shape.py: `facts`)


@dataclass
class Queued:
    """A job the API side handed the dispatcher, as the worker would be handed it."""

    job: str
    key: str  # the deterministic id
    args: dict[str, Any]
    played: bool = False


class _Cast:
    """What one run fakes and remembers: the voice of each agent name, and the jobs the API side
    started (`enqueued`), for a move that plays one of them."""

    def __init__(self, scenario: Scenario) -> None:
        self.scripted = frozenset(scenario.voices)
        machinery = {name: Voice(name, r, repeats=True) for name, r in MACHINERY.items()}
        self.voices = machinery | {name: Voice(name, r) for name, r in scenario.voices.items()}
        self.unscripted: list[str] = []
        self.world = scenario.world
        self.enqueued: list[Queued] = []  # in the order started

    async def notify(self, frame: Frame) -> None:
        return None

    async def enqueue(self, job: str, key: str, **kwargs: object) -> None:
        self.enqueued.append(Queued(job, key, dict(kwargs)))

    async def send(self, key: str, payload: object, topic: str, *, missing_ok: bool = False) -> None:
        return None

    def resolve(
        self,
        model_class: object,
        catalog: object,
        *,
        context: MeterContext,
        emit_usage: EmitUsage | None = None,
        pin: str | None = None,
        **_: object,
    ) -> _Seat:
        """The model seam: no model yet, only what the agent's voice will need once `build` names it."""
        return _Seat(pin, emit_usage, context)

    def agent(self, model: object, /, *, name: str, **kwargs: Any) -> Agent[Any, Any]:
        """`build`'s `Agent(...)`, with the seat swapped for the voice of the agent it names."""
        assert isinstance(model, _Seat), f"agent {name!r} was built around a model the rig did not resolve"
        return Agent(self.voice(name, model), name=name, **kwargs)

    def voice(self, name: str, seat: _Seat) -> Model:
        if name not in self.voices:
            self.unscripted.append(name)
            raise AssertionError(f"the scenario gives agent {name!r} no voice")
        return self.world.model(self.voices[name].model(), name, seat.emit, seat.context)

    def check(self) -> None:
        """Every agent asked had a voice, no reply was refused (an ungranted tool, text from a non-texter,
        one request too many), and every reply the scenario scripted was asked for."""
        assert not self.unscripted, f"agents without a voice: {self.unscripted}"
        faults = {name: v.faults for name, v in self.voices.items() if v.faults}
        assert not faults, f"scripted replies refused: {faults}"
        left = {name: v.unused for name, v in self.voices.items() if name in self.scripted and v.unused}
        assert not left, f"scripted replies the turn never asked for: {left}"


@dataclass(frozen=True)
class _Seat:
    """What the resolve seam was asked for: the pinned ref and the meter's wiring."""

    pin: str | None
    emit: EmitUsage | None
    context: MeterContext


class _Inline:
    """The durable engine's queue, in process: `start` runs the job now as a task under its workflow
    id, `join` awaits it."""

    async def start(
        self, lane: object, key: str, fn: Callable[..., Any], /, *args: object, **kwargs: object
    ) -> asyncio.Task[Any]:
        return asyncio.ensure_future(as_workflow(key, inspect.unwrap(fn)(*args, **kwargs)))

    async def join(self, handle: asyncio.Task[Any]) -> Any:
        return await handle


class _SuiteFrame(FakeFrame):
    """The verifier's fresh frame: its roots are a scratch folder, where the real suite script runs."""

    def __init__(self, task_id: str, **kwargs: Any) -> None:
        super().__init__(task_id, **kwargs)
        self._root = tempfile.mkdtemp(prefix="contract-verify-")
        root = self._root
        self.paths = FramePaths(
            task=f"{root}{TASK}",
            work=f"{root}{WORK}",
            artifacts=f"{root}{ARTIFACTS}",
            skills=f"{root}{SKILLS}",
            verify=f"{root}{VERIFY}",
            inputs=f"{root}{INPUTS}",
            browser=f"{root}{BROWSER}",
            on_host=True,
        )

    async def system_python(
        self,
        script: str,
        *args: str,
        stdin: str | None = None,
        timeout: float = 120.0,  # noqa: ASYNC109
    ) -> ExecResult:
        """Write the frame's files to its roots, run the script under this Python, read the extract back."""
        self._lay_out()
        ran = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            self.paths.render(script),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        given = self.files[stdin][0] if stdin else b""
        out, err = await asyncio.wait_for(ran.communicate(given), timeout)
        if (extract := self._extract()) is not None:
            await self.write_bytes(EXTRACT_FRAME_PATH, extract)
        return ExecResult(ran.returncode or 0, out.decode(), err.decode())

    def _lay_out(self) -> None:
        for virtual, (data, _) in self.files.items():
            real = Path(self.paths.render(virtual))
            assert real.is_relative_to(self._root), f"{virtual} is under none of the frame's roots"
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_bytes(data)

    def _extract(self) -> bytes | None:
        extract = Path(self.paths.render(EXTRACT_FRAME_PATH))
        return extract.read_bytes() if extract.exists() else None

    async def destroy(self) -> None:
        await super().destroy()
        shutil.rmtree(self._root, ignore_errors=True)


def _suite_frame(task_id: str, **kwargs: Any) -> FakeFrame:
    return _SuiteFrame(task_id, **kwargs)


async def _no_previews(frame: object, manifest: object) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


def install(monkeypatch: pytest.MonkeyPatch, scenario: Scenario) -> _Cast:
    """Fake the edges for one run. The tests ask for `fake_frames` and `silent_dispatcher` too."""
    cast = _Cast(scenario)
    inline = _Inline()
    monkeypatch.setattr(agents_base, "resolve", cast.resolve)
    monkeypatch.setattr(agents_base, "Agent", cast.agent)
    scenario.world.install(monkeypatch)
    monkeypatch.setattr(live, "notify", cast.notify)
    dispatch.set_dispatcher(cast)  # the tests' `silent_dispatcher` fixture puts the null one back
    monkeypatch.setattr(durable, "start", inline.start)
    monkeypatch.setattr(durable, "join", inline.join)
    monkeypatch.setattr("prismoid.operations.execution.harvest.extract_previews", _no_previews)
    monkeypatch.setattr(verification, "new_frame", _suite_frame)
    monkeypatch.setattr(verification, "ensure_ready", lambda *_: None)
    return cast


@dataclass
class Run:
    """The scenario in progress, as a move sees it: who asked, the task, what the dispatcher was
    handed, and the facts held so far."""

    owner_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    cast: _Cast
    outcome: str = ""
    facts: dict[str, Any] = field(default_factory=dict[str, Any])

    @asynccontextmanager
    async def acting(self) -> AsyncGenerator[tuple[AsyncSession, Actor]]:
        """A unit of work in the workspace, as the person who asked."""
        async with session_scope(workspace_id=self.workspace_id) as session:
            actor = await session.get(Actor, self.owner_id)
            assert actor is not None
            yield session, actor

    async def play(self, turn: int = 1) -> None:
        """Run one turn of the task to its end, as the worker does once the queue starts it."""
        job = inspect.unwrap(run_task_job.run_task)(str(self.task_id), turn)
        self.outcome = await as_workflow(dispatch.task_turn_job_key(self.task_id, turn), job)


async def run_turn(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> Turn:
    """Create the task as the door would, run its first turn and the scenario's moves; the record."""
    cast = install(monkeypatch, scenario)
    room = Room(scenario.stage)
    async with session_scope() as session:
        actor, workspace = await member(session, "Contract")
        await session.flush()
        made: dict[str, Any] = {}
        for setup in scenario.given:
            made |= await setup(session, actor) or {}
        seated = await room.seat(session, actor, workspace)
        task = await task_ops.create_task(
            session, actor, goal=scenario.goal, clarify="never", **scenario.dials, **made, **seated
        )
        await session.commit()
        run = Run(owner_id=actor.id, workspace_id=workspace.id, task_id=task.id, cast=cast)

    with room.present(run.task_id, monkeypatch):
        await run.play()
        for move in scenario.then:
            await move(run)
    if room.record:
        run.facts["people"] = room.record
    cast.check()
    events, edges = await record_of(run.task_id)
    return Turn(outcome=run.outcome, events=events, edges=edges, facts=run.facts)


async def record_of(task_id: uuid.UUID, since: int = 0) -> tuple[list[DurableEvent], list[Edge]]:
    """The task's durable record after `since`, in order, and its plan's dependency edges."""
    async with row_scope(Task, task_id) as session:
        events: list[DurableEvent] = []
        while page := await TaskEvent.replay(session, task_id, since=events[-1].seq if events else since):
            events += page
        edges = [(e.from_step_id, e.to_step_id, e.kind) for e in await PlanEdge.for_task(session, task_id)]
        return events, edges
