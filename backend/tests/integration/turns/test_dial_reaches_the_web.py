# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A user's dial on an ask for what the web says now stays the dial's one step, which reads the web itself.

Found live (2026-11-10, a cloud Machine with no search engine): a proposed card's Start set the
spreadsheet dial on "use current on-road prices ... and cite sources", the dial made its one
modeler step, and the modeler, which then held no web tool, filled the workbook with its own
estimates. Every agent a dial seats now holds the web door (and search where an engine is set up),
so the dial keeps its one step and the planner model never runs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import planning
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import TaskPlanned
from prismoid.schemas.plan import PlanProposal, ProposedStep, TaskStatus
from tests.support.cast import member
from tests.support.runners import scripted

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

SOURCED = "A spreadsheet of current on-road prices for three SUVs by monthly distance, and cite sources"
ATTACHED = "Totals by region and month from the attached sales export"
NAVIGATE_SOURCED = "Go to cardekho.com and check the current on-road prices of the XUV 7XO variants"


@pytest.fixture(autouse=True)
def a_cloud_machine_with_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    monkeypatch.setattr(settings.search, "engines", "")
    monkeypatch.setattr(settings.worker, "anthropic_api_key", SecretStr("k"))  # the seat's row builds


def _planner(monkeypatch: pytest.MonkeyPatch, *agents: str) -> list[str]:
    """The planner model drafts `agents` in order, each reading the one before; its briefs are kept."""
    briefs: list[str] = []
    steps = [
        ProposedStep(
            label=f"s{i}",
            agent=agent,
            title=agent,
            goal=f"{agent}'s part",
            depends_on=[f"s{i - 1}"] if i else [],
            publishes=i == len(agents) - 1,
        )
        for i, agent in enumerate(agents)
    ]

    async def run(_agent: object, brief: str, _deps: object) -> object:
        briefs.append(brief)
        return SimpleNamespace(output=PlanProposal(must=["costs per km"], steps=steps))

    monkeypatch.setattr(planning, "build", scripted(run))
    return briefs


async def _dialed(db: AsyncSession, goal: str) -> uuid.UUID:
    actor, _ = await member(db, "Dialer")
    task = await task_ops.create_task(db, actor, goal=goal, clarify="never", deliverable="spreadsheet")
    task.transition(TaskStatus.PLANNING)
    await db.commit()
    return task.id


async def _draft(task_id: uuid.UUID) -> TaskPlanned:
    async with row_scope(Task, task_id) as session:
        await planning.draft_plan(session, task_id)
        await session.commit()
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert isinstance(planned, TaskPlanned)
    return planned


async def test_a_dialed_sourced_spreadsheet_is_one_maker_step(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    briefs = _planner(monkeypatch, "operator", "maker")
    planned = await _draft(await _dialed(db, SOURCED))
    assert (planned.route_source, [str(s.agent) for s in planned.plan.steps]) == ("dial", ["maker"])
    assert briefs == [], "the planner model never ran"


@pytest.mark.parametrize("fmt", [None, "pdf"])
async def test_a_navigate_shaped_sourced_ask_under_a_dial_keeps_the_dialed_kind(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, fmt: str | None
) -> None:
    """The computer door would seat one computer step that publishes no document: the dial takes
    the turn first, on the agent that makes the dialed kind."""
    briefs = _planner(monkeypatch, "operator", "writer")
    actor, _ = await member(db, "Dialer")
    task = await task_ops.create_task(
        db, actor, goal=NAVIGATE_SOURCED, clarify="never", deliverable="document", deliverable_format=fmt
    )
    task.transition(TaskStatus.PLANNING)
    await db.commit()
    agent = "maker" if fmt else "writer"
    planned = await _draft(task.id)
    assert (planned.route_source, [str(s.agent) for s in planned.plan.steps]) == ("dial", [agent])
    assert briefs == []


async def test_a_dial_on_the_users_own_data_stays_one_maker_step(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    briefs = _planner(monkeypatch, "operator", "maker")
    planned = await _draft(await _dialed(db, ATTACHED))
    assert (planned.route_source, [str(s.agent) for s in planned.plan.steps]) == ("dial", ["maker"])
    assert briefs == [], "the planner model never ran"
