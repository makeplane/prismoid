# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead, offline: its definition and prompt, every ending reached through the real `build()`, an ending
the turn's facts forbid refused inside the run, and the decision runner scoring it on planted rows."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals.metrics import SuiteKind
from evals.runners import decide, run_trials, summary
from evals.runners.decide import Seat
from evals.schema import Outcome, OutcomeRow
from prismoid.ai.agents import grants
from prismoid.ai.agents.base import build, get
from prismoid.ai.agents.bot_compile import seat_table
from prismoid.ai.agents.lead import ending_of
from prismoid.ai.capabilities import lead_check
from prismoid.ai.capabilities.output_checks import NUDGES, OUTPUT_RETRIES
from prismoid.ai.context.turn import turn_note
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.providers import ClassSettings
from prismoid.schemas.lead import (
    AskUser,
    Deliver,
    Fail,
    LeadFacts,
    ProposeTask,
    Reply,
    SubmitPlan,
    TurnOrigin,
)
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanFacts
from tests.support import web_reach
from tests.support.first_request import fake_row, first_request

WEB = LeadFacts(turn=1, origin=TurnOrigin.WEB)
ROUTINE = LeadFacts(turn=1, origin=TurnOrigin.ROUTINE, unattended=True)
DIALLED = LeadFacts(turn=1, origin=TurnOrigin.WEB, deliverable=ArtifactType.DECK)

PLAN = {
    "say": "I'll build the deck now.",
    "plan": {
        "deliverable": "files",
        "steps": [
            {
                "label": "s1",
                "agent": "maker",
                "title": "Deck",
                "goal": "Build a 10-slide deck.",
                "publishes": True,
                "produces": "deck",
            }
        ],
    },
}
ASK = {"context": "Two products share the name.", "questions": [{"prompt": "Which one?", "type": "text"}]}
OFFER = {"answer": "Hire in two waves.", "goal": "A hiring plan for Q1.", "deliverable": "document"}
FAIL = {"kind": "needs_owner", "reason": "The payload names no file."}
DELIVER = {"result": "No new alerts since 09:00.", "findings": False}
LONE_CHOICE = {
    "context": "Two lists.",
    "questions": [{"prompt": "Which?", "type": "single_select", "options": ["a"]}],
}

CALLS: dict[str, dict[str, Any]] = {
    "submit_plan": PLAN,
    "ask_person": ASK,
    "offer_task": OFFER,
    "fail": FAIL,
    "deliver_to_owner": DELIVER,
    "lone_choice": LONE_CHOICE,
}


def scripted(*responses: str) -> FunctionModel:
    """Answers each request in turn: a name in `CALLS` is that output tool's call, anything else is text."""
    queue = list(responses)

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        said = queue.pop(0)
        if said == "lone_choice":
            return ModelResponse(parts=[ToolCallPart("ask_person", LONE_CHOICE)])
        if said == "bad_offer":
            return ModelResponse(parts=[ToolCallPart("offer_task", {"answer": "x"})])
        return ModelResponse(parts=[ToolCallPart(said, CALLS[said]) if said in CALLS else TextPart(said)])

    return FunctionModel(model)


type Output = str | SubmitPlan | AskUser | ProposeTask | Fail | Deliver


async def run_lead(facts: LeadFacts | None, model: FunctionModel) -> tuple[Output, list[ModelMessage]]:
    """One Lead run on `facts`, through `build()`, and the messages it exchanged."""
    deps = TaskDeps(lead_facts=facts, plan_facts=PlanFacts(seats=seat_table(())))
    runner = build("lead", deps, [fake_row(ModelClass.FRONTIER_EXECUTOR)])
    with runner.agent.override(model=model):
        result = await runner.run("go")
    return cast(Output, result.output), result.all_messages()


async def test_the_first_request_offers_text_and_the_four_ending_tools_and_no_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_reach.an_engine(monkeypatch)  # every tool the Lead holds, `web_search` included, is mounted
    seen: dict[str, AgentInfo] = {}

    def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["info"] = info
        return ModelResponse(parts=[TextPart("Hi.")])

    await run_lead(None, FunctionModel(capture))
    info = seen["info"]
    assert info.allow_text_output
    assert sorted(t.name for t in info.output_tools) == [
        "ask_person",
        "decline",
        "deliver_to_owner",
        "fail",
        "offer_task",
        "submit_plan",
    ]
    assert not {"ask_user", "request_connector", "propose_task"} & {t.name for t in info.function_tools}
    sent = await first_request("lead", browser=False, image=False)
    assert sent.tools == grants.held("lead")


