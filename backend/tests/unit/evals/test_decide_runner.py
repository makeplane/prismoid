# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The decision runner, offline: the real Lead built through `build()`, a scripted model, stub tools.

A row's decision is what it scores: a reply planted as a plan moves the score, a read with no fixture
on the row stops the suite, and the gate exits non-zero on anything but a pass.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals import run
from evals.metrics import SuiteKind
from evals.run import SUITES, Suite
from evals.runners import Runner, decide, not_run, plain_loop, run_trials, summary
from evals.runners.decide import DecideRunner, Seat
from evals.schema import Outcome, OutcomeRow
from prismoid.ai.agents.base import roster
from prismoid.ai.context.turn import turn_note
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.providers.search import search_chain
from prismoid.operations.planning_brief import roster_section
from prismoid.schemas.lead import LeadEndingKind, LeadFacts, TurnOrigin
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit

SEAT = Seat(
    [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=(ModelClass.FAST_EXECUTOR, ModelClass.FRONTIER_EXECUTOR),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal("0")},
        )
    ]
)

ROWS = (
    OutcomeRow(id="r0", message="ask r0", expected_outcome=Outcome.REPLY),
    OutcomeRow(id="r1", message="ask r1", expected_outcome=Outcome.REPLY),
    OutcomeRow(id="p0", message="ask p0", expected_outcome=Outcome.PLAN),
)
HONEST = frozenset({"ask p0"})
PLANTED = HONEST | {"ask r1"}  # one reply row swapped to a plan


def _prompt(messages: list[ModelMessage]) -> str:
    first = messages[0]
    assert isinstance(first, ModelRequest)
    return "".join(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))


def deciding(plans: frozenset[str]) -> FunctionModel:
    """Plans the rows whose ask is in `plans`, replies to every other."""

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        ask = next(a for a in ("ask r0", "ask r1", "ask p0") if a in _prompt(messages))
        if ask in plans:
            return ModelResponse(parts=[ToolCallPart("submit_plan", PLANNED)])
        return ModelResponse(parts=[TextPart(f"An answer to {ask}.")])

    return FunctionModel(model)


async def scored(plans: frozenset[str]) -> summary.SuiteReport:
    trials = await run_trials(ROWS, decide.lead(SEAT, deciding(plans)), trials=1, concurrency=3)
    return summary.report_of(
        "decide",
        SuiteKind.TALK,
        trials,
        k=1,
        tree="t",
        prompts_sha="p" * 64,
        date=dt.date(2026, 11, 14),
        not_run={},
        judged=decide.decided_well,
    )


async def test_a_reply_planted_as_a_plan_moves_the_score() -> None:
    honest, planted = await scored(HONEST), await scored(PLANTED)
    assert [(r.row, r.outcome, r.passed) for r in honest.records] == [
        ("r0", Outcome.REPLY, True),
        ("r1", Outcome.REPLY, True),
        ("p0", Outcome.PLAN, True),
    ]
    assert [(r.row, r.outcome, r.passed) for r in planted.records][1] == ("r1", Outcome.PLAN, False)
    assert summary.gate_failures(planted, None) == ("replies sent to work: r1",)
    assert summary.gate_failures(honest, None) == ()


async def test_a_read_answers_with_the_rows_fixture_and_a_row_without_one_is_not_run() -> None:
    seen: list[str] = []

    def reads(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("web_fetch", {"url": "https://example.org/today"})])
        seen.append(str(returned[0].content))
        return ModelResponse(parts=[TextPart("It is sunny.")])

    row = OutcomeRow(
        id="w", message="weather?", expected_outcome=Outcome.LIGHT_WORK, fixtures={"web_fetch": "Sunny, 21C."}
    )
    trace = await decide.lead(SEAT, FunctionModel(reads))(row)
    assert (trace.outcome, [c.tool for c in trace.tool_calls]) == (Outcome.LIGHT_WORK, ["web.fetch"])
    assert seen == ["Sunny, 21C."]

    bare = row.model_copy(update={"fixtures": {}})
    bare = bare.model_copy(update={"id": "bare"})
    trials = await run_trials([bare, row], decide.lead(SEAT, FunctionModel(reads)), trials=1, concurrency=1)
    kept, skipped = not_run(trials)
    assert [t.row.id for t in kept] == ["w"]
    assert skipped == {"bare": "missing fixture: the agent read with web_fetch"}


@pytest.mark.parametrize(
    ("call", "outcome"),
    [
        (ToolCallPart("ask_user", {"questions": [{"question": "Which one?"}]}), Outcome.ASK),
        (
            ToolCallPart("routine_create", {"name": "n", "instruction": "i", "cron": "0 9 * * 1-5"}),
            Outcome.PROPOSE,
        ),
        (ToolCallPart("shell", {"command": "ls"}), Outcome.PLAN_WITH_MACHINE),
        (ToolCallPart("write_file", {"path": "/work/a.md", "content": "a"}), Outcome.PLAN),
    ],
)
async def test_the_plain_loops_first_act_is_its_outcome(call: ToolCallPart, outcome: Outcome) -> None:
    acted: list[str] = []

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        acted.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[call])

    trace = await plain_loop.runner(SEAT, FunctionModel(model))(
        OutcomeRow(id="x", message="go", expected_outcome=outcome)
    )
    assert trace.outcome is outcome
    assert {"shell", "publish", "ask_user", "web_fetch"} <= set(acted), "every tool a bot can hold"


