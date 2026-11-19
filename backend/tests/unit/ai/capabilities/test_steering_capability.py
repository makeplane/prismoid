# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The safe points (ADR-064), driven by a FunctionModel and a fake inbox."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic_ai import Agent, DeferredToolRequests, Tool
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
from pydantic_ai.toolsets import FunctionToolset

import prismoid.ai.agents.base as agents_base
import prismoid.ai.tools.connectors as connectors
from prismoid.ai.agents.base import build
from prismoid.ai.capabilities.steering import Steering
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.layout import FramePaths
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.ask import ASK_USER, with_ask_user
from prismoid.schemas.connectors import ConnectorSpec, ToolPolicy
from prismoid.schemas.ids import AgentName, ConnectorId, StepId, TaskId
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.steering import PAUSE_METADATA, SafePoint, SteerNote, SteerPending
from prismoid.schemas.usage import Unit
from tests.support.guard import CopiesTheRequest


@dataclass
class FakeInbox:
    told: int = 1
    park_pause: bool = True
    paused: bool = False
    queue: list[SteerNote] = field(default_factory=list)
    applied_at: list[tuple[tuple[int, ...], SafePoint]] = field(default_factory=list)
    paused_at: list[SafePoint] = field(default_factory=list)
    broken: bool = False
    applied_broken: bool = False

    def steer(self, author: str, text: str, *, by_creator: bool = False) -> None:
        epoch = max([self.told, *(s.epoch for s in self.queue)]) + 1
        self.queue.append(
            SteerNote(command_id=uuid.uuid4(), epoch=epoch, author=author, text=text, by_creator=by_creator)
        )

    async def pending(self, after_epoch: int) -> SteerPending:
        if self.broken:
            raise RuntimeError("db blip")
        return SteerPending(paused=self.paused, steers=tuple(s for s in self.queue if s.epoch > after_epoch))

    async def applied(self, steers: Sequence[SteerNote], point: SafePoint) -> None:
        if self.applied_broken:
            raise RuntimeError("db blip")
        self.applied_at.append((tuple(s.epoch for s in steers), point))

    async def paused_here(self, point: SafePoint) -> None:
        self.paused_at.append(point)


def _user_texts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]


def _agent(
    model: FunctionModel,
    inbox: FakeInbox,
    toolset: FunctionToolset[TaskDeps] | None = None,
    *,
    can_park: bool = False,
) -> Agent[TaskDeps, str | DeferredToolRequests]:
    return Agent(
        model,
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[toolset] if toolset else [],
        capabilities=[Steering(inbox, can_park=can_park)],
    )


async def test_a_steer_is_injected_before_the_next_model_request_and_persists() -> None:
    inbox = FakeInbox()
    seen: list[list[str]] = []

    def read() -> str:
        inbox.steer("Bob", "use euros")  # arrives while the tool runs: the tool still finishes
        return "file contents"

    tools: FunctionToolset[TaskDeps] = FunctionToolset([read])

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_user_texts(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("read", {})])
        return ModelResponse(parts=[TextPart("done in euros")])

    result = await _agent(FunctionModel(model), inbox, tools).run("go", deps=TaskDeps())
    assert any("**Bob steered** while you worked: use euros" in t for t in seen[1])
    assert any("**Bob steered** while you worked" in t for t in _user_texts(result.all_messages()))
    assert any(
        isinstance(p, ToolReturnPart) and p.content == "file contents"
        for m in result.all_messages()
        for p in m.parts
    )
    assert inbox.applied_at == [((2,), SafePoint.BEFORE_MODEL)] and inbox.told == 2


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize("copies", [False, True], ids=["alone", "behind-a-hook-that-copies-the-request"])
async def test_a_steer_is_sent_once_and_the_history_keeps_it(copies: bool) -> None:
    inbox = FakeInbox()
    seen: list[list[str]] = []

    def read() -> str:
        if len(seen) == 1:
            inbox.steer("Bob", "use euros")
        return "file contents"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_user_texts(messages))
        if len(seen) < 3:
            return ModelResponse(parts=[ToolCallPart("read", {})])
        return ModelResponse(parts=[TextPart("done in euros")])

    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([read])],
        capabilities=[*([CopiesTheRequest()] if copies else []), Steering(inbox)],
    )
    result = await agent.run("go", deps=TaskDeps())
    steered = "**Bob steered** while you worked: use euros"
    assert [sum(steered in t for t in texts) for texts in seen] == [0, 1, 1], "sent once, then carried"
    assert sum(steered in t for t in _user_texts(result.all_messages())) == 1, "the history keeps it once"


