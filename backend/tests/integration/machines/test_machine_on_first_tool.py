# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Machine starts on the first tool that needs the computer (ADR-063, amended): the real
`execute_step` with the model stubbed, every frame a `FakeFrame`, and the `MachineAcquire` hook run
before each scripted tool call as the agent's run runs it. A step that only reads, writes and
publishes ends on paper with no Machine, even one that holds a shell; its first shell starts exactly one and carries the paper's
files onto it; a park holding a draft moves it to the Machine its resume binds; a run that dies
before a shell leaves no Machine, and one whose files never reached the Machine gives it back. The
acquire holds no task row, so the lock order stays the Machine row, then the task row."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelResponse, ToolCallPart
from sqlalchemy import text

from prismoid.ai.capabilities.machine_acquire import MachineAcquire
from prismoid.ai.frames.paper import PaperFrame
from prismoid.ai.tools.ask import ASK_USER
from prismoid.conf import settings
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution
from prismoid.operations.execution import run as run_stage
from prismoid.schemas.plan import StepState
from tests.support.rigs.machine_execution_rig import model as model
from tests.support.rigs.machine_execution_rig import step_machine, step_run, task_with_steps
from tests.support.runners import paper_first

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def paper_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.machines, "enabled", True)
    monkeypatch.setattr(settings.machines, "paper", True)


async def _tool(deps: Any, name: str) -> None:
    """The capability's hook before a call of `name`, as the run dispatches it."""
    ctx = cast(Any, SimpleNamespace(deps=deps))
    call = ToolCallPart(tool_name=name, args={}, tool_call_id=f"call-{name}")
    await MachineAcquire().before_tool_execute(ctx, call=call, tool_def=cast(Any, None), args={})


async def _writes(deps: Any, path: str, body: bytes) -> None:
    await _tool(deps, "write_file")
    await deps.frame.write_bytes(path, body)


async def _publishes(deps: Any, path: str) -> SimpleNamespace:
    await _tool(deps, "publish")
    await deps.frame.publish(path, "document", "Findings", ())
    return SimpleNamespace(output=f"published {path}")


def _on_paper(frame: Any) -> bool:
    return bool(getattr(frame, "on_paper", False))


async def _delivered(task_id: Any, step_id: Any) -> list[str]:
    """The titles the step delivered, once it is done."""
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.step_state is StepState.DONE
        return [a.title for a in await Artifact.for_step(session, step_id)]


async def test_a_step_writing_markdown_and_running_no_shell_ends_on_paper(
    fake_frames: Any, model: Any
) -> None:
    """A coder holds a shell; a run that never calls it takes no Machine."""

    async def researches(agent: Any, prompt: Any, deps: Any, **kw: Any) -> Any:
        await _tool(deps, "read_file")
        await _writes(deps, "/work/findings.md", b"# Findings\n\nThree vendors.")
        assert _on_paper(deps.frame), "a file the model writes needs no computer"
        return await _publishes(deps, "/work/findings.md")

    model(paper_first(researches))
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    await step_run(task_id, step_id)
    assert await step_machine(task_id) is None, "no Machine row"
    assert fake_frames.made == [], "no frame was made on any driver"
    assert await _delivered(task_id, step_id) == ["Findings"]


async def test_the_first_frame_tool_boots_exactly_one_and_carries_the_paper(
    fake_frames: Any, model: Any
) -> None:
    held: list[Any] = []

    async def codes(agent: Any, prompt: Any, deps: Any, **kw: Any) -> Any:
        await _writes(deps, "/work/notes.md", b"plan")
        assert fake_frames.made == [], "nothing boots before a tool needs the computer"
        await _tool(deps, "shell")
        await _tool(deps, "shell")  # the second call binds nothing new
        held.append(deps.frame)
        assert (await deps.frame.exec("true")).exit_code == 0
        await _writes(deps, "/work/findings.md", b"# Findings")
        return await _publishes(deps, "/work/findings.md")

    model(paper_first(codes))
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    await step_run(task_id, step_id)
    (frame,) = held
    assert not isinstance(frame, PaperFrame) and len(fake_frames.made) == 1, "exactly one Machine"
    assert fake_frames.world[frame.ref].files["/work/notes.md"][0] == b"plan", "the paper's draft came along"
    row = await step_machine(task_id)
    assert row is not None and row.held_by is None, "the attempt gave its Machine back"
    assert await _delivered(task_id, step_id) == ["Findings"]


