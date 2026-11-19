# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard's budget brake, driven by a FunctionModel and a fake spend."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests, Tool, ToolOutput, ToolReturn
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

import prismoid.ai.agents.base as agents_base
import prismoid.ai.capabilities.loops as guard_module
from prismoid.ai.agents.base import build
from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.ask import ASK_USER
from prismoid.ai.tools.frame_tools import PUBLISH
from prismoid.conf import settings
from prismoid.errors import ModelIncapable
from prismoid.schemas.guard import FINISH_NOW, STALL_HELD, budget_notice
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import DONE_TOOL, Outcome
from prismoid.schemas.plan import BudgetFigures
from prismoid.schemas.steering import BUDGET_METADATA
from prismoid.schemas.usage import Unit
from tests.support.guard import CopiesTheRequest, browser_page, watched
from tests.support.runners import runner_for


def _at(spent: str, cap: str = "1.00") -> BudgetFigures:
    return BudgetFigures(ceiling_usd=Decimal(cap), spent_usd=Decimal(spent))


@dataclass
class FakeSpend:
    figures: list[BudgetFigures | None]  # one per read; the last one repeats
    broken: bool = False
    reads: int = field(default=0, init=False)

    async def __call__(self) -> BudgetFigures | None:
        self.reads += 1
        if self.broken:
            raise RuntimeError("db blip")
        return self.figures[min(self.reads, len(self.figures)) - 1]


def _told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _agent(
    model: FunctionModel, tools: FunctionToolset[TaskDeps]
) -> Agent[TaskDeps, str | DeferredToolRequests]:
    return Agent(
        model,
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[tools],
        capabilities=[RunGuard(can_park=True)],
    )


def test_the_notice_names_the_highest_mark_crossed_under_the_cap() -> None:
    assert budget_notice(_at("0.40")) is None
    half = budget_notice(_at("0.50"))
    assert half is not None and half.lead == "You have used half of this task's budget"
    assert "($0.50 of $1.00)" in half.text
    late = budget_notice(_at("0.80"))
    assert late is not None and late.lead.startswith("You have used three quarters")
    assert budget_notice(_at("1.00")) is None, "at the cap the guard parks; it does not talk"


def test_a_notice_orders_the_work_and_never_ends_it() -> None:
    """Regression: "finish the essential part now, then finish with `done`" made the
    computer quit at 75% with nothing read, so the cap's gate opened after the step had ended
    and a raise resumed nothing. The cap pauses; the person decides."""
    for spent in ("0.50", "0.80"):
        notice = budget_notice(_at(spent))
        assert notice is not None
        assert "keep working" in notice.text and "never stop early" in notice.text
        assert "done" not in notice.text


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize("copies", [False, True], ids=["alone", "behind-a-hook-that-copies-the-request"])
async def test_the_notices_enter_before_the_request_once_each(copies: bool) -> None:
    ran: list[str] = []

    def ping() -> str:
        ran.append("ping")
        return "pong"

    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_told(messages))
        if len(seen) < 4:
            return ModelResponse(parts=[ToolCallPart("ping", {})])
        return ModelResponse(parts=[TextPart("finished")])

    spend = FakeSpend([_at("0.10"), _at("0.55"), _at("0.60"), _at("0.80")])
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([ping])],
        capabilities=[*([CopiesTheRequest()] if copies else []), RunGuard(can_park=True)],
    )
    result = await agent.run("go", deps=TaskDeps(spend=spend))
    assert result.output == "finished" and len(ran) == 3
    assert "half of this task's budget" not in seen[0]
    assert seen[1].count("half of this task's budget") == 1
    assert seen[2].count("half of this task's budget") == 1, "told once: the history is the memory"
    assert "three quarters of this task's budget" in seen[3]
    # At 80% only the three-quarters notice is due: the half-mark line in this request can only be the
    # one the second request carried, persisted in the history - and it is there exactly once.
    assert seen[3].count("half of this task's budget") == 1, "the notice persisted, never re-injected"
    kept = _told(result.all_messages())
    assert kept.count("half of this task's budget") == 1 and kept.count("three quarters") == 1, (
        "the history keeps each once"
    )


