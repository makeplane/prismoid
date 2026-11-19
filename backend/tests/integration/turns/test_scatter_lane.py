# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scatter recipe end-to-end minus the models (ADR-050, ADR-092): the Lead's researcher step with a scatter
recipe commits as the map step, the map expands into a READY wave + PENDING synthesis, the tally reads REAL
published blobs from the wave's durable truth, an undercount RE-SCATTERS the gap before synthesis can
promote, and a met target lets synthesis stand on what was gathered.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import orjson
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.rules.budget import BudgetVerdict
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import budget_estimate
from prismoid.operations import swarm as swarm_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanFacts,
    PlanProposal,
    ProposedStep,
    RecipeKind,
    StepRecipe,
    StepSpec,
    StepState,
    TaskStatus,
)
from prismoid.schemas.swarm import RecordSet, ResearchRecord, ScatterResult, SeatBrief
from tests.support.cast import member
from tests.support.lead_turn import lead_plans

pytestmark = pytest.mark.integration

GOAL = "Find at least 8 open-source vector databases; for each give the license and the repo URL."


def _scatter(target: int = 8) -> ScatterResult:
    return ScatterResult(
        objective=GOAL,
        entity="an open-source vector database",
        target_count=target,
        record_fields=["license", "repo"],
        seats=[
            SeatBrief(
                id="g01",
                goal="Sweep the awesome-lists and package registries for vector databases",
                not_here="vendor sites and cloud-only services - g02 owns those",
                expected="a database with a public repo and a stated license",
            ),
            SeatBrief(
                id="g02",
                goal="Sweep vendor sites and comparison articles for open-source engines",
                not_here="package registries and awesome-lists - g01 owns those",
                expected="a database with a public repo and a stated license",
            ),
        ],
    )


def _records(seat: str, names: list[str], *, exhausted: bool = False) -> RecordSet:
    return RecordSet(
        seat=seat,
        records=[
            ResearchRecord(
                entity=n,
                url=f"https://example.com/{n}",
                excerpts=[f"{n} is licensed under Apache-2.0"],
                answer={"license": "Apache-2.0", "repo": f"https://example.com/{n}"},
            )
            for n in names
        ],
        exhausted=exhausted,
    )


async def _publish_fake_records(
    session: AsyncSession, task_id: uuid.UUID, step_row: PlanStep, record_set: RecordSet
) -> None:
    """A seat's harvest, as the tally will find it: real bytes in the real blob store, a
    real dataset-typed artifact row on the step."""
    body = orjson.dumps(record_set.model_dump(mode="json"))
    sha = storage.put(body)
    session.add(
        Artifact(
            id=uuid.uuid4(),
            task_id=task_id,
            step_id=step_row.id,
            lineage_id=uuid.uuid4(),
            revision=1,
            type="dataset",
            title=f"Records - seat {record_set.seat}",
            role="working",
            sha256=sha,
            bytes=len(body),
            storage_key=f"sha256/{sha}",
        )
    )
    await session.flush()


_SWEEP = ProposedStep(
    label="s1",
    agent="researcher",
    title="Sweep for vector databases",
    goal=GOAL,
    recipe=StepRecipe(kind=RecipeKind.SCATTER, target_count=8),
)
_TABLED = ProposedStep(
    label="s2",
    agent="maker",
    title="The table",
    goal="Tabulate every database with its license and repo URL",
    depends_on=["s1"],
    publishes=True,
    produces=ArtifactType.SPREADSHEET,
)
_SWEEPS = PlanProposal(steps=[_SWEEP, _TABLED], deliverable=DeliverableKind.FILES)


async def _mint(plan: PlanProposal = _SWEEPS) -> uuid.UUID:
    """A task whose turn the Lead planned with a scatter recipe step: the plan holds the map step."""
    async with session_scope() as session:
        actor, _ = await member(session, "Sweeper")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal=GOAL, clarify="never")
        task.transition(TaskStatus.PLANNING)
        await session.commit()
        task_id = task.id
    await lead_plans(task_id, plan)
    return task_id


