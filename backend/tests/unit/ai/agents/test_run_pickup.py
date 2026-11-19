# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A guarded run that dies says how its step's next attempt takes it up on the call log (agent loop spec
3.4, ADR-089): a run cut inside a call is re-entered, a run stopped at a logged point (its request
ceiling) is continued by one new user line in the same generation, and a request the provider
refused is never re-sent. Scripted models only (`FunctionModel`) over the
in-memory call log; `run()` reads `guarded` by the agent's name, so two probe definitions sit in the
registry for the length of each test."""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.exceptions import CallDeferred, ModelHTTPError, UnexpectedModelBehavior, UserError
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
from prismoid.ai.agents.base import AgentDef
from prismoid.ai.agents.run import AgentRunner, Continue, FromHistory, Resume
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.conf import settings
from prismoid.errors import ModelIncapable
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.model_class import ModelClass
from tests.support.rigs.durability_drill import MemoryLog
from tests.support.runners import runner_for

GUARDED, PLAIN = "probe-guarded", "probe-plain"
CONTINUE = "Your previous attempt stopped: the request ceiling. Continue from where you stopped."
STEP = StepId(uuid.uuid4())


@pytest.fixture(autouse=True)
def probes(monkeypatch: pytest.MonkeyPatch) -> None:
    agents_base.get("maker")  # the real registry loads first; the probes sit beside it
    for name, guarded in ((GUARDED, True), (PLAIN, False)):
        monkeypatch.setitem(
            agents_base.REGISTRY,
            AgentName(name),
            AgentDef(
                name=AgentName(name), model_class=ModelClass.CODE, description="a probe", guarded=guarded
            ),
        )


def wedge() -> str:
    """A tool whose run never comes back: the worker lost it mid-call."""
    raise UnexpectedModelBehavior("the tool wedged")


def look(n: int) -> str:
    """Look at one thing."""
    return f"saw {n}"


def _agent(name: str, model: FunctionModel, *, tools: bool = True) -> AgentRunner:
    toolsets: list[FunctionToolset[TaskDeps]] = [FunctionToolset([wedge, look], id="probe")] if tools else []
    agent = Agent(
        model,
        name=name,
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=toolsets,
        capabilities=[PrismoidDurability()],
    )
    return runner_for(agent, TaskDeps(step_id=STEP))


def _calls_wedge(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("wedge", {}, tool_call_id="c1")])


def _user_texts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]


def _returns(messages: list[ModelMessage]) -> dict[str, str]:
    return {
        p.tool_call_id: str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }


async def _dead(name: str, model: FunctionModel, log: MemoryLog, *, tools: bool = True) -> ModelIncapable:
    scope = await CallLogScope.open(log, STEP, fresh=False)
    with pytest.raises(ModelIncapable) as dead:
        await _agent(name, model, tools=tools).run("fill the form", log=scope)
    return dead.value


async def test_a_guarded_run_cut_inside_a_call_is_re_entered() -> None:
    assert (await _dead(GUARDED, FunctionModel(_calls_wedge), MemoryLog())).pickup == "reenter"


async def test_an_unguarded_or_unlogged_run_is_never_taken_up() -> None:
    assert (await _dead(PLAIN, FunctionModel(_calls_wedge), MemoryLog())).pickup is None
    with pytest.raises(ModelIncapable) as dead:
        await _agent(GUARDED, FunctionModel(_calls_wedge)).run("fill the form")
    assert dead.value.pickup is None


class _Looks:
    """Looks once per request until told to stop, then answers; counts what the provider served."""

    def __init__(self) -> None:
        self.seen: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        if CONTINUE in _user_texts(messages) and len(self.seen) > 4:
            return ModelResponse(parts=[TextPart("done looking")])
        return ModelResponse(
            parts=[ToolCallPart("look", {"n": len(self.seen)}, tool_call_id=f"l{len(self.seen)}")]
        )


async def test_a_run_stopped_at_its_ceiling_continues_on_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling is a logged point: the next attempt replays the stopped run (nothing bought twice)
    and continues it with one line inside the trailing request, under a fresh ceiling of its own."""
    monkeypatch.setattr(settings.worker, "request_ceiling", 3)
    log, model = MemoryLog(), _Looks()
    dead = await _dead(GUARDED, FunctionModel(model), log)
    assert dead.pickup == "continue" and len(model.seen) == 3
    scope = await CallLogScope.open(log, STEP, fresh=False)
    result = await _agent(GUARDED, FunctionModel(model)).run(Continue("attempt:2", CONTINUE), log=scope)
    assert result.output == "done looking" and scope.key.generation == 0
    assert len(model.seen) == 5, "the ceiling's three, then the continuation's two"
    continued = model.seen[3]
    assert _user_texts(continued) == ["fill the form", CONTINUE], "the contract once, then the line"
    assert _returns(continued) == {f"l{n}": f"saw {n}" for n in (1, 2, 3)}, "what it saw rides along"
    assert isinstance(continued[-2], ModelResponse) and isinstance(continued[-1], ModelRequest), (
        "the return and the line ride ONE request - never two in a row"
    )