async def test_at_the_cap_the_next_tool_dispatch_parks_marked_budget() -> None:
    ran: list[str] = []

    def post() -> str:
        ran.append("post")
        return "posted"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("post", {}, tool_call_id="p1")])

    result = await _agent(FunctionModel(model), FunctionToolset([post])).run(
        "go", deps=TaskDeps(spend=FakeSpend([_at("1.00")]))
    )
    assert isinstance(result.output, DeferredToolRequests)
    assert [c.tool_call_id for c in result.output.calls] == ["p1"]
    assert result.output.metadata["p1"] == BUDGET_METADATA
    assert ran == [], "the call never ran"


async def test_every_parallel_call_reads_the_spend_at_its_dispatch_even_once_armed() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("post", {}, tool_call_id=f"p{n}") for n in (1, 2)])

    def post() -> str:
        return "posted"

    spend = FakeSpend([_at("1.00")])
    result = await _agent(FunctionModel(model), FunctionToolset([post])).run("go", deps=TaskDeps(spend=spend))
    assert isinstance(result.output, DeferredToolRequests)
    assert spend.reads == 3, "a replay reads once per dispatch; the live run must log as many"


async def test_a_question_parks_as_itself_and_an_ending_ends_normally() -> None:
    asked: list[str] = []

    def ask() -> str:
        asked.append("asked")
        return "answered"

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart(ASK_USER, {})])
        return ModelResponse(parts=[TextPart("stopping here: the budget is spent")])

    tools = FunctionToolset([Tool(ask, name=ASK_USER)])
    result = await _agent(FunctionModel(model), tools).run(
        "go", deps=TaskDeps(spend=FakeSpend([_at("2.00")]))
    )
    assert asked == ["asked"], "the guard never takes a question's place"
    assert result.output == "stopping here: the budget is spent"


async def test_a_spend_that_cannot_be_read_skips_the_safe_point() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("ping", {})])
        return ModelResponse(parts=[TextPart("done")])

    def ping() -> str:
        return "pong"

    spend = FakeSpend([_at("5.00")], broken=True)
    result = await _agent(FunctionModel(model), FunctionToolset([ping])).run("go", deps=TaskDeps(spend=spend))
    assert result.output == "done" and spend.reads == 3  # two requests and the ping's dispatch


async def test_no_spend_closure_no_brake() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("ping", {})])
        return ModelResponse(parts=[TextPart("done")])

    def ping() -> str:
        return "pong"

    assert (
        await _agent(FunctionModel(model), FunctionToolset([ping])).run("go", deps=TaskDeps())
    ).output == "done"


def _catalog() -> list[ModelSpec]:
    return [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=(ModelClass.FAST_EXECUTOR,),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal("0")},
        )
    ]


def test_build_mounts_the_guard_only_on_a_guarded_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agents_base, "resolve", lambda *a, **k: FunctionModel(lambda m, i: ModelResponse(parts=[]))
    )

    def guards(name: str) -> list[RunGuard]:
        agent = build(name, TaskDeps(), _catalog()).agent
        return [c for c in agent.root_capability.capabilities if isinstance(c, RunGuard)]

    (guard,) = guards("operator")
    assert guard.can_park, "the operator asks, so its output type admits the park"
    assert guards("judge") == [], "a single-breath agent has no loop to brake"


async def test_the_same_call_three_times_is_a_stall_and_the_second_stall_says_finish() -> None:
    ran: list[int] = []

    def ping(x: int) -> str:
        ran.append(x)
        return "pong"

    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_told(messages))
        if len(seen) <= 7:
            return ModelResponse(parts=[ToolCallPart("ping", {"x": 1}, tool_call_id=f"p{len(seen)}")])
        return ModelResponse(parts=[TextPart("gave up")])

    guard, runs = watched(can_park=True)
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        capabilities=[guard],
    )
    result = await agent.run("go", deps=TaskDeps())
    assert result.output == "gave up"
    assert "You are repeating `ping` without effect" in seen[3] and "repeating `ping`" not in seen[2]
    assert FINISH_NOW in seen[6] and FINISH_NOW not in seen[5]
    assert len(ran) == 6, "the call after the second stall was held, never run"
    assert runs[0].stalls == 2 and runs[0].finish_now


async def test_five_actions_that_leave_the_page_unchanged_are_a_stall() -> None:
    def browser_click(ref: str) -> ToolReturn[str]:
        return browser_page("the save form")

    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_told(messages))
        if len(seen) <= 5:
            return ModelResponse(parts=[ToolCallPart("browser_click", {"ref": f"e1.{len(seen)}"})])
        return ModelResponse(parts=[TextPart("stuck")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([browser_click])],
        capabilities=[RunGuard()],
    )
    await agent.run("go", deps=TaskDeps())
    assert "You are repeating `browser_click` without effect" in seen[5]
    assert all("repeating" not in s for s in seen[:5])


