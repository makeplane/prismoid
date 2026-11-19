# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The selection eval that gates publishing a bot: does the Lead hand a bot's own goals to it?

Each case runs the real Lead, built through `build()`, decision-only (`ai/agents/decision`): it reads
the case's goal as a person's first turn with the roster it plans from, the version under test
swapped in, and stops at its first ending or first non-pure call, before anything acts. A case
passes when the plan seats the bot, or, for a `none` case (a near-miss), when no step does.
`bots.apply_eval` records the verdict on the draft, and `bots.publish` 409s anything not green.
`machinery_agent` is the one way the bot studio's frameless, taskless seats are built.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.ai.agents.bot_compile import BotCompileError, compile_bot, seat_table, seatable
from prismoid.ai.agents.decision import Acted, decision_only
from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.context.turn import turn_note
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.providers.search import search_chain
from prismoid.ai.tools.skills import SkillCard
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.project import Role
from prismoid.errors import NotFound, ValidationFailed
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.bot_scope import authorize, require_scorable
from prismoid.operations.bots import roster_for_task, to_resolved
from prismoid.operations.planning_brief import roster_section
from prismoid.schemas.bots import NO_BOT, BotCase, ResolvedBot, SelectionCaseResult, SelectionReport
from prismoid.schemas.lead import LeadFacts, SubmitPlan, TurnOrigin
from prismoid.schemas.plan import PlanFacts

# A case is a person's first turn in the app with questions off: the gate asks where the work goes,
# and a question would only defer that answer.
_TURN: LeadFacts = LeadFacts(turn=1, origin=TurnOrigin.WEB, clarify="never")


@dataclass(frozen=True)
class Scored:
    """A bot version under test: its name and version, its cases, and the roster it competes in."""

    bot: str
    version: int
    cases: tuple[BotCase, ...]
    roster: tuple[ResolvedBot, ...]


async def machinery_agent(
    session: AsyncSession,
    seat: str,
    *,
    bots: tuple[ResolvedBot, ...] = (),
    lead_facts: LeadFacts | None = None,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    skill_menu: tuple[SkillCard, ...] = (),
) -> AgentRunner:
    """A frameless, taskless seat: spend records with a null `task_id` and commits with the provider call,
    so a refused draft keeps its UsageEvent. Commits nothing itself."""
    deps = TaskDeps(
        task_id=None,
        emit_usage=usage_ops.emitter(session, own_transaction=True),
        bots=bots,
        lead_facts=lead_facts,
        plan_facts=PlanFacts(seats=seat_table(seatable(bots)), skills=dict(skills or {})),
        skill_menu=skill_menu,
    )
    catalog, order = await model_ops.routed_catalog(session)
    return agents_base.build(
        seat, deps, catalog, order=order, settings=await routing_ops.settings_for(session)
    )


async def test_selection(
    session: AsyncSession,
    actor: Actor,
    name: str,
    *,
    version: int | None = None,
    project_id: uuid.UUID | None = None,
) -> SelectionReport:
    """Run a bot version's selection cases on the Lead against its author's roster; a draft scores as edited.
    Commits the read snapshot before the first model call."""
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    record = await _version_under_test(session, actor, name, version=version, project_id=project_id)
    require_scorable(name, record.manifest or {})
    heads = await roster_for_task(session, actor, project_id=project_id)
    under_test = Scored(
        bot=record.name,
        version=record.version,
        cases=tuple(
            BotCase(goal=str(c["goal"]), expect=str(c.get("expect", record.name)))
            for c in (record.manifest or {}).get("cases", [])
        ),
        roster=(*(b for b in heads if b.name != record.name), to_resolved(record)),
    )
    await session.commit()
    return await score(session, under_test)


async def score(session: AsyncSession, under_test: Scored) -> SelectionReport:
    """Each case through the Lead, decision-only; one model call per case, in case order."""
    roster = _compiled(under_test)
    results = [
        _scored(under_test.bot, case, await _seated(session, case.goal, under_test, roster))
        for case in under_test.cases
    ]
    return SelectionReport(
        bot=under_test.bot,
        version=under_test.version,
        passed=sum(1 for r in results if r.ok),
        total=len(results),
        cases=tuple(results),
    )


async def _seated(session: AsyncSession, goal: str, under_test: Scored, roster: str) -> str:
    """The agent the Lead seats for `goal`: the bot when a step names it, else the first step's agent,
    else `none` (it replied, offered, or read or acted before it planned)."""
    runner = await machinery_agent(session, "lead", bots=under_test.roster, lead_facts=_TURN)
    with decision_only(runner, None, []):
        try:
            ending = (await runner.run(f"# The request\n\n{goal}\n\n{roster}\n\n{turn_note(_TURN)}")).output
        except Acted:
            return NO_BOT
    if not isinstance(ending, SubmitPlan):
        return NO_BOT
    agents = [step.agent for step in ending.plan.steps]
    return under_test.bot if under_test.bot in agents else agents[0]


def _compiled(under_test: Scored) -> str:
    """The roster as the Lead reads it; a neighbour that cannot compile is left out, the bot under test refuses."""
    bots = []
    for bot in under_test.roster:
        try:
            bots.append(compile_bot(bot))
        except BotCompileError as bad:
            if bot.name == under_test.bot:
                raise ValidationFailed(f"{bot.name} cannot run yet: {bad}") from bad
            logfire.warn("bot {ref} left out of the selection roster: {why}", ref=bot.ref(), why=str(bad))
    builtins = agents_base.roster(plannable=True)
    return "\n".join(roster_section(builtins, bots, searches=bool(search_chain())))


def _scored(bot: str, case: BotCase, chosen: str) -> SelectionCaseResult:
    """A positive case passes when the bot is seated; a near-miss (`none`) passes unless it is."""
    ok = (chosen == bot) if case.expect != NO_BOT else (chosen != bot)
    return SelectionCaseResult(goal=case.goal, expected=case.expect, chosen=chosen, ok=ok)


async def _version_under_test(
    session: AsyncSession, actor: Actor, name: str, *, version: int | None, project_id: uuid.UUID | None
) -> BotRecord:
    """The named version, else the caller's latest draft, else the published head; a 404 if none."""
    record = (
        await BotRecord.at_version(session, name, version, project_id=project_id, owner_actor_id=actor.id)
        if version is not None
        else await BotRecord.latest_draft(session, name, actor.id, project_id=project_id)
        or await BotRecord.head(session, name, project_id=project_id, owner_actor_id=actor.id)
    )
    if record is None:
        raise NotFound(f"no such bot version: {name}@{version or 'draft'}")
    return record
