# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The people in the room: what a person does to a running task, and exactly when.

A steering scenario is a turn with a person acting on it. The act is the real operation
(`steering.steer`, `retract`, `pause`, `resume`, `cancel`) from the real actor; only WHEN it happens
is scripted, at the edges where the turn is between two of its own steps:

    meanwhile(act, reply)   while a model is answering: a call it then chooses was chosen before
    Edge.HARVEST            a step has published and not yet settled (its fence has not run)
    Edge.JOIN               a step's workflow has ended and the loop has not looked at it yet
    Edge.PARK               a workflow is parked: the person acts, and the engine's own wake,
                            caught as it is sent, is what ends the wait

Nothing sleeps: an edge is a point the engine reaches, so act and engine happen in the same order
in every run. A park nobody wakes fails the scenario instead of waiting a day.

What the engine answered each act, and where it parked, is the turn's `people` fact (`Room.record`):
a refused steer leaves no row in `task_events`, so the 409 and the epoch are visible only there.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pytest
from pydantic_ai.models.function import AgentInfo
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import Workspace, WorkspaceRole
from prismoid.db.session import row_scope
from prismoid.errors import PrismoidError, StaleSteer
from prismoid.jobs import durable
from prismoid.operations import dispatch, execution, steering
from prismoid.operations import projects as project_ops
from prismoid.operations.dispatch import Dispatcher
from prismoid.operations.execution import harvest
from tests.contract.harness.voices import Reply, parts_of
from tests.support.cast import member

type Row = dict[str, Any]
type Act = Callable[[Room], Awaitable[None]]


class Who(StrEnum):
    OWNER = "owner"  # whoever asked
    TEAMMATE = "teammate"  # an editor of the Project the task lives in


class Edge(StrEnum):
    HARVEST = "harvest"
    JOIN = "join"
    PARK = "park"


@dataclass(frozen=True)
class Stage:
    """Who is in the room, and what is done at each visit to an edge, in order. A visit past the
    last act does nothing, except a park, which then needs a wake already sent."""

    teammate: bool = False
    route_out: bool = False  # the steps' frames may reach a host: their shell could have acted in the world
    at: Mapping[Edge, tuple[Act, ...]] = field(default_factory=dict[Edge, tuple[Act, ...]])


# ── the acts ──────────────────────────────────────────────────────────────────────────────────


def steers(text: str, *, by: Who = Who.OWNER, saw: int | None = None) -> Act:
    """Send a steer. `saw` is the epoch their screen showed when they began typing (none: the chat
    door, which is never stale)."""
    return lambda room: room.steer(text, by, saw)


def retracts(nth: int = 1) -> Act:
    """The author withdraws the nth steer sent in this turn (counting from one)."""
    return lambda room: room.retract(nth)


def pauses(by: Who = Who.OWNER) -> Act:
    return lambda room: room.status("pause", steering.pause, by)


def resumes(by: Who = Who.OWNER) -> Act:
    return lambda room: room.status("resume", steering.resume, by)


def cancels(by: Who = Who.OWNER) -> Act:
    return lambda room: room.status("cancel", steering.cancel, by)


def together(*acts: Act) -> Act:
    async def all_of(room: Room) -> None:
        for act in acts:
            await act(room)

    return all_of


def meanwhile(act: Act, reply: Reply) -> Reply:
    """The person acts while the model is answering, then the model gives `reply`: a call it
    chooses here was chosen before the act."""

    async def respond(info: AgentInfo) -> Any:
        await act(_ROOM.get())
        return await parts_of(reply, info)

    return respond


# ── the room ──────────────────────────────────────────────────────────────────────────────────

_ROOM: contextvars.ContextVar[Room] = contextvars.ContextVar("contract_room")
_WORKFLOW: contextvars.ContextVar[str] = contextvars.ContextVar("contract_workflow")


async def as_workflow[R](key: str, run: Awaitable[R]) -> R:
    """Run `run` as the durable workflow of id `key`: the address a park there is woken at."""
    token = _WORKFLOW.set(key)
    try:
        return await run
    finally:
        _WORKFLOW.reset(token)


async def _a_host(*_: object) -> set[str]:
    return {"api.example.com"}


class _Mailbox:
    """The dispatcher of a turn run in process: every wake the engine sends, kept by workflow and
    topic for the park that reads it; whatever else it is asked goes on to the dispatcher it replaces."""

    def __init__(self, inner: Dispatcher) -> None:
        self.sent: dict[tuple[str, str], list[Any]] = {}
        self._inner = inner

    async def enqueue(self, job: str, key: str, **kwargs: Any) -> None:
        await self._inner.enqueue(job, key, **kwargs)

    async def send(self, key: str, payload: Any, topic: str, *, missing_ok: bool = False) -> None:
        self.sent.setdefault((key, topic), []).append(payload)


