# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's turn on the express rung: its facts, its prompt's tail, its wall and its ending, read (ADR-092).

The Lead runs as the turn's express step, frameless, on the express rung's ladder. Before anything
else the rung says "Thinking", so the person sees a face within the first-delta budget. What the run
reads beside the thread is the facts intake computed, the roster a plan is drawn from (the same
presentation the bot selection gate shows) and the turn note, last. The express wall bounds its deciding:
its web tools stop there, and a reply it is writing gets until a reply's wall. A run cut at that wall ends in
what it had written and a plain line saying it ran out of time (or, on a turn that must be work, the one-step
plan that keeps the turn's kind), never in a document nobody asked for. Its ending goes to the completion
path (`turn/commit.py`), except an ask, which parks the step on the existing question gate, its batch
built as the intake's is (each question's default, the TTL, proceed-with-defaults when nobody answers):
the answer, or the defaults, wake the step, and a fresh Lead run, told it already asked, decides again.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import lead as lead_agent
from prismoid.ai.agents.base import AgentDef, roster
from prismoid.ai.agents.bot_compile import seat_table, seatable
from prismoid.ai.capabilities import lead_check
from prismoid.ai.context.turn import turn_note
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import ClassSettings
from prismoid.ai.gateway.run_cost import with_prices
from prismoid.ai.tools.providers.search import search_price
from prismoid.db.base import utcnow
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.skill import SkillRecord
from prismoid.db.models.task import Task
from prismoid.operations import gates, live, questions
from prismoid.operations.execution import inline
from prismoid.operations.execution.deferrals import AttemptResult, ConnectDeferral, Deferral, Produced
from prismoid.operations.execution.taps import narrate
from prismoid.operations.planning_brief import WebReach, roster_section, skill_index, web_reach, web_road
from prismoid.operations.planning_steps import resolved_bots, turn_ask
from prismoid.operations.turn import intake as turn_intake
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.lead import AskUser, LeadEndingKind, LeadFacts, Reply
from prismoid.schemas.plan import Clarification, GateKind, PlanFacts
from prismoid.schemas.skills import SkillManifest

LEAD: Final = str(lead_agent.LEAD.name)
THINKING: Final = "Thinking"

# The call id a Lead's ask parks under: its run ended in an output, so no tool call waits on the answer.
ASK_CALL: Final = "lead:ask"

# A Lead cut at its wall: the line under what it had written, and, on a turn that must be work, the plan's.
WALL_CUT: Final = (
    "I ran out of time before I finished this answer. Ask me to go on, or to research it as a task."
)
WALL_EMPTY: Final = (
    "I ran out of time before I could answer this. Ask again, or ask me to research it as a task."
)
WALL_WHY: Final = "deciding took longer than its time limit"
WALL_SAY: Final = (
    "This took longer to decide than it should, so one step will take on the whole request directly."
)


@dataclass(frozen=True, slots=True)
class LeadTurn:
    """What one Lead run reads beside the thread: the turn's facts and what it plans from (the roster, the
    skills a step may load, the web's road when no search engine is set up)."""

    facts: LeadFacts
    roster: str
    attached: str = ""  # the turn's attachments the Lead reads, fenced as data


async def think(task: Task) -> None:
    """The first delta, before any read the run needs: the phase voice says the Lead is thinking."""
    project = ProjectId(task.project_id) if task.project_id else None
    voice = live.phase_narrator(TaskId(task.id), project_id=project, after_seq=task.event_seq)
    await voice("start", THINKING, agent=LEAD)


async def brief(
    session: AsyncSession,
    task: Task,
    deps: TaskDeps,
    *,
    asked: bool,
    catalog: list[ModelSpec],
    order: dict[str, list[str]],
    settings: dict[str, ClassSettings],
    reads: bool = False,
) -> LeadTurn:
    """Set the Lead's facts, plan facts and bots on `deps` and read what it plans from; `asked`: this run
    follows the turn's ask; `reads`: it reads the turn's attachments, and what it did not read is unread."""
    intake = await turn_intake.intake(session, task)
    facts = intake.facts
    if asked and deps.step_id is not None:
        await keep_answers(session, task, deps.step_id)
    attached = await inline.lead_files(intake.inputs) if reads else None
    update: dict[str, object] = {"asked": True} if asked else {}
    if attached is not None:
        update["inputs_unread"] = attached.unread
    facts = facts.model_copy(update=update)
    request = (await turn_ask(session, task)).message or task.goal
    deps.lead_facts = facts
    deps.bots = tuple(await resolved_bots(session, task))
    bots = seatable(deps.bots)
    skills = await published_skills(session)
    known = PlanFacts(request=request, skills=skills.needs, seats=seat_table(bots))
    deps.plan_facts = with_prices(known, catalog, search=search_price(), order=order, settings=settings)
    reach = web_reach(task, roster(plannable=True), catalog)
    turn = lead_turn(facts, bots, skills=skills.described, reach=reach)
    return turn if attached is None else replace(turn, attached=attached.section())


