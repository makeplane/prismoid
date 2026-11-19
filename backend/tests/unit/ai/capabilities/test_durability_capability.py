# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log as a run's durable unit (ADR-089), in process: a scripted five-request step on a real
`AgentRunner` and `PrismoidDurability`, over the in-memory call log (`tests/support/rigs/durability_drill.py`).
A death is the world's `Killed`, raised by the scripted provider; the drills count what the provider
served and what the tools did, never the engine's internals."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, RunContext, ToolApproved
from pydantic_ai.exceptions import ApprovalRequired, UserError
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.base import build
from prismoid.ai.agents.run import AgentRunner, Resume, TapSlot
from prismoid.ai.capabilities.durability import (
    LOG_VERSION,
    CallLogScope,
    PrismoidDurability,
    ReplayDiverged,
    active,
    request_fingerprint,
)
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.ids import StepId
from tests.support.durability_pins import full_catalog
from tests.support.rigs.durability_drill import REQUESTS, Killed, MemoryLog, World, notes_and_looks
from tests.support.runners import PLAIN

STEP = StepId(uuid.uuid4())


def _toolset(world: World, toolset_id: str = "drill") -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset(id=toolset_id)

    def note(text: str) -> str:
        """Write a note (changes the world)."""
        world.effects.append(f"note:{text}")
        return f"noted {text}"

    def look(query: str) -> str:
        """Read the world."""
        return f"saw {query} with {len(world.effects)} notes"

    toolset.add_function(note)
    toolset.add_function(look)
    return toolset