class Room:
    """One run's people and what they did. Built by the rig for a scenario's `Stage`."""

    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        self.record: list[Row] = []  # every act and every park, in order, with what the engine answered
        self.task_id = uuid.uuid4()
        self._people: dict[Who, uuid.UUID] = {}
        self._names: dict[str, Who] = {}
        self._sent: list[tuple[Who, uuid.UUID]] = []  # steers sent, in order: who, which command
        self._visits = dict.fromkeys(Edge, 0)
        self._mail = _Mailbox(dispatch.NullDispatcher())

    async def seat(self, session: AsyncSession, owner: Actor, workspace: Workspace) -> dict[str, Any]:
        """Seat the people the stage names; the dials the task is made with. A teammate means a
        Project where they are an editor (anyone else's steer would be a 404)."""
        self._people[Who.OWNER] = owner.id
        self._names[owner.display_name] = Who.OWNER
        if not self.stage.teammate:
            return {}
        mate, _ = await member(session, "Teammate", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(session, owner, name="contract")).project
        session.add(ProjectMember(project_id=project.id, actor_id=mate.id, role="editor", added_by=owner.id))
        await session.flush()
        self._people[Who.TEAMMATE] = mate.id
        self._names[mate.display_name] = Who.TEAMMATE
        return {"project_id": project.id}

    @contextlib.contextmanager
    def present(self, task_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
        """The turn, while it runs: the engine's edges reach this room until it ends."""
        self.task_id = task_id
        self._mail = _Mailbox(dispatch.get_dispatcher())
        monkeypatch.setattr(dispatch, "_dispatcher", self._mail)
        monkeypatch.setattr(durable, "wait", self._park)
        monkeypatch.setattr(durable, "join", self._after(durable.join, Edge.JOIN))
        monkeypatch.setattr(harvest, "extract_previews", self._after(harvest.extract_previews, Edge.HARVEST))
        if self.stage.route_out:
            monkeypatch.setattr(execution, "frame_allowlist", _a_host)
        token = _ROOM.set(self)
        try:
            yield
        finally:
            _ROOM.reset(token)

    # the edges

    def _after[**P, R](self, real: Callable[P, Awaitable[R]], edge: Edge) -> Callable[P, Awaitable[R]]:
        async def run(*args: P.args, **kwargs: P.kwargs) -> R:
            result = await real(*args, **kwargs)
            await self._visit(edge)
            return result

        return run

    async def _visit(self, edge: Edge) -> None:
        visit = self._visits[edge]
        self._visits[edge] += 1
        acts = self.stage.at.get(edge, ())
        if visit < len(acts):
            await acts[visit](self)

    async def _park(self, topic: str, timeout_s: float) -> Any:
        self.record.append({"did": "parked", "on": topic})
        await self._visit(Edge.PARK)
        here = _WORKFLOW.get()
        if waiting := self._mail.sent.get((here, topic)):
            return waiting.pop(0)
        elsewhere = sorted(key for (key, on), wakes in self._mail.sent.items() if on == topic and wakes)
        assert not elsewhere, f"woken at {elsewhere} on {topic!r}, parked at {here!r}"
        raise AssertionError(f"the turn parked on {topic!r} and no act woke it: it would wait {timeout_s}s")

    # the acts

    @contextlib.asynccontextmanager
    async def _as(self, who: Who) -> AsyncGenerator[tuple[AsyncSession, Actor]]:
        async with row_scope(Task, self.task_id) as session:
            actor = await session.get(Actor, self._people[who])
            assert actor is not None
            yield session, actor

    async def _note(self, did: str, who: Who, answer: str, **more: object) -> None:
        async with row_scope(Task, self.task_id) as session:
            task = await session.get(Task, self.task_id)
            assert task is not None
            self.record.append(
                {"did": did, "by": who.value, "answer": answer, **more, "epoch": task.plan_epoch}
            )

    async def steer(self, text: str, by: Who, saw: int | None) -> None:
        try:
            async with self._as(by) as (session, actor):
                row = await steering.steer(session, actor, self.task_id, text, issued_epoch=saw)
        except StaleSteer as stale:  # the 409: it names what the author had not seen
            missed = [[m["kind"], self._names[str(m["author"])].value] for m in stale.missed]
            await self._note("steer", by, stale.code, missed=missed)
            return
        self._sent.append((by, row.command_id))
        await self._note("steer", by, "accepted")

    async def retract(self, nth: int) -> None:
        by, command_id = self._sent[nth - 1]
        try:
            async with self._as(by) as (session, actor):
                await steering.retract(session, actor, self.task_id, command_id)
        except PrismoidError as refused:  # an apply that won the race settled it first
            await self._note("retract", by, refused.code)
            return
        await self._note("retract", by, "retracted")

    async def status(
        self, did: str, door: Callable[[AsyncSession, Actor, uuid.UUID], Awaitable[str]], by: Who
    ) -> None:
        try:
            async with self._as(by) as (session, actor):
                answer = await door(session, actor, self.task_id)
        except PrismoidError as refused:
            await self._note(did, by, refused.code)
            return
        await self._note(did, by, answer)