async def test_the_leads_request_carries_the_rungs_roster_and_turn_note() -> None:
    sent: list[str] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        sent.append(_prompt(messages))
        return ModelResponse(parts=[TextPart("An answer.")])

    row = OutcomeRow(id="l", message="ask l", expected_outcome=Outcome.REPLY)
    await decide.lead(SEAT, FunctionModel(model))(row)
    shown = "".join(roster_section(roster(plannable=True), (), searches=bool(search_chain()))).strip()
    assert f"{shown}\n\n# Ambient facts" in sent[0], "the roster the rung shows, before the ambient facts"
    assert sent[0].endswith(f"\n\n{turn_note(decide.attended(row))}"), "the turn note, last"


ATTENDED = LeadFacts(turn=1, origin=TurnOrigin.WEB)
ALONE = LeadFacts(turn=1, origin=TurnOrigin.ROUTINE, unattended=True)
WRITTEN = {"label": "s1", "agent": "writer", "title": "Write", "goal": "Write it.", "publishes": True}
PLANNED = {"say": "On it.", "plan": {"deliverable": "files", "steps": [WRITTEN]}}
# Every ending, as the Lead ends a turn that allows it: the tool, its arguments, the outcome and whether it delivered.
ENDED: dict[LeadEndingKind, tuple[LeadFacts, ToolCallPart | TextPart, Outcome, bool]] = {
    LeadEndingKind.REPLY: (ATTENDED, TextPart("An answer."), Outcome.REPLY, True),
    LeadEndingKind.PLAN: (ATTENDED, ToolCallPart("submit_plan", PLANNED), Outcome.PLAN, True),
    LeadEndingKind.ASK: (
        ATTENDED,
        ToolCallPart(
            "ask_person", {"context": "Which?", "questions": [{"prompt": "Which?", "type": "text"}]}
        ),
        Outcome.ASK,
        False,
    ),
    LeadEndingKind.PROPOSE: (
        ATTENDED,
        ToolCallPart(
            "offer_task", {"answer": "An answer.", "goal": "A sheet.", "deliverable": "spreadsheet"}
        ),
        Outcome.PROPOSE,
        True,
    ),
    LeadEndingKind.FAIL: (
        ALONE,
        ToolCallPart("fail", {"kind": "unavailable", "reason": "The source did not answer."}),
        Outcome.REPLY,
        False,
    ),
    LeadEndingKind.DELIVER: (
        ALONE,
        ToolCallPart("deliver_to_owner", {"result": "Nothing new.", "findings": False}),
        Outcome.REPLY,
        True,
    ),
    LeadEndingKind.DECLINE: (
        ATTENDED,
        ToolCallPart("decline", {"why": "refused", "answer": "I won't help find where she lives."}),
        Outcome.REPLY,
        True,
    ),
}


def test_every_ending_has_a_row() -> None:
    assert set(ENDED) == set(LeadEndingKind)


@pytest.mark.parametrize("kind", list(LeadEndingKind))
async def test_each_lead_ending_maps_to_its_outcome(kind: LeadEndingKind) -> None:
    facts, part, outcome, delivered = ENDED[kind]

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[part])

    runner = DecideRunner(decide.LEAD, SEAT, FunctionModel(model), facts=lambda _row: facts)
    trace = await runner(OutcomeRow(id="e", message="go", expected_outcome=outcome))
    assert (trace.outcome, trace.delivered) == (outcome, delivered)


def arguments(**overrides: object) -> argparse.Namespace:
    given = {"trials": None, "limit": None, "concurrency": None, "dry_run": False, "json": False}
    return argparse.Namespace(
        **(
            given
            | {"baseline": "", "gate": False, "report": ""}
            | {"rows": (), "tag": (), "match": "", "failed_in": "", "sample": None, "seed": None}
            | overrides
        )
    )


@pytest.fixture
def stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `decide` suite over ROWS in tmp_path, on the fake seat, reports written there too."""
    lines = (row.model_dump_json(exclude_defaults=True) for row in ROWS)
    (tmp_path / "rows.jsonl").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(run, "DATASETS", tmp_path)
    monkeypatch.setattr(summary, "REPORTS", tmp_path / "reports")

    async def fake_seat() -> Seat:
        return SEAT

    monkeypatch.setattr(decide, "seat", fake_seat)
    # The gate compares latency between runs: wall time on a busy runner made it read a
    # regression ("regressed: latency"), so the fake runs take no time at all.
    monkeypatch.setattr(decide, "time", SimpleNamespace(monotonic=lambda: 0.0))
    return tmp_path


def registered(monkeypatch: pytest.MonkeyPatch, plans: frozenset[str]) -> None:
    def decides(seat: Seat) -> Runner:
        return decide.lead(seat, deciding(plans))

    monkeypatch.setitem(SUITES, "decide", Suite("rows.jsonl", SuiteKind.TALK, timeout_s=5, decides=decides))


@pytest.mark.parametrize(
    ("plans", "limit", "code", "said"),
    [
        (HONEST, None, 0, ""),
        (PLANTED, None, 1, "GATE FAIL: regressed: broken rows (r1); replies sent to work: r1"),
        (HONEST, 2, 1, "GATE INCONCLUSIVE: inconclusive: baseline rows not run here: p0"),
    ],
)
async def test_the_gate_exits_zero_only_on_a_pass(
    stand: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plans: frozenset[str],
    limit: int | None,
    code: int,
    said: str,
) -> None:
    registered(monkeypatch, HONEST)
    baseline = stand / "base.json"
    assert await run.run_suite("decide", arguments(report=str(baseline))) == 0
    assert json.loads(baseline.read_text())["gate"] == "ungated"
    capsys.readouterr()

    registered(monkeypatch, plans)
    got = await run.run_suite("decide", arguments(limit=limit, baseline=str(baseline), gate=True))
    assert got == code
    assert said in capsys.readouterr().err
