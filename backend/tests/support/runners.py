# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Runners for tests: a real `AgentRunner` around a hand-made agent (`runner_for`), a scripted one
(`ScriptedRunner`, `scripted` for a `build()` stand-in) that hands every run to a test's script with
the deps it was built with, so a test drives the engine without a model or a catalog, and
`run_recorded`, which runs tool calls the way a step does: under `ToolRecorder`. A scripted agent
that holds a shell starts on paper and uses its shell first, so it takes its Machine before the
script runs (`capabilities/machine_acquire.py`); a script marked `paper_first` drives that itself."""

from __future__ import annotations

import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import AbstractToolset

from prismoid.ai.agents.base import AgentDef, definition_for
from prismoid.ai.agents.cancellation import Cancelled
from prismoid.ai.agents.run import AgentRunner, Continue, FromHistory, Resume, Start
from prismoid.ai.capabilities.durability import CallLogScope
from prismoid.ai.capabilities.machine_acquire import needs_machine
from prismoid.ai.capabilities.tool_record import ToolRecorder
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.call_rows import JournalTool
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass

type Script = Callable[..., Awaitable[Any]]

# The output each step's scripted run last ended on: a resume answers it, as the real runner answers
# the ending its replay stops on.
_ENDED: dict[object, object] = {}
_PAPER_FIRST: weakref.WeakSet[Script] = weakref.WeakSet()  # scripts that call their own frame tools

PLAIN: AgentDef = AgentDef(
    name=AgentName("probe"), model_class=ModelClass.FAST_EXECUTOR, description="a test probe"
)


def paper_first(script: Script) -> Script:
    """Mark a script that calls its own frame tools: no first shell is assumed before it runs."""
    _PAPER_FIRST.add(script)
    return script


def _shell_first(name: str, deps: TaskDeps) -> bool:
    """Would the agent `name` (a real one) take its Machine at its first call?"""
    try:
        tools = definition_for(name, deps).tools
    except KeyError:
        return False
    return any(needs_machine(tool) for tool in tools)


def runner_for(agent: Agent[TaskDeps, Any], deps: TaskDeps) -> AgentRunner:
    """A real runner around a hand-made agent: the definition its name resolves to, else a plain one."""
    try:
        definition = definition_for(str(agent.name or ""), deps)
    except KeyError:
        definition = PLAIN
    return AgentRunner(agent, deps, definition)


@dataclass(frozen=True, slots=True)
class ScriptedRunner(AgentRunner):
    """A runner with no agent: `run(start)` awaits `script(runner, prompt, deps, **given)`, with a
    resume's `answers` to the ending of the step's last scripted run (or its line as the prompt), and
    a continued run's line as the prompt."""

    script: Script

    @classmethod
    def of(cls, script: Script, deps: Any, name: str = "probe") -> ScriptedRunner:
        return cls(
            cast("Agent[TaskDeps, Any]", None),
            deps,
            AgentDef(name=AgentName(name), model_class=ModelClass.FAST_EXECUTOR, description="scripted"),
            script,
        )

    async def run(
        self, start: Start, *, log: CallLogScope | None = None, cancelled: Cancelled | None = None
    ) -> Any:
        given: dict[str, Any] = {}
        prompt: Any = start
        if isinstance(start, Resume):
            answer = start.answer(_ENDED.get(self.deps.step_id))
            prompt, given["answers"] = (answer, None) if isinstance(answer, str) else ("", answer)
        elif isinstance(start, FromHistory):
            prompt, given = start.line, {"history": start.history, "answers": start.answers}
        elif isinstance(start, Continue):
            prompt = start.line
        machine = self.deps.machine
        shell = machine is not None and _shell_first(str(self.definition.name), self.deps)
        if machine is not None and shell and self.script not in _PAPER_FIRST:
            self.deps.frame = await machine.acquire()
        result = await self.script(
            self, prompt, self.deps, **{k: v for k, v in given.items() if v is not None}
        )
        _ENDED[self.deps.step_id] = getattr(result, "output", None)
        return result


def scripted(script: Script) -> Callable[..., ScriptedRunner]:
    """A `build(name, deps, catalog, ...)` replacement whose runners run `script`."""

    def build(name: str, deps: Any, *_args: Any, **_kwargs: Any) -> ScriptedRunner:
        return ScriptedRunner.of(script, deps, name)

    return build


async def run_recorded(
    toolset: AbstractToolset[Any],
    calls: Sequence[tuple[str, dict[str, Any]]],
    journal: JournalTool,
    *,
    frame: Any = None,
) -> None:
    """Run `calls` one per model request under `ToolRecorder`, the step's `frame` on the deps."""

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent = sum(isinstance(m, ModelResponse) for m in messages)
        if sent >= len(calls):
            return ModelResponse(parts=[TextPart("done")])
        tool, args = calls[sent]
        return ModelResponse(parts=[ToolCallPart(tool, args, f"call-{sent}")])

    agent = Agent(
        FunctionModel(script), deps_type=TaskDeps, toolsets=[toolset], capabilities=[ToolRecorder()]
    )
    await agent.run("go", deps=TaskDeps(frame=frame, journal_tool=journal))