def test_the_lead_asks_the_frontier_executor_at_low_effort_where_its_class_thinks_at_medium() -> None:
    catalog = [
        fake_row(c) for c in (ModelClass.FRONTIER_EXECUTOR, ModelClass.FAST_EXECUTOR, ModelClass.DECIDER)
    ]
    medium = {
        c.value: ClassSettings(effort="medium")
        for c in (ModelClass.FRONTIER_EXECUTOR, ModelClass.FAST_EXECUTOR)
    }

    def asked(name: str) -> tuple[str | None, object]:
        runner = build(name, TaskDeps(), catalog, settings=medium)
        model, settings = runner.agent.model, runner.agent.model_settings
        served = model.model_name if isinstance(model, Model) else None
        return served, (settings if isinstance(settings, dict) else {}).get("thinking")

    assert asked("lead") == ("m-frontier-executor", "low")
    assert asked("writer") == ("m-fast-executor", "medium")


def test_the_prompt_is_about_a_thousand_words_in_the_named_blocks() -> None:
    lead = get("lead")
    assert len(lead.prompt.split()) <= 1003
    assert [block.id for block in lead.blocks] == [
        "lead.how-a-turn-ends",
        "lead.reply-whenever-a-message-can-hold-the-result",
        "lead.the-shape-of-a-reply",
        "lead.the-live-world",
        "lead.plan-when-the-outcome-needs-hands",
        "lead.writing-a-plan",
        "lead.ask-only-when-a-guess-would-ruin-the-work",
        "lead.offer-a-task-when-a-file-would-serve-better",
        "lead.language",
        "lead.be-honest",
    ]
    for rule in (
        "a long paste is material",
        "state your assumptions in one line and proceed",
        "latest message's language",
        "every step receives the request and the thread",
        "come from a step running code",
        "`skill_read`",
        "rests on pages a research step reads",
        "a standalone piece past about a page",
        "win over a remembered preference",
        "never sits in a code fence",
    ):
        assert rule in " ".join(lead.prompt.lower().split()), rule


@pytest.mark.parametrize(
    ("facts", "said", "ending"),
    [
        (WEB, "Here is your Slack message.", Reply),
        (WEB, "submit_plan", SubmitPlan),
        (WEB, "ask_person", AskUser),
        (WEB, "offer_task", ProposeTask),
        (ROUTINE, "fail", Fail),
        (ROUTINE, "deliver_to_owner", Deliver),
    ],
)
async def test_each_ending_is_reachable_offline(facts: LeadFacts, said: str, ending: type) -> None:
    output, messages = await run_lead(facts, scripted(said))
    assert isinstance(ending_of(output), ending)
    assert sum(isinstance(m, ModelResponse) for m in messages) == 1


async def test_an_output_tool_beside_text_in_one_response_ends_the_run_with_the_plan() -> None:
    def both(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("On it."), ToolCallPart("submit_plan", PLAN)])

    output, _ = await run_lead(WEB, FunctionModel(both))
    assert isinstance(output, SubmitPlan) and output.say == "I'll build the deck now."