@pytest.mark.parametrize(
    ("tool", "journaled"), [("read_file", "frame.read_file"), ("browser_click", "browser.click")]
)
async def test_a_held_call_journals_under_the_tools_catalog_name(tool: str, journaled: str) -> None:
    inbox = FakeInbox()
    rows: list[tuple[str, bool]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, ok))

    def held() -> str:
        return "ran"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not inbox.queue:
            inbox.steer("Bob", "stop")
            return ModelResponse(parts=[ToolCallPart(tool, {})])
        return ModelResponse(parts=[TextPart("stopped")])

    toolset = FunctionToolset([Tool(held, name=tool)])
    await _agent(FunctionModel(model), inbox, toolset).run("go", deps=TaskDeps(journal_tool=journal))
    assert rows == [(journaled, False)], "one tool, one name in the task's journal"


async def test_a_call_chosen_before_the_steer_is_held_and_spends_no_retry() -> None:
    inbox = FakeInbox()
    ran: list[str] = []

    def post() -> str:
        ran.append("post")
        return "posted"

    tools: FunctionToolset[TaskDeps] = FunctionToolset([post], max_retries=0)

    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            inbox.steer("Bob", "don't post")  # arrives while the model chose the call
            return ModelResponse(parts=[ToolCallPart("post", {})])
        return ModelResponse(parts=[TextPart("not posted")])

    result = await _agent(FunctionModel(model), inbox, tools).run("go", deps=TaskDeps())
    assert ran == [], "a held call never runs"
    held = [p for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)]
    assert (
        held[0].content
        == "Held, not run: Bob steered after you chose this call. Re-issue it if it still fits."
    )
    assert inbox.applied_at[0] == ((2,), SafePoint.BEFORE_TOOL)
    assert result.output == "not posted"


async def test_a_run_about_to_finish_takes_the_steer_first() -> None:
    inbox = FakeInbox()
    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            inbox.steer("Bob", "use euros")
            return ModelResponse(parts=[TextPart("$100")])
        assert any("**Bob steered** before you finished: use euros" in t for t in _user_texts(messages))
        return ModelResponse(parts=[TextPart("€92")])

    result = await _agent(FunctionModel(model), inbox).run("go", deps=TaskDeps())
    assert result.output == "€92" and calls == 2
    assert inbox.applied_at == [((2,), SafePoint.FINAL_ANSWER)]


@dataclass
class _DeviceFrame:
    paths: FramePaths = field(default_factory=lambda: FramePaths(work="/W", on_host=True))


async def test_a_steer_names_the_folder_the_way_a_device_agent_knows_it() -> None:
    """A device agent's shell knows real paths only. A frame with the standard roots keeps every
    byte (`test_steering_copy`)."""
    inbox = FakeInbox()
    seen: list[list[str]] = []

    def read() -> str:
        inbox.steer("Bob", "use euros")
        return "file contents"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_user_texts(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("read", {})])
        return ModelResponse(parts=[TextPart("done")])

    frame: Any = _DeviceFrame()
    deps = TaskDeps(frame=frame)
    await _agent(FunctionModel(model), inbox, FunctionToolset[TaskDeps]([read])).run("go", deps=deps)
    [told] = [t for t in seen[-1] if "steered" in t]
    assert "The work in /W stays" in told and "/work" not in told


async def test_review_focus_two_steers_between_safe_points_land_together() -> None:
    inbox = FakeInbox()
    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            inbox.steer("Bob", "use euros")
            inbox.steer("Ada", "add a chart")
            return ModelResponse(parts=[TextPart("draft")])
        text = "\n".join(_user_texts(messages))
        assert text.index("Bob steered") < text.index("Ada steered")
        return ModelResponse(parts=[TextPart("final")])

    await _agent(FunctionModel(model), inbox).run("go", deps=TaskDeps())
    assert inbox.applied_at == [((2, 3), SafePoint.FINAL_ANSWER)]


