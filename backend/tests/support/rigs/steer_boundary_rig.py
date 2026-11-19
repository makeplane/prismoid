# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the frontier steer boundary tests (ADR-064), shared by the `test_steer_boundary*`
files: real rows of a turn the Lead planned, and the Lead's replan run stubbed (`turn.replan._run`) by a
stub that commits its session before anything else, exactly as the real run does before its model call.
A plain module, not a test file."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from prismoid import storage
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import live, planning, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.turn import replan as turn_replan
from prismoid.operations.turn.replan import Replanned
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.ids import AgentName, ProjectId, TaskId
from prismoid.schemas.lead import PlanRevision
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanProposal,
    PlanView,
    ProposedStep,
    StepKind,
    StepSpec,
    StepState,
    TaskStatus,
)
from prismoid.schemas.steering import SteerNote
from prismoid.schemas.usage import Unit, UsageRecord
from tests.support.cast import member

STEER = "Only cover German providers"


CHURN = "Analyze our churn and tell me what to fix"  # a doing ask: the Lead plans it


def rig_spec(title: str, agent: str = "writer", lane_role: str | None = None) -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        title=title,
        lane_role=lane_role,  # pyright: ignore[reportArgumentType]
        contract=Contract(
            goal=title,
            acceptance=["it exists"],
            inputs=[],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=title),
        ),
    )


async def project_task(goal: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Ada's Project task with Bob an editor there. Returns (task id, Bob's id)."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"b-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, ada, goal=goal, project_id=project.id, clarify="never")
        await s.commit()
        return task.id, bob.id