@pytest.mark.parametrize(
    ("facts", "forbidden", "then", "refusal"),
    [
        (DIALLED, "A deck is a set of slides.", "submit_plan", "cannot end with a text reply"),
        (ROUTINE, "ask_person", "fail", "cannot end with `ask_person`"),
        (ROUTINE, "Is the digest for today?", "deliver_to_owner", "cannot end with a text reply"),
        (WEB, "deliver_to_owner", "Hello.", "cannot end with `deliver_to_owner`"),
        (WEB, "fail", "Hello.", "cannot end with `fail`"),
        (LeadFacts(turn=1, origin=TurnOrigin.WEB, clarify="always"), "Hello.", "ask_person", "text reply"),
    ],
)
async def test_an_ending_the_facts_forbid_is_refused_and_the_model_ends_again(
    facts: LeadFacts, forbidden: str, then: str, refusal: str
) -> None:
    output, messages = await run_lead(facts, scripted(forbidden, then))
    assert ending_of(output).ending in facts.allowed_endings()
    told = [str(getattr(p, "content", "")) for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    refused = [said for said in told if "cannot end with" in said]
    assert len(refused) == 1 and refusal in refused[0]


async def test_an_ask_whose_batch_cannot_open_as_typed_is_refused_in_the_run() -> None:
    output, messages = await run_lead(WEB, scripted("lone_choice", "ask_person"))
    assert isinstance(output, AskUser) and output.questions[0].type.value == "text"
    told = [str(getattr(p, "content", "")) for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    assert any("fewer than two options" in said for said in told)


async def test_a_model_that_keeps_a_forbidden_ending_runs_the_dialled_kinds_plan_saying_why() -> None:
    output, _ = await run_lead(DIALLED, scripted(*["Just a reply."] * (NUDGES + 1)))
    assert isinstance(output, SubmitPlan) and output.say == lead_check.ENDING_SAY
    assert output.plan.fallback == lead_check.ENDING_WHY
    (only,) = output.plan.steps
    assert only.agent == "maker" and only.produces is ArtifactType.DECK


async def test_a_forbidden_ending_never_kills_the_run_once_the_shared_pool_is_spent() -> None:
    fumbles = ["bad_offer"] * OUTPUT_RETRIES  # each malformed call spends one retry of the shared pool
    output, _ = await run_lead(DIALLED, scripted(*fumbles, "Just a reply."))
    assert isinstance(output, SubmitPlan) and output.plan.fallback == lead_check.ENDING_WHY


async def test_without_facts_no_ending_is_refused() -> None:
    output, _ = await run_lead(None, scripted("fail"))
    assert isinstance(output, Fail)


def test_the_turn_note_names_the_allowed_endings_and_the_facts_behind_them() -> None:
    assert turn_note(WEB).startswith("# This turn\n\n- this turn may end with a text reply, `submit_plan`")
    routine = turn_note(ROUTINE.model_copy(update={"budget_left_usd": Decimal("1.5")}))
    assert "`ask_person`" not in routine and "`fail`" in routine
    assert "this routine turn runs on its own" in routine and "$1.50" in routine
    dialled = turn_note(DIALLED.model_copy(update={"inputs_unread": 2, "direct_bot": "scout"}))
    assert "a text reply" not in dialled and "a deck" in dialled
    assert "cannot read here: 2" in dialled and "runs as scout" in dialled
    sized = turn_note(WEB.model_copy(update={"size": "standard · ~5 pages"}))
    assert "the person set the size: standard · ~5 pages" in sized and "size" not in turn_note(WEB)
    asked = turn_note(WEB.model_copy(update={"asked": True}))
    assert "`ask_person`" not in asked and "already asked" in asked


# The decision runner on planted rows (E-01): replies and plans, scored as the person would see them.
DECIDES = Seat([fake_row(ModelClass.FRONTIER_EXECUTOR)])
ROWS = (
    OutcomeRow(id="r0", message="Format this for Slack: ship it Friday", expected_outcome=Outcome.REPLY),
    OutcomeRow(id="r1", message="Translate to French: good morning", expected_outcome=Outcome.REPLY),
    OutcomeRow(id="p0", message="Make me a 10-slide deck on Q3", expected_outcome=Outcome.PLAN),
    OutcomeRow(id="a0", message="What does Plane cost?", expected_outcome=Outcome.ASK),
    OutcomeRow(id="o0", message="Give me a hiring plan", expected_outcome=Outcome.PROPOSE),
)
ENDS = {"p0": "submit_plan", "a0": "ask_person", "o0": "offer_task"}


def deciding(ends: dict[str, str]) -> FunctionModel:
    """Ends each row as `ends` says by the row's message, with text for every other; the turn note must close the ask."""
    by_message = {row.message: ends.get(row.id, "") for row in ROWS}

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        first = messages[0]
        assert isinstance(first, ModelRequest)
        prompt = "".join(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
        assert prompt.rstrip().endswith(
            "this turn may end with a text reply, `submit_plan`, `ask_person`, `offer_task`, `decline`"
        )
        said = next(end for message, end in by_message.items() if message in prompt)
        return ModelResponse(
            parts=[ToolCallPart(said, CALLS[said]) if said else TextPart("Done: here it is.")]
        )

    return FunctionModel(model)


async def scored(ends: dict[str, str]) -> summary.SuiteReport:
    trials = await run_trials(ROWS, decide.lead(DECIDES, deciding(ends)), trials=1, concurrency=3)
    return summary.report_of(
        "decide-lead",
        SuiteKind.TALK,
        trials,
        k=1,
        tree="t",
        prompts_sha="p" * 64,
        date=dt.date(2026, 11, 15),
        not_run={},
        judged=decide.decided_well,
    )


async def test_the_decision_runner_scores_the_leads_endings_and_a_planted_swap_moves_the_score() -> None:
    honest = await scored(ENDS)
    assert [(r.row, r.outcome, r.passed) for r in honest.records] == [
        ("r0", Outcome.REPLY, True),
        ("r1", Outcome.REPLY, True),
        ("p0", Outcome.PLAN, True),
        ("a0", Outcome.ASK, True),
        ("o0", Outcome.PROPOSE, True),
    ]
    assert summary.gate_failures(honest, None) == ()
    planted = await scored({**ENDS, "r1": "submit_plan", "p0": ""})
    assert [(r.row, r.outcome, r.passed) for r in planted.records][1:3] == [
        ("r1", Outcome.PLAN, False),
        ("p0", Outcome.REPLY, False),
    ]
    assert "replies sent to work: r1" in summary.gate_failures(planted, None)