async def test_a_park_holding_a_draft_resumes_on_the_machine_with_it(
    fake_frames: Any, silent_dispatcher: None, model: Any
) -> None:
    seen: list[bytes] = []

    async def drafts_then_asks(agent: Any, prompt: Any, deps: Any, **kw: Any) -> Any:
        if kw.get("answers") is None:
            await _writes(deps, "/work/draft.md", b"half a memo")
            call = ToolCallPart(
                tool_name=ASK_USER, args={"questions": [{"question": "Tone?"}]}, tool_call_id="ask-1"
            )
            return SimpleNamespace(
                output=DeferredToolRequests(calls=[call]), all_messages=lambda: [ModelResponse(parts=[call])]
            )
        assert not _on_paper(deps.frame), "the resume binds the Machine the park kept"
        seen.append(await deps.frame.read_bytes("/work/draft.md"))
        return await _publishes(deps, "/work/draft.md")

    model(paper_first(drafts_then_asks))
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    assert (await step_run(task_id, step_id)).startswith(execution.ASKING)
    row = await step_machine(task_id)
    assert row is not None and row.held_by is not None, "the park holds the Machine its draft moved to"
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.resume_state
        step.resume_state = {**step.resume_state, "answer": "plain", "cancelled": False}
        await session.commit()
    await step_run(task_id, step_id)
    assert seen == [b"half a memo"]
    await _delivered(task_id, step_id)


async def test_a_crash_before_boot_leaves_no_machine(fake_frames: Any, model: Any) -> None:
    async def dies(agent: Any, prompt: Any, deps: Any, **kw: Any) -> Any:
        await _writes(deps, "/work/notes.md", b"plan")
        raise ConnectionResetError("the worker went away")

    model(paper_first(dies))
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    with pytest.raises(ConnectionResetError):
        await step_run(task_id, step_id)
    assert await step_machine(task_id) is None and fake_frames.made == []


async def test_a_machine_the_paper_never_reached_is_given_back(
    fake_frames: Any, model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(self: PaperFrame, frame: Any) -> None:
        raise OSError("the copy failed")

    monkeypatch.setattr(PaperFrame, "carry_to", refused)

    async def codes(agent: Any, prompt: Any, deps: Any, **kw: Any) -> Any:
        await _tool(deps, "shell")
        return SimpleNamespace(output="never")

    model(paper_first(codes))
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    with pytest.raises(OSError, match="the copy failed"):
        await step_run(task_id, step_id)
    row = await step_machine(task_id)
    assert row is not None and row.held_by is None, "no hold is left behind"


async def test_the_acquire_holds_no_task_row(
    fake_frames: Any, model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every acquire takes the Machine row, then the task row; mid-run the step holds neither."""
    real = run_stage.claim
    free: list[bool] = []

    async def checked(session: Any, task: Any, *args: Any, **kw: Any) -> Any:
        async with session_scope(workspace_id=task.workspace_id) as other:
            got = await other.scalar(
                text("select id from tasks where id = :t for update nowait"), {"t": task.id}
            )
            free.append(got == task.id)
            await other.rollback()
        return await real(session, task, *args, **kw)

    monkeypatch.setattr(run_stage, "claim", checked)

    async def codes(agent: Any, prompt: Any, deps: Any, **kw: Any) -> Any:
        await _tool(deps, "shell")
        await _writes(deps, "/work/a.md", b"a")
        return await _publishes(deps, "/work/a.md")

    model(paper_first(codes))
    task_id, (step_id,) = await task_with_steps(1, agent="maker")
    await step_run(task_id, step_id)
    assert free == [True], "the task row was free when the Machine was claimed"