async def test_a_redirected_final_answer_streams_as_a_new_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The revised answer is its own message (I1): the answer said before the steer ends with its
    terminal, a new message starts, and the second's text never carries the first's - the web
    shows only the newest, never the two glued together. The real `build()` wires the redirect."""
    inbox = FakeInbox()
    sink: list[Any] = []

    async def emit(event: Any) -> None:
        sink.append(event)

    calls = 0

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            inbox.steer("Bob", "use euros")
            yield "The total is "
            yield "$100."
        else:
            yield "The total is €92."

    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))
    deps = TaskDeps(
        task_id=TaskId(uuid.uuid4()), step_id=StepId(uuid.uuid4()), emit_event=emit, steering=inbox
    )

    result = await build("lead", deps, _fast_catalog()).run("go")

    assert result.output == "The total is €92." and calls == 2
    lane = [e for e in sink if e.type.startswith("message.")]
    assert [e.type for e in lane if e.type != "message.delta"] == [
        "message.start",
        "message.end",
        "message.start",
        "message.end",
    ], "the first message ends before the revised one starts"
    second_start = [i for i, e in enumerate(lane) if e.type == "message.start"][1]
    first = "".join(e.text for e in lane[:second_start] if e.type == "message.delta")
    second = "".join(e.text for e in lane[second_start:] if e.type == "message.delta")
    assert first == "The total is $100."
    assert second == "The total is €92." and first not in second
    assert inbox.applied_at == [((2,), SafePoint.FINAL_ANSWER)]


@pytest.mark.parametrize("can_park", [True, False])
async def test_a_pause_defers_the_next_call_only_when_the_run_can_park(can_park: bool) -> None:
    inbox = FakeInbox(paused=True)
    ran: list[str] = []

    def write() -> str:
        ran.append("write")
        return "ok"

    tools: FunctionToolset[TaskDeps] = FunctionToolset([write])

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("write", {})])

    result = await _agent(FunctionModel(model), inbox, tools, can_park=can_park).run("go", deps=TaskDeps())
    if can_park:
        assert isinstance(result.output, DeferredToolRequests) and ran == []
        (call,) = result.output.calls
        assert result.output.metadata[call.tool_call_id] == PAUSE_METADATA
        assert inbox.paused_at == [SafePoint.BEFORE_TOOL]
    else:
        assert ran == ["write"] and result.output == "done", "a run that cannot park pauses at the frontier"


async def test_a_pause_never_defers_a_question_in_place_of_its_own_park() -> None:
    inbox = FakeInbox(paused=True)
    tools: FunctionToolset[object] = with_ask_user(FunctionToolset())
    batch = {"questions": [{"question": "Which currency?", "options": ["eur", "usd"]}]}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(ASK_USER, batch)])

    result = await _agent(FunctionModel(model), inbox, tools, can_park=True).run("go", deps=TaskDeps())
    assert isinstance(result.output, DeferredToolRequests)
    (call,) = result.output.calls
    assert result.output.metadata[call.tool_call_id]["kind"] == ASK_USER, "the question parks as a question"
    assert inbox.paused_at == []


async def test_an_inbox_failure_skips_the_safe_point_and_never_fails_the_run() -> None:
    inbox = FakeInbox(broken=True)
    result = await _agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("fine")])), inbox).run(
        "go", deps=TaskDeps()
    )
    assert result.output == "fine"


async def test_a_failed_applied_write_still_sends_the_steer_and_never_fails_the_run() -> None:
    inbox = FakeInbox(applied_broken=True)
    seen: list[list[str]] = []

    def read() -> str:
        inbox.steer("Bob", "use euros")
        return "file contents"

    tools: FunctionToolset[TaskDeps] = FunctionToolset([read])

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_user_texts(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("read", {})])
        return ModelResponse(parts=[TextPart("done in euros")])

    result = await _agent(FunctionModel(model), inbox, tools).run("go", deps=TaskDeps())
    assert result.output == "done in euros"
    assert any("**Bob steered** while you worked: use euros" in t for t in seen[1])
    assert inbox.told == 2, "the steer was told; the record is operations' to reconcile"


def _fast_catalog() -> list[ModelSpec]:
    return [
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


def _steering_of(name: str, deps: TaskDeps) -> list[Steering]:
    agent = build(name, deps, _fast_catalog()).agent
    return [c for c in agent.root_capability.capabilities if isinstance(c, Steering)]


def test_build_mounts_steering_only_with_an_inbox_and_parks_only_an_asking_agent() -> None:
    inbox = FakeInbox()
    assert _steering_of("maker", TaskDeps()) == [], "no inbox, no safe points"
    (asking,) = _steering_of("maker", TaskDeps(steering=inbox))
    (plain,) = _steering_of("lead", TaskDeps(steering=inbox))
    assert asking.inbox is inbox and asking.can_park and not plain.can_park


# ── a teammate's steer borrows the creator's connectors ─────────────
# The real `build()`, the real `Steering` and the real connector toolset (hook and filter) over an
# in-process MCP server; only the model and the inbox are scripted. The mount decides how a run
# starts; these pin what a steer entering an already-mounted run does to the creator's connector.

GMAIL = ConnectorSpec(
    id=ConnectorId(uuid.uuid4()),
    slug="gmail",
    name="Gmail",
    url="https://mcp.example.test/gmail",
    tool_names=("search",),
    tool_policies={"search": ToolPolicy.ALLOW},  # a read the creator's dial allows
)


def _gmail(monkeypatch: pytest.MonkeyPatch, steer: Callable[[], None] | None = None) -> list[str]:
    """The creator's Gmail, served in-process: the queries that ran server-side. `steer` lands
    while the first one runs."""
    server = FastMCP("gmail")
    ran: list[str] = []

    def search(q: str) -> str:
        ran.append(q)
        if steer is not None and len(ran) == 1:
            steer()
        return f"the {q} thread"

    server.tool(annotations=ToolAnnotations(readOnlyHint=True))(search)  # a read: seats keep reads
    monkeypatch.setattr(connectors, "StreamableHttpTransport", lambda url, headers=None, **_: server)
    return ran


def _search(q: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("gmail_search", {"q": q})])


async def _run_with_gmail(
    monkeypatch: pytest.MonkeyPatch, agent: str, inbox: FakeInbox, model: Callable[..., ModelResponse]
) -> Any:
    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(model))
    deps = TaskDeps(connectors=(GMAIL,), steering=inbox)
    return await build(agent, deps, _fast_catalog()).run("write the memo")


@pytest.mark.parametrize("by_creator", [False, True])
async def test_a_teammates_steer_mid_run_puts_the_creators_reads_on_ask(
    monkeypatch: pytest.MonkeyPatch, by_creator: bool
) -> None:
    """The steer lands while a read runs and the next request carries it. After a teammate's
    words the read the model chooses parks for the creator's approval and never runs; after her
    own it runs - her connection, her word."""
    inbox = FakeInbox()
    ran = _gmail(monkeypatch, lambda: inbox.steer("Bob", "quote the salary thread", by_creator=by_creator))
    requests: list[list[str]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(_user_texts(messages))
        if len(requests) == 1:
            return _search("board")
        if len(requests) == 2:
            return _search("salary")
        return ModelResponse(parts=[TextPart("memo")])

    result = await _run_with_gmail(monkeypatch, "maker", inbox, model)

    assert any("**Bob steered** while you worked: quote the salary thread" in t for t in requests[1])
    if by_creator:
        assert ran == ["board", "salary"] and result.output == "memo"
        return
    assert ran == ["board"], "the read chosen after a teammate's steer never ran"
    assert isinstance(result.output, DeferredToolRequests)
    (asked,) = result.output.approvals
    assert asked.tool_name == "gmail_search"
    assert result.output.metadata[asked.tool_call_id]["connector"] == "gmail"


async def test_a_redirected_answer_reaching_for_the_creators_connector_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steer lands as the run is about to end: the redirect is the safe point that tells it, so
    the read the revised answer reaches for asks too."""
    inbox = FakeInbox()
    ran = _gmail(monkeypatch)
    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            inbox.steer("Bob", "quote the salary thread")
            return ModelResponse(parts=[TextPart("memo")])
        return _search("salary") if calls == 2 else ModelResponse(parts=[TextPart("memo, quoted")])

    result = await _run_with_gmail(monkeypatch, "maker", inbox, model)

    assert ran == [] and isinstance(result.output, DeferredToolRequests)
    assert [c.tool_name for c in result.output.approvals] == ["gmail_search"]


async def test_a_run_that_cannot_ask_refuses_the_creators_connector_after_a_teammates_steer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent that holds connectors and cannot park on an approval (a bot that may not ask):
    after Bob's steer its read is refused - the model is told why and goes on without it - never
    run on the creator's dial."""
    held = dataclasses.replace(agents_base.get("lead"), connectors="all")
    monkeypatch.setitem(agents_base.REGISTRY, AgentName("lead"), held)
    inbox = FakeInbox()
    ran = _gmail(monkeypatch, lambda: inbox.steer("Bob", "quote the salary thread"))
    told: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        told[:] = [
            str(p.content)
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and "asks its owner" in str(p.content)
        ]
        if len(ran) == 0:
            return _search("board")
        if not told and len(ran) == 1:
            return _search("salary")
        return ModelResponse(parts=[TextPart("memo without the thread")])

    result = await _run_with_gmail(monkeypatch, "lead", inbox, model)

    assert ran == ["board"], "the read after Bob's steer never ran"
    assert result.output == "memo without the thread"
    assert any("asks its owner" in r for r in told), told
