# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard's loop sense for a step that builds with the shell: a shell call after which the frame
finds a changed file under `/work` is an edit, so the same build run again after it is progress; the same
build that changes nothing is still a stall; and a person's answer to a park starts the evidence afresh."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.loops import RECALL_FROM, RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.driver import ExecResult
from tests.support.guard import watched

REWRITE = "cat > /work/build.py <<'EOF'\nprint('v{n}')\nEOF"
BUILD = "python3 /work/build.py"
CHANGED = {"work": "changed"}


@dataclass
class _Work:
    """A frame whose `/work` check finds a change after each command in `changes`."""

    changes: set[str]
    last: str = ""
    checks: list[str] = field(default_factory=list[str])

    async def exec(self, command: str, timeout: float = 120.0) -> ExecResult:  # noqa: ASYNC109
        self.checks.append(command)
        return ExecResult(0, "/work/build.py\n" if self.last in self.changes else "", "")

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: str = "agent") -> None:
        return None


def _shell(work: _Work) -> Tool[TaskDeps]:
    def shell(command: str) -> str:
        work.last = command
        return "Saved /work/report.docx"

    return Tool(shell)


async def _run(
    commands: list[str], work: _Work | None, history: list[ModelMessage] | None = None, since: int = 0
) -> RunGuard:
    """One shell call per request, then a text ending; the run's guard."""
    asked = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal asked
        asked += 1
        if asked <= len(commands):
            return ModelResponse(
                parts=[ToolCallPart("shell", {"command": commands[asked - 1]}, tool_call_id=f"c{asked}")]
            )
        return ModelResponse(parts=[TextPart("end")])

    guard, runs = watched()
    agent = Agent(
        FunctionModel(model),
        deps_type=TaskDeps,
        output_type=str,
        toolsets=[FunctionToolset([_shell(work or _Work(set()))])],
        capabilities=[guard],
    )
    await agent.run(
        "go",
        message_history=history,
        deps=TaskDeps(frame=cast(Any, work)),
        metadata={RECALL_FROM: since} if since else None,
    )
    return runs[0]


def _rebuilds(n: int) -> list[str]:
    return [c for k in range(n) for c in (REWRITE.format(n=k), BUILD)]


async def test_a_build_rerun_after_a_shell_rewrite_is_progress() -> None:
    rewrites = {REWRITE.format(n=k) for k in range(3)}
    guard = await _run(_rebuilds(3), _Work(rewrites))
    assert guard.stalls == 0


async def test_the_same_build_that_changes_nothing_is_still_a_stall() -> None:
    guard = await _run([BUILD, BUILD, BUILD], _Work(set()))
    assert guard.stalls == 1


def _history(calls: list[tuple[str, dict[str, str] | None]]) -> list[ModelMessage]:
    """A stopped run's history: each shell call and its return, with the change mark the guard left."""
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
    for n, (command, mark) in enumerate(calls):
        history.append(
            ModelResponse(parts=[ToolCallPart("shell", {"command": command}, tool_call_id=f"h{n}")])
        )
        history.append(
            ModelRequest(parts=[ToolReturnPart("shell", "ok", tool_call_id=f"h{n}", metadata=mark)])
        )
    return ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(history))


async def test_a_continued_run_counts_the_shell_edits_its_history_marked() -> None:
    marked = [(c, CHANGED if c != BUILD else None) for c in _rebuilds(2)]
    guard = await _run([BUILD], _Work(set()), _history([*marked, (REWRITE.format(n=9), CHANGED)]))
    assert guard.stalls == 0
    unmarked = await _run([BUILD], _Work(set()), _history([(BUILD, None), (BUILD, None)]))
    assert unmarked.stalls == 1, "a true loop across a continuation is still the step's"


async def test_a_persons_answer_starts_the_stall_evidence_afresh() -> None:
    stuck = _history([("ls /work", None)] * 6)  # two stalls: the run was told to finish
    held = await _run(["ls /work"], _Work(set()), stuck)
    assert held.finish_now
    answered = await _run(["ls /work"], _Work(set()), stuck, since=6)
    assert (answered.stalls, answered.finish_now) == (0, False)