async def test_actions_that_change_the_page_never_stall() -> None:
    clicks = 0

    def browser_click(ref: str) -> ToolReturn[str]:
        nonlocal clicks
        clicks += 1
        return browser_page(f"row {clicks}")

    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_told(messages))
        if len(seen) <= 6:
            return ModelResponse(parts=[ToolCallPart("browser_click", {"ref": f"e1.{len(seen)}"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([browser_click])],
        capabilities=[RunGuard()],
    )
    await agent.run("go", deps=TaskDeps())
    assert all("repeating" not in s for s in seen)


@pytest.mark.parametrize(("pages", "stalls"), [(["a", "b", "c", "d"], 0), (["a", "a", "a", "a"], 1)])
async def test_looking_again_is_a_repeat_only_while_the_page_stands_still(
    pages: list[str], stalls: int
) -> None:
    shown = iter(pages)

    def browser_snapshot() -> ToolReturn[str]:
        return browser_page(next(shown))

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls <= 4:
            return ModelResponse(parts=[ToolCallPart("browser_snapshot", {})])
        return ModelResponse(parts=[TextPart("done")])

    guard, runs = watched()
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([browser_snapshot])],
        capabilities=[guard],
    )
    await agent.run("go", deps=TaskDeps())
    # Each look's key carries the page the previous look brought back: (none), a, b, c never
    # repeat; (none), a, a, a is the same look at a page that is not moving - the third is a stall.
    assert runs[0].stalls == stalls


async def test_a_guarded_run_stops_at_its_request_ceiling_and_says_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.worker, "request_ceiling", 3)

    def ping(n: int) -> str:
        return "pong"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("ping", {"n": len(messages)})])

    tools: FunctionToolset[TaskDeps] = FunctionToolset([ping])
    guarded = Agent(
        FunctionModel(model), deps_type=TaskDeps, output_type=str, toolsets=[tools], name="operator"
    )
    with pytest.raises(ModelIncapable, match=r"hit its request ceiling .*continue from where you stopped"):
        await runner_for(guarded, TaskDeps()).run("go")


async def test_an_unguarded_run_keeps_todays_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "request_ceiling", 3)

    def ping(n: int) -> str:
        return "pong"

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls <= 4:
            return ModelResponse(parts=[ToolCallPart("ping", {"n": calls})])
        return ModelResponse(parts=[TextPart("memo")])

    unguarded = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        name="unguarded",
    )
    assert (await runner_for(unguarded, TaskDeps()).run("go")).output == "memo", (
        "five requests: under the library's 50"
    )


async def test_a_held_call_journals_under_the_catalog_name() -> None:
    rows: list[tuple[str, bool, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, ok, denied_reason))

    def browser_click(ref: str) -> str:
        return "clicked"  # no page: only rule 1 (the same call) can stall

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls <= 7:
            return ModelResponse(
                parts=[ToolCallPart("browser_click", {"ref": "e1.1"}, tool_call_id=f"c{calls}")]
            )
        return ModelResponse(parts=[TextPart("stopped")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([browser_click])],
        capabilities=[RunGuard()],
    )
    await agent.run("go", deps=TaskDeps(journal_tool=journal))
    # Calls 3 and 6 stall; call 7 is held - journaled as the catalog names the tool, never `browser_click`.
    assert rows == [("browser.click", False, STALL_HELD)]


async def test_a_run_never_inherits_the_stalls_of_an_earlier_run_of_the_same_agent() -> None:
    """Each run of a reused agent keeps its own stall evidence, so one run's finish order never holds the next."""
    ran: list[int] = []

    def ping(x: int) -> str:
        ran.append(x)
        return "pong"

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls in (1, 2, 3, 4, 5, 6, 8):  # six identical calls end the first run told to finish
            return ModelResponse(parts=[ToolCallPart("ping", {"x": 1}, tool_call_id=f"p{calls}")])
        return ModelResponse(parts=[TextPart("end")])

    guard, runs = watched()
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([ping])],
        capabilities=[guard],
    )
    await agent.run("first", deps=TaskDeps())
    assert runs[0].finish_now, "the first run was told to finish"
    await agent.run("second", deps=TaskDeps())
    assert len(ran) == 7, "the second run's call ran: it was not held"
    assert not runs[1].finish_now and not guard.finish_now