async def keep_answers(session: AsyncSession, task: Task, step_id: uuid.UUID) -> None:
    """The Lead's answered ask as the task's answers: what `/task/answers.json` mounts for every step it plans,
    and what a step's same question is answered from. Each question carries its prompt (`gates.answers_for`)."""
    gate = await Gate.latest_for_step(session, step_id)
    if gate is None or gate.kind != GateKind.INPUT.value or not gate.resolution:
        return
    if answers := await gates.answers_for(session, task.id, gate.resolution):
        task.answers = answers


def lead_turn(
    facts: LeadFacts, bots: Sequence[AgentDef], *, skills: Mapping[str, str], reach: WebReach
) -> LeadTurn:
    """The turn's facts and what a plan is drawn from: plannable built-ins, the task's bots, loadable skills
    and, with no search engine, the browser's road to the web."""
    roster_lines = roster_section(roster(plannable=True), bots, searches=reach is WebReach.SEARCH)
    sections = ["".join(roster_lines).strip(), skill_index(skills).strip(), web_road(reach).strip()]
    return LeadTurn(facts=facts, roster="\n\n".join(part for part in sections if part))


@dataclass(frozen=True, slots=True)
class Skills:
    """The published skills a plan's step may name: the tools each declares it needs (`allowed-tools`),
    which the plan check holds a step's seat to, and the description the Lead reads."""

    needs: dict[str, tuple[str, ...]]
    described: dict[str, str]


async def published_skills(session: AsyncSession) -> Skills:
    """Every published skill at its head, as the Lead plans from it."""
    heads = await SkillRecord.published_heads(session)
    manifests = {r.name: SkillManifest.model_validate(r.manifest or {}) for r in heads}
    return Skills(
        needs={name: m.allowed_tools for name, m in manifests.items()},
        described={name: m.description for name, m in manifests.items()},
    )


def tail(turn: LeadTurn, ambient: str) -> str:
    """The prompt's tail: the roster, the ambient facts, the attachments read, then the turn note last."""
    attached = f"{turn.attached.rstrip()}\n\n" if turn.attached else ""
    return f"{turn.roster}\n\n{ambient.rstrip()}\n\n{attached}{turn_note(turn.facts)}"


def walled(turn: LeadTurn, deps: TaskDeps, had: Sequence[ModelMessage]) -> Produced:
    """A Lead cut at its wall: what it had written, and a line saying it ran out of time; on a turn whose
    facts allow no reply but a plan, the one-step plan that keeps the turn's kind, saying why."""
    allowed = turn.facts.allowed_endings()
    if LeadEndingKind.REPLY in allowed or LeadEndingKind.PLAN not in allowed:
        written = partial_reply(had)
        text = f"{written.rstrip()}\n\n{WALL_CUT}" if written.strip() else WALL_EMPTY
        return Produced("", [], Reply(text=text))
    known = deps.plan_facts or PlanFacts()
    return Produced("", [], lead_check.floor(turn.facts, known, WALL_WHY, say=WALL_SAY))


def partial_reply(had: Sequence[ModelMessage]) -> str:
    """The reply text of the run's last response, as far as it was written; empty when that response was
    calling a tool or ending another way."""
    last = next((m for m in reversed(had) if isinstance(m, ModelResponse)), None)
    if last is None or any(isinstance(part, ToolCallPart) for part in last.parts):
        return ""
    return "".join(part.content for part in last.parts if isinstance(part, TextPart))


async def ended(session: AsyncSession, task: Task, step: PlanStep, output: Any) -> AttemptResult:
    """The run's ending as the rung's result: an ask parks on its gate, its reason on the thread first;
    every other ending is produced for the completion path to commit."""
    ending = lead_agent.ending_of(output)
    if not isinstance(ending, AskUser):
        return Produced("", [], ending)
    await narrate(session, task, step, ending.context)  # rides the park's commit
    if ending.kind is GateKind.CONNECT:
        return ConnectDeferral(
            tool_call_id=ASK_CALL, service=ending.service, why=ending.context, told=step.epoch
        )
    # The intake's own builder: each question's default, the TTL and proceed-with-defaults on timeout.
    asked = questions.build(Clarification(context=ending.context, questions=ending.questions), now=utcnow())
    return Deferral(tool_call_id=ASK_CALL, questions=[], asked=asked, told=step.epoch)