async def _map_step(session: AsyncSession, task_id: uuid.UUID) -> PlanStep:
    """The turn's one map step."""
    [row] = [
        s
        for s in await PlanStep.for_task(session, task_id)
        if StepSpec.model_validate(s.spec).lane_role == "scatter_map"
    ]
    return row


async def _commits_one_map_step(task_id: uuid.UUID) -> None:
    async with session_scope() as session:
        map_row = await _map_step(session, task_id)
        spec, state = StepSpec.model_validate(map_row.spec), map_row.step_state
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert str(spec.agent) == "researcher" and state is StepState.READY
    assert isinstance(planned, TaskPlanned) and planned.route_source == "lead"
    assert not planned.fallback, "the Lead's plan stands, never the floor"


async def test_the_leads_scatter_recipe_commits_as_one_map_step_on_the_researcher() -> None:
    await _commits_one_map_step(await _mint())


async def test_a_lone_scatter_step_is_a_plan_its_synthesis_delivers() -> None:
    """The synthesis the map mints publishes the findings, so a plan of the recipe step alone delivers."""
    await _commits_one_map_step(await _mint(PlanProposal(steps=[_SWEEP], deliverable=DeliverableKind.FILES)))


async def test_the_seats_the_map_mints_are_priced_against_the_task_budget_before_they_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The map's two seats, at the flat $0.25 an unpriced seat costs, need $0.50: a $0.40 task
    parks before the wave, a $1.00 task dispatches it. Only the gather seats are priced, never
    the map or synthesis, and a settled wave projects nothing."""

    async def unpriced(session: AsyncSession) -> PlanFacts:
        return PlanFacts()

    monkeypatch.setattr(budget_estimate, "head_book", unpriced)
    task_id = await _mint()
    async with session_scope() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        map_row = await _map_step(session, task_id)
        await swarm_ops.expand_after_scatter(session, task, map_row, _scatter())
        await session.commit()

    async def project(limit: str) -> BudgetVerdict | None:
        async with session_scope() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            return await budget_estimate.scatter_projection(
                session,
                task,
                Decimal(limit),
                task_spent=Decimal(0),
                project_limit=None,
                project_spent=Decimal(0),
            )

    parked = await project("0.40")
    assert parked is not None and (parked.scope, parked.projected) == ("task", Decimal("0.50"))
    assert await project("1.00") is None

    async with session_scope() as session:
        for s in await PlanStep.for_task(session, task_id):
            if StepSpec.model_validate(s.spec).lane_role == "gather":
                s.state = StepState.DONE.value
        await session.commit()
    assert await project("0.40") is None


async def test_expansion_iteration_and_the_met_target() -> None:
    task_id = await _mint()

    # ── the map lands: the wave + synthesis ───────────────────────────────────────────────
    async with session_scope() as session:
        from prismoid.db.models.task import Task

        task = await session.get(Task, task_id)
        assert task is not None
        map_row = await _map_step(session, task_id)
        added = await swarm_ops.expand_after_scatter(session, task, map_row, _scatter())
        await session.commit()
    assert added == 3  # 2 seats + synthesis

    async with session_scope() as session:
        steps = await PlanStep.for_task(session, task_id)
        by_role: dict[str, list[PlanStep]] = {}
        for s in steps:
            by_role.setdefault(StepSpec.model_validate(s.spec).lane_role or "", []).append(s)
        assert [s.step_state for s in by_role["gather"]] == [StepState.READY, StepState.READY]
        assert {str(StepSpec.model_validate(s.spec).agent) for s in by_role["gather"]} == {"researcher"}
        assert by_role["synthesis"][0].step_state is StepState.PENDING
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
        assert isinstance(planned, TaskPlanned) and planned.route == "scatter"

        # publish the map artifact the tally will re-read (the durable count contract)
        from prismoid.db.models.task import Task

        task = await session.get(Task, task_id)
        body = orjson.dumps(_scatter().model_dump(mode="json"))
        sha = storage.put(body)
        session.add(
            Artifact(
                id=uuid.uuid4(),
                task_id=task_id,
                step_id=by_role["scatter_map"][0].id,
                lineage_id=uuid.uuid4(),
                revision=1,
                type="dataset",
                title="Scatter map - an open-source vector database",
                role="working",
                sha256=sha,
                bytes=len(body),
                storage_key=f"sha256/{sha}",
            )
        )
        await session.commit()

    # ── round 1 settles SHORT: 5/8 unique (one alias dupe) → the gap re-scatters ─────────
    async with session_scope() as session:
        from prismoid.db.models.task import Task

        task = await session.get(Task, task_id)
        assert task is not None
        steps = await PlanStep.for_task(session, task_id)
        gathers = [s for s in steps if StepSpec.model_validate(s.spec).lane_role == "gather"]
        # g01's blob LIES about its identity (it echoes its sibling's id) while declaring
        # exhaustion - the SPEC's stamp must own the accounting: the exhaustion
        # lands on g01's own fence, and g02 stays live for the gap round.
        await _publish_fake_records(
            session, task_id, gathers[0], _records("g02", ["qdrant", "milvus", "weaviate"], exhausted=True)
        )
        await _publish_fake_records(
            session, task_id, gathers[1], _records("g02", ["Qdrant, Inc.", "chroma", "vespa"])
        )
        for g in gathers:
            g.state = StepState.DONE.value
        await session.flush()
        await swarm_ops.after_gather(session, task, gathers[-1])
        await session.commit()

    async with session_scope() as session:
        steps = await PlanStep.for_task(session, task_id)
        round2 = [
            s
            for s in steps
            if (sp := StepSpec.model_validate(s.spec)).lane_role == "gather" and sp.scatter_round == 2
        ]
        assert round2, "an undercount wave must re-scatter the gap (the iteration law)"
        assert all(s.step_state is StepState.READY for s in round2)
        synthesis = next(s for s in steps if StepSpec.model_validate(s.spec).lane_role == "synthesis")
        assert synthesis.step_state is StepState.PENDING, "synthesis waits for the new round"
        spec2 = StepSpec.model_validate(round2[0].spec)
        assert spec2.contract is not None and "never re-submit" in spec2.contract.goal
        assert "qdrant" in spec2.contract.goal, "the exclusion list carries the canonical finds"
        # exhaustion keyed off the SPEC's stamp, never the blob's lying echo:
        # g01 declared its own fence empty, so the gap wave re-briefs g02 only
        assert [StepSpec.model_validate(s.spec).scatter_seat for s in round2] == ["g02"]
        # the gap seat carries the map edge - the mount IS the edge
        map_row = next(s for s in steps if StepSpec.model_validate(s.spec).lane_role == "scatter_map")
        edges = await PlanEdgeRow.for_task(session, task_id)
        assert any(e.from_step_id == map_row.id and e.to_step_id == round2[0].id for e in edges), (
            "a round-2 seat must mount the scatter map exactly as its craft promises"
        )

    # ── round 2 fills the gap: the tally is satisfied, no third wave ─────────────────────
    async with session_scope() as session:
        from prismoid.db.models.task import Task

        task = await session.get(Task, task_id)
        assert task is not None
        steps = await PlanStep.for_task(session, task_id)
        round2 = [
            s
            for s in steps
            if (sp := StepSpec.model_validate(s.spec)).lane_role == "gather" and sp.scatter_round == 2
        ]
        # a round-2 seat carries its FENCE id verbatim (no suffix - run 5's law); the live
        # fence here is g02 (g01's spec-keyed exhaustion held despite its blob's lying echo)
        assert StepSpec.model_validate(round2[0].spec).scatter_seat == "g02"
        await _publish_fake_records(
            session, task_id, round2[0], _records("g02", ["pgvector", "lancedb", "marqo"])
        )
        for g in round2:
            g.state = StepState.DONE.value
        await session.flush()
        await swarm_ops.after_gather(session, task, round2[-1])
        await session.commit()

    async with session_scope() as session:
        steps = await PlanStep.for_task(session, task_id)
        rounds = [
            StepSpec.model_validate(s.spec).scatter_round
            for s in steps
            if StepSpec.model_validate(s.spec).lane_role == "gather"
        ]
        assert max(r or 1 for r in rounds) == 2, "the met target launches no third wave"