async def test_a_run_told_to_finish_can_still_finish_with_done() -> None:
    """Output tools never pass `before_tool_execute`: the guard holds every call but the ending."""

    def ping(x: int) -> str:
        return "pong"

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls <= 7:
            return ModelResponse(parts=[ToolCallPart("ping", {"x": 1}, tool_call_id=f"p{calls}")])
        return ModelResponse(
            parts=[ToolCallPart(DONE_TOOL, {"status": "partial", "summary": "ping never changed anything"})]
        )

    guard, runs = watched()
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=ToolOutput(Outcome, name=DONE_TOOL),
        toolsets=[FunctionToolset([ping])],
        capabilities=[guard],
    )
    result = await agent.run("go", deps=TaskDeps())
    assert runs[0].finish_now and calls == 8, "call 7 was held, call 8 ended the run"
    assert result.output.status == "partial"


def _scripted(calls: list[tuple[str, dict[str, Any]]]) -> FunctionModel:
    """One call per request, in order, then a text ending."""
    asked = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal asked
        asked += 1
        if asked <= len(calls):
            name, args = calls[asked - 1]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{asked}")])
        return ModelResponse(parts=[TextPart("end")])

    return FunctionModel(model)


async def _guarded(
    calls: list[tuple[str, dict[str, Any]]], tools: list[Tool[TaskDeps]], **deps: Any
) -> RunGuard:
    guard, runs = watched()
    agent = Agent(
        _scripted(calls),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset(tools)],
        capabilities=[guard],
    )
    assert (await agent.run("go", deps=TaskDeps(**deps))).output == "end"
    return runs[0]


@pytest.mark.parametrize("other", ["read_file", "web_fetch"])
async def test_page_facts_on_another_tools_return_are_not_the_page(other: str) -> None:
    """Which returns are the browser's is decided by tool name (the browser module's law): another
    tool's return carrying page facts never moves the page a look is keyed on."""

    def browser_snapshot() -> ToolReturn[str]:
        return browser_page("the same users page")

    def read(url: str) -> ToolReturn[str]:
        return browser_page(f"something else entirely: {url}")

    calls: list[tuple[str, dict[str, Any]]] = [("browser_snapshot", {})]
    for n in range(3):
        calls += [(other, {"url": f"https://example.com/{n}"}), ("browser_snapshot", {})]
    guard = await _guarded(calls, [Tool(browser_snapshot), Tool(read, name=other)])
    # The looks keyed on the page the browser last showed: (none), A, A, A - the fourth is a stall.
    assert guard.stalls == 1


async def test_metadata_that_is_not_page_facts_never_fails_the_run() -> None:
    def browser_snapshot() -> ToolReturn[str]:
        return ToolReturn("ok", metadata={"page": ["not", "a", "digest"]})

    guard = await _guarded([("browser_snapshot", {}), ("browser_snapshot", {})], [Tool(browser_snapshot)])
    assert guard.stalls == 0


