# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Browser work, pinned at the planning seam: an imperative aimed at a site is the Lead's turn (no plan
before it), and the Lead's operator plan opens the hosts the user named without asking (`allow_domains`), never
a host a live connector covers, nor a host only the model named; a follow-up that names no URL keeps the
thread's host, and the turn opens no intake round before the Lead.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.jobs import effects
from prismoid.operations import connectors as connector_ops
from prismoid.operations import execution
from prismoid.operations import tasks as task_ops
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanProposal,
    ProposedStep,
    StepSpec,
    StepState,
    TaskStatus,
)
from tests.support.lead_turn import lead_plans

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

GOAL = "Open https://admin.example.test/orders and update the three open records"
FOLLOW_UP = "Click Logout and tell me what the page says"


def _operates(goal: str) -> PlanProposal:
    return PlanProposal(
        steps=[
            ProposedStep(label="s1", agent="operator", title="Do it on the site", goal=goal, browser=True)
        ],
        deliverable=DeliverableKind.ACTION,
    )


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


async def _lead_planned(db: AsyncSession, actor: Actor, goal: str) -> tuple[uuid.UUID, StepSpec]:
    """Turn 1 of `goal`, planned by the Lead as one operator step: the task's id and that step's spec."""
    task = await task_ops.create_task(db, actor, goal=goal, clarify="never")
    task.transition(TaskStatus.PLANNING)
    task_id = task.id
    await db.commit()
    return task_id, await _operator_of(db, task_id, goal)


async def _operator_of(db: AsyncSession, task_id: uuid.UUID, goal: str) -> StepSpec:
    lead = await lead_plans(task_id, _operates(goal))
    db.expire_all()
    rows = await PlanStep.for_task(db, task_id)
    assert StepSpec.model_validate(rows[-2].spec).agent == "lead" and rows[-2].id == lead
    first = (await TaskEvent.of_types(db, task_id, ("task.planned",)))[0]
    assert isinstance(first, TaskPlanned) and first.route_source == "lead", "the turn is the Lead's"
    return StepSpec.model_validate(rows[-1].spec)


async def test_a_browser_ask_is_the_leads_and_its_operator_opens_the_named_host(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    _, spec = await _lead_planned(db, actor, GOAL)
    assert str(spec.agent) == "operator" and spec.browser
    assert spec.contract is not None
    assert spec.contract.allow_domains == ["admin.example.test"], "the first open never asks"
    assert spec.contract.output is None


async def test_a_host_a_live_connector_covers_is_never_the_browsers_to_open(
    db: AsyncSession,
    actor: Actor,
    monkeypatch: pytest.MonkeyPatch,
    notion_probe: None,
) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    # Named so its own vocabulary never collides with the ask's words: this pins host coverage.
    await connector_ops.create_connector(
        db, actor, server_url="https://admin.example.test/mcp", name="Ops Console"
    )
    _, spec = await _lead_planned(db, actor, GOAL)
    assert spec.contract is not None and spec.contract.allow_domains == [], "the connector's host is its own"


async def test_a_follow_up_with_no_url_keeps_the_threads_host(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follow-up to browser work names no URL: the Lead's turn, and its operator still opens the host the
    thread's goal named, without asking."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    task_id, _ = await _lead_planned(db, actor, GOAL)
    await db.refresh(actor)  # the plan landed in its own sessions: re-read what this one holds
    for row in await PlanStep.for_task(db, task_id):
        row.state = StepState.DONE.value
    await db.flush()
    await execution.advance(db, task_id, TaskStatus.RUNNING)
    await execution.deliver(db, task_id, "Done.")
    await task_ops.continue_task(db, actor, task_id, goal=FOLLOW_UP, clarify="never")
    await db.commit()

    spec = await _operator_of(db, task_id, FOLLOW_UP)
    assert str(spec.agent) == "operator"
    assert spec.contract is not None and spec.contract.allow_domains == ["admin.example.test"]


# A new turn never asks a door about a named format or talk between browser turns: the Lead's file plans are the
# `plan.file` rows of `evals/cases/lead_parity.py`, and the follow-up host is pinned above.


async def test_a_host_only_the_lead_names_is_never_opened_without_asking(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's hosts are not trusted: a host absent from the person's words (a page's injection) is not
    pre-approved egress, whatever the step asks for."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    task = await task_ops.create_task(db, actor, goal=GOAL, clarify="never")
    task.transition(TaskStatus.PLANNING)
    task_id = task.id
    await db.commit()
    step = ProposedStep(
        label="s1",
        agent="operator",
        title="Do it on the site",
        goal=GOAL,
        hosts=["evil.example.com", "admin.example.test"],
    )
    await lead_plans(task_id, PlanProposal(steps=[step], deliverable=DeliverableKind.ACTION))
    db.expire_all()
    spec = StepSpec.model_validate((await PlanStep.for_task(db, task_id))[-1].spec)
    assert str(spec.agent) == "operator"
    assert spec.contract is not None and spec.contract.allow_domains == ["admin.example.test"]


async def test_only_the_browser_step_opens_the_named_host_a_connector_does_not_cover(
    db: AsyncSession,
    actor: Actor,
    monkeypatch: pytest.MonkeyPatch,
    notion_probe: None,
) -> None:
    """A goal that names two sites and a file: the Lead's browser step opens the site the person typed
    without asking; the writer after it opens nothing, and the host a live connector covers stays its own."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    await connector_ops.create_connector(
        db, actor, server_url="https://admin.example.test/mcp", name="Ops Console"
    )
    goal = "Open books.toscrape.com and admin.example.test and save the Travel books as travel_books.csv"
    task = await task_ops.create_task(db, actor, goal=goal, clarify="never")
    task.transition(TaskStatus.PLANNING)
    task_id = task.id
    await db.commit()
    plan = PlanProposal(
        steps=[
            ProposedStep(
                label="s1", agent="operator", title="Collect", goal="collect the Travel books", browser=True
            ),
            ProposedStep(
                label="s2",
                agent="writer",
                title="Note",
                goal="write a note",
                depends_on=["s1"],
                publishes=True,
            ),
        ],
        deliverable=DeliverableKind.FILES,
    )
    await lead_plans(task_id, plan)
    db.expire_all()
    specs = [StepSpec.model_validate(r.spec) for r in await PlanStep.for_task(db, task_id)]
    domains = {str(s.agent): s.contract.allow_domains for s in specs if s.contract is not None}
    assert domains["operator"] == ["books.toscrape.com"]
    assert domains["writer"] == []


@pytest.mark.parametrize("computer", [True, False])
async def test_computer_work_at_a_named_site_starts_without_an_intake_round(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch, computer: bool
) -> None:
    """An intake question once asked "should I sign in with the credentials you gave?" before a login turn. The
    turn is the Lead's, which asks for itself, so `run_task` opens no intake round before it."""
    monkeypatch.setattr(settings.computer_use, "enabled", computer)
    login = "Log in to the-internet.herokuapp.com/login with username tomsmith and password hunter2"
    task = await task_ops.create_task(db, actor, goal=login, clarify="auto")
    task_id = task.id
    await db.commit()
    assert await effects.turn_is_express(str(task_id)) is True
