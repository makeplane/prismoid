# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The skill routing eval and the eval-verdict guard, against the registry.

Each routing case is the real Lead, built through `build()` and run decision-only, its model scripted:
the case is green when the Lead's plan names the expected skill on a step, or its reply reads it
(`skill_read`), and red otherwise. The skill index it reads is the published heads with the version under
test's own description swapped in. `apply_eval` records a verdict **only on a draft** - a published
version's green is frozen, earned at publish, and a re-test must not silently downgrade it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.db.models.skill import EvalStatus, SkillRecord, SkillStatus
from prismoid.db.session import session_scope
from prismoid.operations import skill_selection
from prismoid.operations import skills as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration

_GOOD_MD = """---
description: Load when the user wants a routetest thing. Not for spreadsheets or code.
capability: fast-executor
---
Do the routetest thing well.
"""
PLANNED = "make me a routetest file"
READ = "summarise this paragraph for me"
_CASES = "routing:\n  - goal: {planned}\n    expect: {name}\n  - goal: {read}\n    expect: summarize\n"

# How the scripted Lead loads a skill for a goal: on a plan's step, or by reading it for its reply.
Loads = dict[str, tuple[str, str]]


def _lead(loads: Loads) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    """A scripted Lead: a goal in `loads` plans a writer step naming the skill, or reads it; else a plain reply."""

    def decide(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        first = messages[0]
        assert isinstance(first, ModelRequest)
        prompt = next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
        assert ": Load when the user wants a routetest thing" in prompt, "the draft is on the skill index"
        goal = prompt.split("\n")[2]
        how, skill = loads.get(goal, ("reply", ""))
        if how == "read" and len(messages) > 1:  # the skill was read: the reply follows
            return ModelResponse(parts=[TextPart("Here is the summary.")])
        if how == "read":
            return ModelResponse(parts=[ToolCallPart("skill_read", {"name": skill}, "call-1")])
        if how == "plan":
            step = {"label": "s1", "agent": "writer", "title": "Do it", "goal": goal, "publishes": True}
            plan = {"say": "On it.", "plan": {"steps": [{**step, "skills": [skill]}], "deliverable": "files"}}
            return ModelResponse(parts=[ToolCallPart("submit_plan", plan, "call-1")])
        return ModelResponse(parts=[TextPart("Here is the answer.")])

    return decide


async def _an_actor(session: AsyncSession) -> uuid.UUID:
    return (await member(session, "Router"))[0].id


async def _routed(monkeypatch: pytest.MonkeyPatch, name: str, loads: Loads) -> tuple[int, int, list[str]]:
    """Push `name` and run its routing cases on the scripted Lead: passed, total, and the skill each loaded."""
    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(_lead(loads)))
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        await ops.push(
            session,
            actor_id=actor_id,
            name=name,
            skill_md=_GOOD_MD,
            cases_yaml=_CASES.format(planned=PLANNED, name=name, read=READ),
        )
        report = await skill_selection.test_routing(session, name, version=1)
    return report.passed, report.total, [c.chosen for c in report.cases]


async def test_a_skill_the_lead_plans_or_reads_for_its_goals_is_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"routetest-{uuid.uuid4().hex[:6]}"
    passed, total, chosen = await _routed(
        monkeypatch, name, {PLANNED: ("plan", name), READ: ("read", "summarize")}
    )
    assert (passed, total) == (2, 2) and chosen == [name, "summarize"]


async def test_a_skill_the_lead_does_not_load_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"routefail-{uuid.uuid4().hex[:6]}"
    passed, total, chosen = await _routed(monkeypatch, name, {PLANNED: ("plan", "summarize")})
    assert (passed, total) == (0, 2) and chosen == ["summarize", ""]


async def test_apply_eval_records_on_a_draft() -> None:
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        await ops.push(
            session,
            actor_id=actor_id,
            name="routeapply",
            skill_md=_GOOD_MD,
            cases_yaml=_CASES.format(planned=PLANNED, name="routeapply", read=READ),
        )
        await ops.apply_eval(session, "routeapply", version=1, green=True)
        record = await SkillRecord.at_version(session, "routeapply", 1)
        status, evals = record.status, record.eval_status  # pyright: ignore[reportOptionalMemberAccess]

    assert status == SkillStatus.DRAFT.value
    assert evals == EvalStatus.GREEN.value


async def test_apply_eval_leaves_a_published_version_frozen() -> None:
    # report v1 is a published builtin, green. A red routing re-test must not downgrade it.
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        await ops.apply_eval(session, "report", version=1, green=False)
        record = await SkillRecord.at_version(session, "report", 1)
        status, evals = record.status, record.eval_status  # pyright: ignore[reportOptionalMemberAccess]

    assert status == SkillStatus.PUBLISHED.value
    assert evals == EvalStatus.GREEN.value, (
        "a published version's verdict is frozen - re-test must not un-green it"
    )
