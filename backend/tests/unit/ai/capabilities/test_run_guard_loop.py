# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard's loop sense for the browser: a call is the element it acted on, not the reference it
named (every look renumbers the references, `e7.12` then `e8.12` for the same button); a page is what
it shows, not its references' numbering; a page that keeps coming back unchanged earns one soft word
before the brake; and one lone field entry per response on the same page earns one reminder to
chain."""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, Tool, ToolReturn
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

from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.guard import BATCH_REMINDER, PAGE_UNCHANGED
from tests.support.guard import browser_page, watched

_WHERE = "shop.example.com/s"


def _results(epoch: int, *, unmoved: bool = False, page: str = "results") -> ToolReturn[str]:
    """The same results page, as a new look numbers it."""
    return browser_page(page, where=_WHERE, epoch=epoch, unmoved=unmoved)


def _told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


async def _run(
    responses: list[list[tuple[str, dict[str, Any]]]], tools: list[Tool[TaskDeps]], guard: RunGuard
) -> list[str]:
    """One response per request (each a list of calls), then a text ending. What the model was told
    before each request."""
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_told(messages))
        if len(seen) <= len(responses):
            calls = responses[len(seen) - 1]
            return ModelResponse(
                parts=[ToolCallPart(n, a, tool_call_id=f"c{len(seen)}.{i}") for i, (n, a) in enumerate(calls)]
            )
        return ModelResponse(parts=[TextPart("end")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset(tools)],
        capabilities=[guard],
    )
    await agent.run("go", deps=TaskDeps())
    return seen


def _numbered_pages() -> Tool[TaskDeps]:
    """A click that does nothing: every return is the same page under a fresh epoch."""
    epoch = 0

    def browser_click(ref: str) -> ToolReturn[str]:
        nonlocal epoch
        epoch += 1
        return _results(epoch)

    return Tool(browser_click)


async def test_the_same_button_under_a_new_reference_is_the_same_call() -> None:
    guard, runs = watched()
    seen = await _run([[("browser_click", {"ref": f"e{n}.1"})] for n in range(4)], [_numbered_pages()], guard)
    assert runs[0].stalls == 1, "Next, Next, Next on a page that does not move is a stall"
    assert "You are repeating `browser_click` without effect" in seen[4]


async def test_a_page_renumbered_is_the_same_page() -> None:
    """Five clicks on different elements that each bring the same page back under new numbers."""
    guard, runs = watched()
    await _run([[("browser_click", {"ref": f"e{n}.{n + 1}"})] for n in range(5)], [_numbered_pages()], guard)
    assert runs[0].stalls == 1


async def test_the_same_text_into_the_same_field_is_the_same_call_and_another_text_is_not() -> None:
    epoch = 0

    def browser_type(ref: str, text: str) -> ToolReturn[str]:
        nonlocal epoch
        epoch += 1
        return _results(epoch)

    def typed(texts: list[str]) -> list[list[tuple[str, dict[str, Any]]]]:
        return [[("browser_type", {"ref": f"e{n}.2", "text": t})] for n, t in enumerate(texts)]

    same, same_runs = watched()
    other, other_runs = watched()
    await _run(typed(["1tb ssd"] * 4), [Tool(browser_type)], same)
    epoch = 0
    await _run(typed(["1tb ssd", "2tb ssd", "1tb nvme", "1tb sata"]), [Tool(browser_type)], other)
    assert (same_runs[0].stalls, other_runs[0].stalls) == (1, 0)


async def test_a_page_that_keeps_coming_back_unchanged_gets_one_soft_word_before_any_brake() -> None:
    def browser_snapshot(find: str | None = None) -> ToolReturn[str]:
        return _results(1)

    guard, runs = watched()
    looks: list[list[tuple[str, dict[str, Any]]]] = [
        [("browser_snapshot", {})],
        [("browser_snapshot", {"find": "price"})],
        [("browser_snapshot", {"find": "sort"})],
        [("browser_snapshot", {"find": "filter"})],
        [("browser_snapshot", {"find": "Sponsored"})],
    ]
    seen = await _run(looks, [Tool(browser_snapshot)], guard)
    assert PAGE_UNCHANGED not in seen[2] and PAGE_UNCHANGED in seen[3]
    assert seen[-1].count(PAGE_UNCHANGED) == 1, "said once"
    assert runs[0].stalls == 0 and not runs[0].finish_now, "a word, never a brake"


def _entries() -> list[Tool[TaskDeps]]:
    def browser_type(ref: str, text: str) -> ToolReturn[str]:
        return _results(1, page=f"search {text}")

    def browser_select(ref: str, value: str) -> ToolReturn[str]:
        return _results(1, page=f"sorted by {value}")

    def browser_click(ref: str) -> ToolReturn[str]:
        return browser_page("results", where="shop.example.com/results")

    return [Tool(browser_type), Tool(browser_select), Tool(browser_click)]


async def test_lone_entries_on_one_page_are_reminded_to_chain_once() -> None:
    lone: list[list[tuple[str, dict[str, Any]]]] = [
        [("browser_type", {"ref": "e1.2", "text": "ssd"})],
        [("browser_select", {"ref": "e1.3", "value": "price"})],
        [("browser_type", {"ref": "e1.2", "text": "nvme ssd"})],
        [("browser_select", {"ref": "e1.3", "value": "rating"})],
    ]
    seen = await _run(lone, _entries(), RunGuard())
    assert BATCH_REMINDER not in seen[1] and BATCH_REMINDER in seen[2]
    assert seen[-1].count(BATCH_REMINDER) == 1, "once per run"


async def test_chained_entries_and_a_lone_act_on_a_new_page_are_never_reminded() -> None:
    chained: list[list[tuple[str, dict[str, Any]]]] = [
        [
            ("browser_type", {"ref": "e1.2", "text": "ssd"}),
            ("browser_select", {"ref": "e1.3", "value": "price"}),
        ],
        [("browser_type", {"ref": "e1.2", "text": "ssd"})],
        [("browser_click", {"ref": "e1.1"})],
    ]
    seen = await _run(chained, _entries(), RunGuard())
    assert all(BATCH_REMINDER not in s for s in seen)


async def test_a_resumed_run_is_not_reminded_again() -> None:
    lone: list[list[tuple[str, dict[str, Any]]]] = [
        [("browser_type", {"ref": "e1.2", "text": "ssd"})],
        [("browser_select", {"ref": "e1.3", "value": "price"})],
    ]
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_told(messages))
        n = len(seen)
        if n <= len(lone):
            name, args = lone[n - 1][0]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{n}")])
        return ModelResponse(parts=[TextPart("end")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset(_entries())],
        capabilities=[RunGuard()],
    )
    done = await agent.run("go", deps=TaskDeps())
    assert BATCH_REMINDER in seen[-1]

    again: list[str] = []

    def more(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        again.append(_told(messages))
        if len(again) == 1:
            return ModelResponse(
                parts=[ToolCallPart("browser_type", {"ref": "e1.2", "text": "nvme"}, tool_call_id="r1")]
            )
        return ModelResponse(parts=[TextPart("end")])

    resumed = Agent(
        FunctionModel(more),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset(_entries())],
        capabilities=[RunGuard()],
    )
    await resumed.run("go on", deps=TaskDeps(), message_history=done.all_messages())
    assert again[-1].count(BATCH_REMINDER) == 1, "the history already said it"


# ── the probe's `unmoved`, look-alike buttons ───────


async def test_five_acts_the_probe_saw_change_nothing_are_a_stall_whatever_the_page_ticks() -> None:
    """The page hash moves with a clock or a rotating banner; the sidecar's probe still knows the
    act did nothing (`PageState.unmoved`), and rule 2 counts that, where it is known."""
    tick = 0

    def browser_click(ref: str) -> ToolReturn[str]:
        nonlocal tick
        tick += 1
        return _results(1, unmoved=True, page=f"results at {tick}:00")

    guard, runs = watched()
    await _run([[("browser_click", {"ref": f"e1.{n}"})] for n in range(5)], [Tool(browser_click)], guard)
    assert runs[0].stalls == 1


async def test_acts_that_changed_the_page_are_never_counted_by_their_word_alone() -> None:
    tick = 0

    def browser_click(ref: str) -> ToolReturn[str]:
        nonlocal tick
        tick += 1
        return _results(1, page=f"results at {tick}:00")

    guard, runs = watched()
    await _run([[("browser_click", {"ref": f"e1.{n}"})] for n in range(5)], [Tool(browser_click)], guard)
    assert runs[0].stalls == 0


async def test_three_look_alike_buttons_are_three_elements() -> None:
    """Three "Add to cart" buttons on one list are three products, not one button clicked three times."""

    def browser_click(ref: str) -> ToolReturn[str]:
        return _results(1, page="three carts")

    def browser_snapshot(find: str | None = None) -> ToolReturn[str]:
        return _results(1, page="three carts")

    guard, runs = watched()
    clicks = [[("browser_click", {"ref": f"e1.{n}"})] for n in (1, 2, 3)]
    await _run([[("browser_snapshot", {})], *clicks], [Tool(browser_click), Tool(browser_snapshot)], guard)
    assert runs[0].stalls == 0


async def test_a_lone_act_that_changed_nothing_still_reads_where_it_was() -> None:
    """An act the probe saw change nothing still left the page where it was (`PageState.where`)."""

    def browser_type(ref: str, text: str) -> ToolReturn[str]:
        return browser_page(None, where=_WHERE)

    def browser_click(ref: str) -> ToolReturn[str]:
        return browser_page(None, where=_WHERE, unmoved=True)

    lone = [[("browser_type", {"ref": "e1.2", "text": "ssd"})], [("browser_click", {"ref": "e1.1"})]]
    seen = await _run(lone, [Tool(browser_type), Tool(browser_click)], RunGuard())
    assert BATCH_REMINDER in seen[2]