async def test_a_guard_that_cannot_read_a_return_lets_the_run_go_on(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(tool: str, metadata: object) -> None:
        raise RuntimeError("a return the guard cannot read")

    monkeypatch.setattr(guard_module, "_state_of", boom)

    def browser_click(ref: str) -> ToolReturn[str]:
        return browser_page("x")

    await _guarded([("browser_click", {"ref": "e1.1"})], [Tool(browser_click)])


@pytest.mark.parametrize("finisher", ["browser_handoff", ASK_USER, "request_connector"])
async def test_a_run_told_to_finish_may_still_park(finisher: str) -> None:
    """A hand-off, a question or a connect request ends the run as a park: a way to finish, never held."""
    ran: list[str] = []
    rows: list[str] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append(tool)

    def ping(x: int) -> str:
        return "pong"

    def finish(reason: str) -> str:
        ran.append(finisher)
        return "parked"

    calls: list[tuple[str, dict[str, Any]]] = [("ping", {"x": 1})] * 6 + [(finisher, {"reason": "sign in"})]
    guard = await _guarded(calls, [Tool(ping), Tool(finish, name=finisher)], journal_tool=journal)
    assert guard.finish_now, "two stalls came first"
    assert ran == [finisher] and rows == []


async def test_a_run_told_to_finish_may_still_publish_what_it_built() -> None:
    """A researcher or a coder delivers through `publish`: told to finish, it must still hand its
    work over, or the attempt ends as prose and the retry walks into the same loop."""
    published: list[str] = []

    def ping(x: int) -> str:
        return "pong"

    def publish(path: str) -> None:
        published.append(path)

    done: tuple[str, dict[str, Any]] = (PUBLISH, {"path": "/work/a.md"})
    calls: list[tuple[str, dict[str, Any]]] = [("ping", {"x": 1})] * 6 + [done]
    guard = await _guarded(calls, [Tool(ping), Tool(publish, name=PUBLISH)])
    assert guard.finish_now and published == ["/work/a.md"]


async def test_five_fills_that_leave_the_page_unchanged_are_a_stall() -> None:
    def browser_fill(ref: str, text: str) -> ToolReturn[str]:
        return browser_page("the name form")

    calls = [("browser_fill", {"ref": "e1.1", "text": f"Mary {n}"}) for n in range(5)]
    assert (await _guarded(calls, [Tool(browser_fill)])).stalls == 1


@pytest.mark.parametrize("navigate", ["browser_open", "browser_back"])
async def test_a_new_page_starts_the_run_of_five_again(navigate: str) -> None:
    def browser_click(ref: str) -> ToolReturn[str]:
        return browser_page("the same page")

    def go() -> ToolReturn[str]:
        return browser_page("the same page")

    clicks = [("browser_click", {"ref": f"e1.{n}"}) for n in range(5)]
    calls = [*clicks[:4], (navigate, {}), clicks[4]]
    assert (await _guarded(calls, [Tool(browser_click), Tool(go, name=navigate)])).stalls == 0


async def test_an_action_that_brings_no_page_neither_counts_nor_breaks_the_run() -> None:
    def browser_click(ref: str) -> ToolReturn[str]:
        return browser_page(None if ref == "e1.3" else "the same page")

    calls = [("browser_click", {"ref": f"e1.{n}"}) for n in range(6)]  # e1.3 brings no page
    assert (await _guarded(calls, [Tool(browser_click)])).stalls == 1, "five page-carrying clicks around it"


async def test_scrolling_down_a_page_that_moves_is_progress_not_a_stall() -> None:
    """The same scroll on a page that keeps changing is reading a long page: an act's key carries
    the page it acted on (rule 2 catches the acts that leave the page unchanged)."""
    scrolled = 0

    def browser_scroll(direction: str) -> ToolReturn[str]:
        nonlocal scrolled
        scrolled += 1
        return browser_page(f"rows {scrolled * 10} to {scrolled * 10 + 9}")

    calls: list[tuple[str, dict[str, Any]]] = [("browser_scroll", {"direction": "down"})] * 6
    guard = await _guarded(calls, [Tool(browser_scroll)])
    assert guard.stalls == 0 and not guard.finish_now


async def test_the_same_scroll_on_a_page_that_stands_still_is_still_a_repeat() -> None:
    def browser_scroll(direction: str) -> ToolReturn[str]:
        return browser_page("the bottom of the page")

    calls: list[tuple[str, dict[str, Any]]] = [("browser_scroll", {"direction": "down"})] * 4
    assert (await _guarded(calls, [Tool(browser_scroll)])).stalls == 1


def _edits() -> list[Tool[TaskDeps]]:
    def write_file(path: str, content: str) -> str:
        return f"wrote {path}"

    def shell(command: str) -> str:
        return "1 failed"

    return [Tool(write_file), Tool(shell)]


async def test_running_the_tests_again_after_an_edit_is_not_a_repeat() -> None:
    """Edit, run the tests, edit, run them again: a write_file starts a new window for the same
    call - the command is the same, the code it runs is not."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for n in range(6):
        calls += [("write_file", {"path": "app.py", "content": f"v{n}"}), ("shell", {"command": "./test.sh"})]
    guard = await _guarded(calls, _edits())
    assert guard.stalls == 0 and not guard.finish_now


async def test_the_same_command_with_no_edit_between_is_still_a_repeat() -> None:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("write_file", {"path": "app.py", "content": "v1"}),
        *[("shell", {"command": "./test.sh"})] * 3,
    ]
    assert (await _guarded(calls, _edits())).stalls == 1
