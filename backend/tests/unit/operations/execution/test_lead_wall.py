# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Lead cut at its wall ends in what it had written and a plain line saying it ran out of time; only a turn
whose facts allow no reply but a plan runs the one-step plan, which keeps the turn's dialed kind."""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

from prismoid.ai.agents.bot_compile import seat_table
from prismoid.ai.deps import TaskDeps
from prismoid.operations.execution import lead_rung
from prismoid.schemas.lead import LeadFacts, Reply, SubmitPlan, TurnOrigin
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanFacts

WEB = LeadFacts(turn=1, origin=TurnOrigin.WEB)
ASKED = [ModelRequest(parts=[UserPromptPart("the three vector databases, compared")])]
SEARCHED = ModelResponse(parts=[ToolCallPart("web_search", {"queries": ["vector databases"]})])
WRITING = ModelResponse(parts=[TextPart("Pinecone is hosted; Qdrant and Weaviate")], state="interrupted")


def _walled(facts: LeadFacts, *had: ModelRequest | ModelResponse) -> object:
    deps = TaskDeps(plan_facts=PlanFacts(request="compare them", seats=seat_table(())))
    turn = lead_rung.LeadTurn(facts=facts, roster="")
    return lead_rung.walled(turn, deps, [*ASKED, *had]).outline


def test_a_reply_cut_mid_sentence_keeps_what_it_wrote_and_says_so() -> None:
    ending = _walled(WEB, SEARCHED, WRITING)
    assert isinstance(ending, Reply)
    assert ending.text.startswith("Pinecone is hosted; Qdrant and Weaviate")
    assert ending.text.endswith(lead_rung.WALL_CUT)


def test_a_run_cut_while_searching_says_it_ran_out_of_time_and_plans_nothing() -> None:
    ending = _walled(WEB, SEARCHED)
    assert ending == Reply(text=lead_rung.WALL_EMPTY)


def test_a_dialed_kind_runs_the_one_step_plan_of_that_kind_saying_why() -> None:
    dialed = WEB.model_copy(update={"deliverable": ArtifactType.SPREADSHEET})
    ending = _walled(dialed, SEARCHED)
    assert isinstance(ending, SubmitPlan) and ending.say == lead_rung.WALL_SAY
    assert ending.plan.fallback == lead_rung.WALL_WHY
    (only,) = ending.plan.steps
    assert only.agent == "maker" and only.produces is ArtifactType.SPREADSHEET


def test_a_turn_that_may_only_ask_hears_it_ran_out_of_time() -> None:
    ask_first = WEB.model_copy(update={"clarify": "always"})
    assert _walled(ask_first, SEARCHED) == Reply(text=lead_rung.WALL_EMPTY)