def _runner(
    world: World, *, events: list[str] | None = None, model: Any = None, toolset_id: str = "drill"
) -> AgentRunner:
    """A runner the way `build()` makes one: durability mounted, the run's events through it."""

    async def handler(ctx: RunContext[TaskDeps], stream: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in stream:
            if events is not None:
                events.append(type(event).__name__)

    agent = Agent(
        model or world.model(),
        name="drill",
        deps_type=TaskDeps,
        toolsets=[_toolset(world, toolset_id)],
        output_type=[str, DeferredToolRequests],
        capabilities=[PrismoidDurability(event_stream_handler=handler)],
    )
    return AgentRunner(agent, TaskDeps(step_id=STEP), PLAIN, live=TapSlot())


async def _attempt(log: MemoryLog, world: World, *, fresh: bool = False, version: int = LOG_VERSION) -> Any:
    scope = await CallLogScope.open(log, STEP, fresh=fresh, version=version)
    return await _runner(world).run("write the report", log=scope)


async def _baseline() -> tuple[Any, World]:
    world = World(notes_and_looks())
    return await _attempt(MemoryLog(), world), world


async def test_outside_a_scope_the_capability_is_inert() -> None:
    world, log = World(notes_and_looks()), MemoryLog()
    result = await _runner(world).run("write the report")
    assert result.output == f"Done after {REQUESTS} requests."
    assert world.provider_calls == REQUESTS and log.writes == 0


@pytest.mark.parametrize("k", range(1, REQUESTS))
async def test_a_death_after_request_k_rebuys_nothing(k: int) -> None:
    """Drill D1 in process: the re-entered attempt replays every recorded unit and goes live after."""
    base, base_world = await _baseline()
    world, log = World(notes_and_looks(), die_before=k + 1), MemoryLog()
    with pytest.raises(Killed):
        await _attempt(log, world)
    world.die_before = None
    result = await _attempt(log, world)
    assert world.provider_calls == REQUESTS, "no request is bought twice"
    assert world.effects == base_world.effects, "no tool call runs twice"
    assert result.output == base.output


async def test_the_request_in_flight_at_the_death_is_bought_again() -> None:
    """Drill D1b: the window the log cannot close. The provider answered request 3 and the process
    died before its row was written, so exactly that one request is bought a second time."""
    world, log = World(notes_and_looks(), die_inside=3), MemoryLog()
    with pytest.raises(Killed):
        await _attempt(log, world)
    world.die_inside = None
    await _attempt(log, world)
    assert world.provider_calls == REQUESTS + 1


async def test_a_tool_writes_its_intent_before_its_body_and_its_result_after() -> None:
    world, log = World(notes_and_looks(), die_before=2), MemoryLog()
    with pytest.raises(Killed):
        await _attempt(log, world)
    units = log.units(STEP)
    tools = sorted(unit for unit, phase in units if phase == "intent" and unit != "segment#1")
    assert len(tools) == 2 and all("@" in unit for unit in tools), "keyed by the call's id"
    assert all((unit, "result") in units for unit in tools)
    assert units[(tools[0], "intent")].payload in (
        {"tool": "look", "args": {"query": "x"}},
        {"tool": "note", "args": {"text": "a"}},
    )
    assert [unit for unit, phase in units if phase == "result" and "model.request" in unit] == [
        "drill__model.request_stream#0"
    ]


async def test_an_unpinned_rename_runs_a_logged_write_again() -> None:
    """Drill D7, the hazard the pins exist for: the same step, its toolset renamed across a death,
    misses the logged write and makes it a second time (`test_operation_name_pins.py` bites first)."""
    world, log = World(notes_and_looks(), die_before=2), MemoryLog()
    with pytest.raises(Killed):
        await _attempt(log, world)
    world.die_before = None
    scope = await CallLogScope.open(log, STEP, fresh=False)
    await _runner(world, toolset_id="drill-renamed").run("write the report", log=scope)
    assert world.effects.count("note:a") == 2


async def test_lane_two_is_quiet_while_the_run_replays() -> None:
    world, log = World(notes_and_looks(), die_before=4), MemoryLog()
    with pytest.raises(Killed):
        await _attempt(log, world)
    events: list[str] = []
    world.die_before = None
    scope = await CallLogScope.open(log, STEP, fresh=False)
    await _runner(world, events=events).run("write the report", log=scope)
    assert events.count("PartStartEvent") == 2, "only the two live requests (4 and 5) stream"
    assert events.count("FunctionToolResultEvent") == 1, "only the live tool call reports"


async def test_a_fresh_attempt_opens_the_next_generation() -> None:
    log, world = MemoryLog(), World(notes_and_looks())
    await _attempt(log, world)
    await _attempt(log, world, fresh=True)
    assert world.provider_calls == 2 * REQUESTS
    assert len(log.units(STEP, 1)) == len(log.units(STEP, 0)) + 1, "the same units, and its opening row"


async def test_a_death_before_a_fresh_generations_first_call_never_replays_the_one_before() -> None:
    """The generation a fresh attempt opens is the latest at once: a re-entry after a death inside
    its first request starts over live, and never replays the attempt before it."""
    log, world = MemoryLog(), World(notes_and_looks())
    await _attempt(log, world)
    world.die_inside = 1
    with pytest.raises(Killed):
        await _attempt(log, world, fresh=True)
    world.die_inside = None
    await _attempt(log, world)
    assert world.provider_calls == 2 * REQUESTS + 1, "the second attempt ran live, its first request twice"
    assert len(world.effects) == 2 * 3


async def test_a_changed_request_fences_with_a_note_naming_the_unfinished_call() -> None:
    """A regenerated request that is not the recorded one is never replayed against: the run starts
    the next generation, told which call never reported an outcome."""
    log, world = MemoryLog(), World(notes_and_looks(), die_before=3)
    with pytest.raises(Killed):
        await _attempt(log, world)
    (unit, _), entry = next((k, v) for k, v in log.units(STEP).items() if k[1] == "result" and "note" in k[0])
    del log.rows[(STEP, 0)][(unit, "result")]  # the call started and never reported
    opening = log.rows[(STEP, 0)][("segment#1", "intent")]
    changed = {**cast(dict[str, Any], opening.payload), "line": "a different prompt"}
    log.rows[(STEP, 0)][("segment#1", "intent")] = replace(opening, payload=changed)
    prompts: list[str] = []

    async def seen(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        request = messages[0]
        assert isinstance(request, ModelRequest)
        prompts.append("".join(str(p.content) for p in request.parts if isinstance(p, UserPromptPart)))
        yield "checked first"

    scope = await CallLogScope.open(log, STEP, fresh=False)
    result = await _runner(world, model=FunctionModel(stream_function=seen)).run(
        "write the report", log=scope
    )
    assert result.output == "checked first" and scope.key.generation == 1
    assert prompts[0].startswith("a different prompt"), "the fenced attempt keeps its logged prompt"
    assert "note(" in prompts[0] and "never reported an outcome" in prompts[0]
    assert entry.payload is not None


async def test_another_log_version_fences_and_the_note_survives_a_later_death() -> None:
    """Drill D6: a generation recorded under another version is not replayed. The fenced generation
    keeps its note, so a death after the fence still tells the next re-entry."""
    log, world = MemoryLog(), World(notes_and_looks(), die_before=3)
    with pytest.raises(Killed):
        await _attempt(log, world)
    (unit, _) = next(k for k in log.units(STEP) if k[1] == "result" and "note" in k[0])
    del log.rows[(STEP, 0)][(unit, "result")]
    fenced = await CallLogScope.open(log, STEP, fresh=False, version=LOG_VERSION + 1)
    assert fenced.key.generation == 1 and fenced.fence_note is not None
    assert '"text":"a"' in fenced.fence_note or '"text":"b"' in fenced.fence_note
    reopened = await CallLogScope.open(log, STEP, fresh=False, version=LOG_VERSION + 1)
    assert reopened.key.generation == 1 and reopened.fence_note == fenced.fence_note


async def test_a_diverged_replay_raises_inside_the_scope() -> None:
    log, world = MemoryLog(), World(notes_and_looks(), die_before=2)
    with pytest.raises(Killed):
        await _attempt(log, world)
    world.die_before = None
    scope = await CallLogScope.open(log, STEP, fresh=False)
    agent = _runner(world).agent
    with active(scope), pytest.raises(ReplayDiverged):
        await agent.run("another prompt", deps=TaskDeps(step_id=STEP))


async def test_a_runtime_toolset_is_refused_inside_a_scope() -> None:
    scope = await CallLogScope.open(MemoryLog(), STEP, fresh=False)
    agent = _runner(World(notes_and_looks())).agent
    with active(scope), pytest.raises(UserError):
        await agent.run("go", deps=TaskDeps(), toolsets=[FunctionToolset(id="late")])


async def test_an_approved_call_is_its_own_unit_and_replays() -> None:
    """The approval the first run records never stands in for the approved call's own result."""
    world, log = World([[("send", {"to": "ada"})]]), MemoryLog()
    sent: list[str] = []
    toolset: FunctionToolset[Any] = FunctionToolset(id="drill")

    def send(ctx: RunContext[Any], to: str) -> str:
        """Send it (needs a person's yes)."""
        if not ctx.tool_call_approved:
            raise ApprovalRequired
        sent.append(to)
        return f"sent to {to}"

    toolset.add_function(send)
    agent = Agent(
        world.model(),
        name="drill",
        deps_type=TaskDeps,
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
        capabilities=[PrismoidDurability()],
    )
    runner = AgentRunner(agent, TaskDeps(step_id=STEP), PLAIN)
    first = await runner.run("send it", log=await CallLogScope.open(log, STEP, fresh=True))
    assert isinstance(first.output, DeferredToolRequests)
    (call,) = first.output.approvals
    resume = Resume(
        "gate:g1",
        lambda asked: DeferredToolResults(
            approvals={cast(DeferredToolRequests, asked).approvals[0].tool_call_id: ToolApproved()}
        ),
    )
    for _ in range(2):  # the resume, then a re-entry that replays it: one generation throughout
        await runner.run(resume, log=await CallLogScope.open(log, STEP, fresh=False))
    units = log.units(STEP, 0)
    assert (f"drill__function_toolset__drill.call_tool:send@{call.tool_call_id}!approved", "result") in units
    assert sent == ["ada"], "approved once, sent once"
    assert world.provider_calls == 2, "the resume replays the parked run: only its answer is bought"


async def test_a_park_answered_across_a_log_version_bump_runs_again_from_its_prompt() -> None:
    """A park logged under one version and answered under the next: the fenced generation carries the
    prompt, the run goes again from it and the answer answers the deferral it reaches - one gate, sent
    once, no crash."""
    world, log = World([[("send", {"to": "ada"})]]), MemoryLog()
    sent: list[str] = []
    toolset: FunctionToolset[Any] = FunctionToolset(id="drill")

    def send(ctx: RunContext[Any], to: str) -> str:
        """Send it (needs a person's yes)."""
        if not ctx.tool_call_approved:
            raise ApprovalRequired
        sent.append(to)
        return f"sent to {to}"

    toolset.add_function(send)
    agent = Agent(
        world.model(),
        name="drill",
        deps_type=TaskDeps,
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
        capabilities=[PrismoidDurability()],
    )
    runner = AgentRunner(agent, TaskDeps(step_id=STEP), PLAIN)
    first = await runner.run("send it", log=await CallLogScope.open(log, STEP, fresh=True))
    assert isinstance(first.output, DeferredToolRequests)
    asked: list[object] = []

    def approve(ending: object) -> DeferredToolResults:
        asked.append(ending)
        return DeferredToolResults(
            approvals={cast(DeferredToolRequests, ending).approvals[0].tool_call_id: ToolApproved()}
        )

    scope = await CallLogScope.open(log, STEP, fresh=False, version=LOG_VERSION + 1)
    result = await runner.run(Resume("gate:g1", approve), log=scope)
    assert scope.key.generation == 1 and isinstance(result.output, str)
    assert len(asked) == 1 and sent == ["ada"], "one gate's answer, sent once"
    carried = cast(dict[str, Any], log.units(STEP, 1)[("segment#1", "intent")].payload)
    assert carried["line"] == "send it", "the fenced generation's prompt, carried"


def test_the_fingerprint_reads_content_only() -> None:
    first = [ModelResponse(parts=[ToolCallPart("look", {"query": "x"}, tool_call_id="c1")])]
    again = [
        ModelResponse(parts=[ToolCallPart("look", {"query": "x"}, tool_call_id="c1")], model_name="other")
    ]
    changed = [ModelResponse(parts=[ToolCallPart("look", {"query": "y"}, tool_call_id="c1")])]
    assert request_fingerprint(first) == request_fingerprint(again)
    assert request_fingerprint(first) != request_fingerprint(changed)


def test_every_built_agent_carries_the_call_log() -> None:
    runner = build("writer", TaskDeps(), full_catalog())
    assert PrismoidDurability.from_agent(runner.agent) is not None
