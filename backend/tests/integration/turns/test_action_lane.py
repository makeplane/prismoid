# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An action on a service, pinned at the planning seam: the imperative is the Lead's turn (no plan
before it), the service it names still attaches by intent, and the Lead's operator plan delivers the side
effect, never a document (`contract.output=None`); an unconnected service leaves the Lead its connect ask.

Born from a live failure: "Create a Notion page titled Connector Test..." with Notion connected and mounted
delivered a DOCUMENT titled "Instructions to Create the Notion Page".
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.operations import connectors as connector_ops
from prismoid.operations import planning
from prismoid.operations import tasks as task_ops
from prismoid.operations.turn import intake as turn_intake
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.plan import DeliverableKind, PlanProposal, ProposedStep, StepSpec, TaskStatus
from tests.support.lead_turn import lead_plans

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

MCP = "https://mcp.example.test/mcp"
GOAL = "Create a Notion page titled Connector Test with a two-line hello note in it"


@pytest.fixture
def notion_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(
            tools=(
                ConnectorToolInfo(name="create_page", read_only=False),
                ConnectorToolInfo(name="search", read_only=True),
            ),
            instructions="",
            server_info={},
        )

    monkeypatch.setattr(connector_ops, "probe_server", probe)


_ACTS = PlanProposal(
    steps=[ProposedStep(label="s1", agent="operator", title="Create the page", goal=GOAL)],
    deliverable=DeliverableKind.ACTION,
)


async def test_an_action_on_a_connected_service_is_the_operators_not_a_document(
    db: AsyncSession, actor: Actor, notion_probe: None
) -> None:
    await connector_ops.create_connector(db, actor, server_url=MCP, name="Notion")
    task = await task_ops.create_task(db, actor, goal=GOAL, clarify="never")
    task.transition(TaskStatus.PLANNING)
    task_id = task.id
    assert task.connector_ids, "naming Notion auto-attaches the live row (ADR-054 W8)"
    await db.commit()

    lead = await lead_plans(task_id, _ACTS)
    db.expire_all()
    first = (await TaskEvent.of_types(db, task_id, ("task.planned",)))[0]
    assert isinstance(first, TaskPlanned) and first.route_source == "lead", "the turn is the Lead's"
    [step] = [s for s in await PlanStep.for_task(db, task_id) if s.id != lead]
    spec = StepSpec.model_validate(step.spec)
    assert str(spec.agent) == "operator"
    assert spec.contract is not None and spec.contract.output is None, "the side effect is the deliverable"


async def test_a_named_but_unconnected_service_leaves_the_lead_its_connect_ask(
    db: AsyncSession, actor: Actor
) -> None:
    # No rows at all: nothing attaches, and the Lead may ask the person to connect the service.
    task = await task_ops.create_task(db, actor, goal=GOAL, clarify="auto")
    task.transition(TaskStatus.PLANNING)
    await db.flush()
    view = await planning.draft_plan(db, task.id)
    await db.commit()

    assert task.connector_ids == []
    assert [s.agent for s in view.steps] == ["lead"]
    facts = (await turn_intake.intake(db, task)).facts
    assert LeadEndingKind.ASK in facts.allowed_endings()
