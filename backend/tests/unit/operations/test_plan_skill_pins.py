# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A plan's skills are pinned at the commit, never chosen by a separate router: each step loads the skills
its proposal named at their published head, a USER-dialed kind's format skill rides every step from the task,
and the task's own pins come first. A skill gone since the plan's check is left out, never a failed plan."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.task import Task
from prismoid.errors import NotFound
from prismoid.operations import planning_steps, planning_turns
from prismoid.schemas.contract import Contract, SkillPin
from prismoid.schemas.ids import AgentName, SkillName, StepId
from prismoid.schemas.plan import PlanProposal, ProposedStep, StepKind, StepSpec

NO_SESSION = cast(AsyncSession, None)  # the patched resolver never touches it
HEADS = {"compare": 4, "model": 2, "report": 7}


def _pin(name: str) -> SkillPin:
    return SkillPin(name=SkillName(name), version=HEADS[name])


@pytest.fixture(autouse=True)
def heads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The published heads; each name the resolver is asked for, in order."""
    asked: list[str] = []

    async def resolve_ref(_session: Any, ref: str, **_kw: Any) -> SkillPin:
        asked.append(ref)
        if ref not in HEADS:
            raise NotFound(f"no skill {ref}")
        return _pin(ref)

    monkeypatch.setattr(planning_steps.skill_ops, "resolve_ref", resolve_ref)
    return asked


def _task(**over: Any) -> Task:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "skill_pins": [],
        "mode": "standard",
        "deliverable": None,
        "deliverable_source": None,
        "deliverable_format": None,
    }
    return Task(**(fields | over))


def _step(*skills: str) -> ProposedStep:
    return ProposedStep(label="s1", agent="writer", title="Write", goal="write", skills=list(skills))


async def test_each_skill_a_plan_names_is_pinned_at_its_head_and_a_gone_one_left_out(
    heads: list[str],
) -> None:
    proposal = PlanProposal(steps=[_step("report", "gone"), _step("compare", "report")])
    assert await planning_steps.step_pins(NO_SESSION, proposal) == {
        "compare": _pin("compare"),
        "report": _pin("report"),
    }
    assert sorted(heads) == ["compare", "gone", "report"], "each name asked once"


@pytest.mark.parametrize(
    ("over", "lane", "pinned"),
    [
        ({"deliverable": "spreadsheet", "deliverable_source": "user"}, "standard", ["model"]),
        ({"deliverable": "spreadsheet", "deliverable_source": None}, "standard", ["model"]),
        ({"deliverable": "spreadsheet", "deliverable_source": "plan"}, "standard", []),
        ({"deliverable": "document", "deliverable_source": "user"}, "standard", []),
        ({}, "standard", []),
        ({"deliverable": "spreadsheet", "deliverable_source": "user"}, "express", []),
        ({"deliverable": "spreadsheet", "deliverable_source": "user", "mode": "fleet"}, "standard", []),
        (
            {
                "deliverable": "spreadsheet",
                "deliverable_source": "user",
                "skill_pins": [_pin("report").model_dump()],
            },
            "standard",
            ["report"],
        ),
    ],
    ids=[
        "user dial",
        "unsourced dial",
        "plan stamp",
        "prose",
        "undialed",
        "express",
        "fleet",
        "pinned already",
    ],
)
async def test_only_a_user_dialed_file_kind_pins_its_format_skill(
    over: dict[str, Any], lane: str, pinned: list[str]
) -> None:
    task = _task(**over)
    await planning_turns.pin_dialed_skill(NO_SESSION, task, lane)
    assert [p["name"] for p in task.skill_pins] == pinned


def test_the_tasks_pins_come_first_and_a_steps_own_follow() -> None:
    spec = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        contract=Contract(goal="g"),
        skill_pins=[_pin("compare"), SkillPin(name=SkillName("report"), version=1)],
    )
    task = _task(skill_pins=[_pin("report").model_dump(mode="json")])
    [(_, pinned)] = planning_steps.pin_skills([(StepId(uuid.uuid4()), spec)], task)
    assert pinned.skill_pins == [_pin("report"), _pin("compare")], "the task's version wins its name"