@pytest.mark.parametrize(("status", "pickup"), [(503, "reenter"), (400, None), (413, None)])
async def test_a_provider_failure_is_named_and_a_refused_request_is_never_resent(
    status: int, pickup: str | None
) -> None:
    def fails(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status, "probe-model")

    dead = await _dead(GUARDED, FunctionModel(fails), MemoryLog(), tools=False)
    assert dead.context.get("provider") is True
    assert dead.pickup == pickup


async def test_a_continue_the_library_refuses_is_refused_and_carries_nothing() -> None:
    def refuses(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise UserError("the history cannot continue")

    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("fill the form")]),
        ModelResponse(parts=[ToolCallPart("wedge", {}, tool_call_id="c1")]),
    ]
    with pytest.raises(ModelIncapable) as dead:
        log = await CallLogScope.open(MemoryLog(), STEP, fresh=True)
        await _agent(GUARDED, FunctionModel(refuses), tools=False).run(
            FromHistory(history, None, CONTINUE), log=log
        )
    assert dead.value.context.get("refused") is True
    assert dead.value.pickup is None, "a refused conversation is never re-sent"


class _Verdict(BaseModel):
    n: int


def _unanswered(messages: list[ModelMessage]) -> set[str]:
    """The ids of the tool calls in `messages` that no later request answers."""
    called = {p.tool_call_id for m in messages if isinstance(m, ModelResponse) for p in m.tool_calls}
    answered = {
        p.tool_call_id
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }
    return called - answered


async def test_a_run_dead_on_its_output_retries_continues_with_every_call_answered() -> None:
    """The dead run ends on an output-tool call nobody answered, which a provider refuses; pydantic-ai
    closes it with a synthesized return before every request, so the continuation is sendable."""
    sent: list[list[ModelMessage]] = []

    def answers(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(list(messages))
        assert not _unanswered(messages), "a tool call without its result reached the provider"
        n: object = 1 if CONTINUE in _user_texts(messages) else "not a number"
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"n": n}, tool_call_id=f"o{len(sent)}")]
        )

    def runner() -> AgentRunner:
        agent = Agent(
            FunctionModel(answers),
            name=GUARDED,
            deps_type=TaskDeps,
            output_type=_Verdict,
            retries=1,
            capabilities=[PrismoidDurability()],
        )
        return runner_for(agent, TaskDeps(step_id=STEP))

    log = MemoryLog()
    with pytest.raises(ModelIncapable) as dead:
        await runner().run("judge it", log=await CallLogScope.open(log, STEP, fresh=False))
    assert dead.value.pickup == "continue"
    scope = await CallLogScope.open(log, STEP, fresh=False)
    result = await runner().run(Continue("attempt:2", CONTINUE), log=scope)
    assert result.output == _Verdict(n=1) and len(sent) == 3


async def test_a_parks_answer_starts_the_guards_evidence_afresh() -> None:
    """Six looks at the same thing told the run to finish, then it asked; after the person's answer the
    same look runs, where a run continued without one would still be held."""
    ask = "ask_user"

    def ask_user(question: str) -> str:
        raise CallDeferred()

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        made = sum(isinstance(m, ModelResponse) for m in messages)
        if made < 6 or made == 7:
            return ModelResponse(parts=[ToolCallPart("look", {"n": 1}, tool_call_id=f"l{made}")])
        if made == 6:
            return ModelResponse(parts=[ToolCallPart(ask, {"question": "which?"}, tool_call_id="q")])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model),
        name=GUARDED,
        deps_type=TaskDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([look, ask_user], id="probe")],
        capabilities=[RunGuard(can_park=True), PrismoidDurability()],
    )
    runner = runner_for(agent, TaskDeps(step_id=STEP))
    log = MemoryLog()
    parked = await runner.run("look", log=await CallLogScope.open(log, STEP, fresh=True))
    assert isinstance(parked.output, DeferredToolRequests)
    resume = Resume("gate:g1", lambda ending: DeferredToolResults(calls={"q": "the first"}))
    result = await runner.run(resume, log=await CallLogScope.open(log, STEP, fresh=False))
    assert result.output == "done"
    assert _returns(result.all_messages())["l7"] == "saw 1"