async def rig_drafted(
    steps: list[tuple[str, StepState, str | None]],
    edges: list[tuple[str, str]],
    *,
    lane: str | None = None,
    route: str = "standard",
    source: str = "lead",
) -> tuple[uuid.UUID, uuid.UUID]:
    """A running task drafted at epoch 1, its turn routed `route` by `source` (`task.planned`): each step is
    (title, state, the title of the file it published or None), the edges are by title, and `lane`
    is the lane_role of the steps not yet done. Returns (task id, Bob's id)."""
    task_id, bob_id = await project_task("Compare EU cloud providers in a memo")
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.transition(TaskStatus.PLANNING)
        rows: dict[str, PlanStep] = {}
        for ordinal, (title, state, _) in enumerate(steps):
            rows[title] = PlanStep(
                id=uuid.uuid4(),
                task_id=task_id,
                ordinal=ordinal,
                kind=StepKind.STEP.value,
                state=state.value,
                title=title,
                spec=rig_spec(
                    title,
                    "researcher" if title == "research" else "maker",
                    lane_role=None if state is StepState.DONE else lane,
                ).model_dump(mode="json"),
                epoch=task.plan_epoch,
                turn_epoch=task.turn_epoch,
            )
        s.add_all(rows.values())
        await s.flush()
        for upstream, target in edges:
            s.add(PlanEdge(task_id=task_id, from_step_id=rows[upstream].id, to_step_id=rows[target].id))
        for title, _, published in steps:
            if published is None:
                continue
            body = f"# {published}\n".encode()
            digest = storage.put(body)
            s.add(
                Artifact(
                    id=uuid.uuid4(),
                    lineage_id=uuid.uuid4(),
                    task_id=task_id,
                    step_id=rows[title].id,
                    type="document",
                    title=published,
                    sha256=digest,
                    bytes=len(body),
                    storage_key=storage.key_for(digest),
                    provenance={"path": f"/artifacts/{published.lower()}.md"},
                )
            )
        seq = await task.next_seq(s)
        await TaskEvent.append(
            s,
            TaskPlanned(
                seq=seq,
                task_id=TaskId(task_id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                plan=PlanView(),
                route=route,
                route_source=source,
            ),
        )
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        assert task.plan_epoch == 1
    return task_id, bob_id


async def researched(
    *,
    write_done: bool = False,
    write_lane: str | None = None,
    route: str = "standard",
    source: str = "lead",
) -> tuple[uuid.UUID, uuid.UUID]:
    """`research` DONE with its findings published, then `write` (READY, or DONE too) after it."""
    write = StepState.DONE if write_done else StepState.READY
    return await rig_drafted(
        [("research", StepState.DONE, "Findings"), ("write", write, None)],
        [("research", "write")],
        lane=write_lane,
        route=route,
        source=source,
    )


async def rig_steer(actor_id: uuid.UUID, task_id: uuid.UUID, text: str = STEER) -> Steer:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        return await steering.steer(s, actor, task_id, text)


async def _spend(task_id: uuid.UUID) -> None:
    """The Lead's own provider call, recorded on its own session (the real emitter's posture)."""
    async with row_scope(Task, task_id) as own:
        await usage_ops.record(
            own,
            UsageRecord(
                task_id=TaskId(task_id),
                step_id=None,
                provider="local",
                model="m",
                family=Family("qwen"),
                units={Unit.REQUEST: 1},
                cost_usd=Decimal("0.01"),
                price_version=1,
                at=utcnow() + dt.timedelta(seconds=1),
            ),
        )
        await own.commit()


@dataclass(frozen=True, slots=True)
class LeadCall:
    """What one replan run was handed: the steers it answers and the done work's lines."""

    taken: tuple[SteerNote, ...]
    done: tuple[str, ...]


def lead_replans(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[LeadCall],
    *,
    during: Callable[[], Awaitable[object]] | None = None,
    titles: tuple[str, str] = ("outline", "write the memo"),
    fails: bool = False,
) -> None:
    """The stubbed replan run revises the steps not started into `titles[0]` → `titles[1]`; `during` runs
    mid-call, and with `fails` the first call's provider then fails. Called again (an editor's replan inside
    `during`), it revises into `titles`."""

    async def fake_run(session: Any, task: Task, taken: Any, done: Any, unstarted: Any) -> PlanRevision:
        calls.append(LeadCall(tuple(taken), tuple(done.lines)))
        task_id = task.id
        await session.commit()  # the real run releases the snapshot and the row before the model
        first = len(calls) == 1
        if during is not None and first:
            await during()
        if fails and first:
            raise RuntimeError("the provider was down")
        await _spend(task_id)
        steps = [
            ProposedStep(label="s1", agent="researcher", title=titles[0], goal=titles[0]),
            ProposedStep(
                label="s2", agent="writer", title=titles[1], goal=titles[1], depends_on=["s1"], publishes=True
            ),
        ]
        plan = PlanProposal(must=["German only"], steps=steps, deliverable=DeliverableKind.FILES)
        return PlanRevision(say=f"Re-planned - {len(steps)} steps", plan=plan)

    monkeypatch.setattr(turn_replan, "_run", fake_run)


async def exploding(*args: object, **kwargs: object) -> object:
    raise AssertionError("this boundary must not ask the Lead")


def narrated(
    monkeypatch: pytest.MonkeyPatch,
    said: list[tuple[str, str]],
    *,
    on_end: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Record the Lead's re-plan phase facts (the live lane) in order; `on_end` runs as the end is said."""

    def narrator(*args: Any, **kwargs: Any) -> Any:
        async def fact(phase: str, label: str, *, agent: str = "") -> None:
            assert agent == "lead"
            said.append((phase, label))
            if phase == "end" and on_end is not None:
                await on_end()

        return fact

    monkeypatch.setattr(live, "phase_narrator", narrator)


def failing_lead(
    monkeypatch: pytest.MonkeyPatch, *, during: Callable[[], Awaitable[object]] | None = None
) -> None:
    """The replan run commits, `during` runs, then the provider fails."""

    async def failing(session: Any, *args: Any, **kwargs: Any) -> Any:
        await session.commit()
        if during is not None:
            await during()
        raise RuntimeError("the provider was down")

    monkeypatch.setattr(turn_replan, "_run", failing)


async def rig_steps(task_id: uuid.UUID) -> list[PlanStep]:
    async with row_scope(Task, task_id) as s:
        rows = await PlanStep.for_task(s, task_id)
        for row in rows:
            s.expunge(row)
        return rows


async def rig_steers(task_id: uuid.UUID) -> list[Steer]:
    async with row_scope(Task, task_id) as s:
        rows = await Steer.since(s, task_id, 0)
        for row in rows:
            s.expunge(row)
        return rows


async def rig_events[E](task_id: uuid.UUID, kind: type[E]) -> list[E]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, kind)]


async def rig_boundary(task_id: uuid.UUID) -> bool:
    """The boundary on its own unit of work: did the frontier move (read it again)?"""
    async with row_scope(Task, task_id) as s:
        outcome: Replanned = await steering.boundary(s, task_id)
        await s.commit()
    return outcome.moved


async def draft(task_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.transition(TaskStatus.PLANNING)
        await s.commit()
    async with row_scope(Task, task_id) as s:
        await planning.draft_plan(s, task_id)
        await s.commit()
