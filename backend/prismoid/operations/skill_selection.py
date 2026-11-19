# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The routing eval that gates publishing a skill: does the Lead load a skill for the goals it names?

Each case runs the real Lead, built through `build()`, decision-only (`ai/agents/decision`), the way the
bot selection gate does: it reads the case's goal as a person's first turn with the roster, the skills a
step may load and the skills it reads itself, the version under test's description swapped into both, and
stops at its first ending or first non-pure call, before anything acts. The skill it loaded is the one a
plan's step names or the one its reply reads (`skill_read`); a case passes when that is the one it
expects. `skills.apply_eval` records the verdict on the draft, and `skills.publish` 409s anything not green.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Sequence

from pydantic_ai import capture_run_messages
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.ai.agents.decision import Acted, decision_only
from prismoid.ai.context.turn import turn_note
from prismoid.ai.tools.providers.search import search_chain
from prismoid.ai.tools.skills import SKILL_READ, SkillCard
from prismoid.db.models.skill import SkillRecord
from prismoid.operations import skills as skill_ops
from prismoid.operations.bot_selection import machinery_agent
from prismoid.operations.planning_brief import roster_section, skill_index
from prismoid.schemas.lead import LeadFacts, SubmitPlan, TurnOrigin
from prismoid.schemas.skills import ResolvedSkill, RoutingCaseResult, RoutingReport, SkillManifest

# A case is a person's first turn in the app with questions off: the gate asks which skill the work
# loads, and a question would only defer that answer.
_TURN: LeadFacts = LeadFacts(turn=1, origin=TurnOrigin.WEB, clarify="never")


@dataclasses.dataclass(frozen=True)
class Index:
    """The skills a case is decided against: what a plan's step may name (each skill's needed tools and
    description) and the menu the Lead reads from itself."""

    needs: dict[str, tuple[str, ...]]
    described: dict[str, str]
    menu: tuple[SkillCard, ...]


async def test_routing(session: AsyncSession, name: str, *, version: int | None = None) -> RoutingReport:
    """Run a skill version's routing cases on the Lead with this version's description swapped into the
    published heads. Commits the read snapshot before the first model call."""
    target = await skill_ops.resolve(session, name, version=version)
    index = await index_with(session, target)
    await session.commit()
    results: list[RoutingCaseResult] = []
    for case in target.routing_cases:
        chosen = await loaded(session, case.goal, index, expect=str(case.expect))
        results.append(
            RoutingCaseResult(
                goal=case.goal, expected=str(case.expect), chosen=chosen, ok=chosen == case.expect
            )
        )
    return RoutingReport(
        skill=str(name),
        version=target.version,
        passed=sum(1 for r in results if r.ok),
        total=len(results),
        cases=results,
    )


async def gate(session: AsyncSession, name: str, *, version: int | None = None) -> RoutingReport:
    """Run a version's routing cases (`test_routing`) and record the verdict on its draft, which `publish` reads."""
    report = await test_routing(session, name, version=version)
    await skill_ops.apply_eval(session, name, version=report.version, green=report.green)
    return report


async def index_with(session: AsyncSession, target: ResolvedSkill | None = None) -> Index:
    """The published heads as the Lead plans from them, `target`'s version swapped in for its name."""
    heads = {
        r.name: SkillManifest.model_validate(r.manifest or {})
        for r in await SkillRecord.published_heads(session)
    }
    menu = await skill_ops.menu(session, "lead")
    if target is not None:
        heads[str(target.name)] = target.manifest
        menu = tuple(
            dataclasses.replace(card, version=target.version, description=target.manifest.description)
            if card.name == target.name
            else card
            for card in menu
        )
    return Index(
        needs={n: m.allowed_tools for n, m in heads.items()},
        described={n: m.description for n, m in heads.items()},
        menu=menu,
    )


async def loaded(session: AsyncSession, goal: str, index: Index, *, expect: str = "") -> str:
    """The skill the Lead loads for `goal`: `expect` when its plan or its reading names it, else the first
    it names, else "" (it named none, or read or acted before it decided)."""
    runner = await machinery_agent(
        session, "lead", lead_facts=_TURN, skills=index.needs, skill_menu=index.menu
    )
    roster = "".join(roster_section(agents_base.roster(plannable=True), [], searches=bool(search_chain())))
    prompt = (
        f"# The request\n\n{goal}\n\n{roster.strip()}\n\n{skill_index(index.described)}\n{turn_note(_TURN)}"
    )
    ending: object = None
    with capture_run_messages() as messages, decision_only(runner, None, []), contextlib.suppress(Acted):
        ending = (await runner.run(prompt)).output  # an act or a read ends the decision before it runs
    named = [*_planned(ending), *_read(messages)]
    return expect if expect and expect in named else next(iter(named), "")


def _planned(ending: object) -> list[str]:
    """The skills a plan's steps name, in step order."""
    if not isinstance(ending, SubmitPlan):
        return []
    return [skill for step in ending.plan.steps for skill in step.skills]


def _read(messages: Sequence[ModelMessage]) -> list[str]:
    """The skills the run read for itself (`skill_read`), in call order."""
    return [
        str(part.args_as_dict().get("name", ""))
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name == SKILL_READ
    ]
