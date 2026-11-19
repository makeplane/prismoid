# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A steer supersedes an approval its step was waiting on (ADR-064).

A write chosen before the steer must never run on an approval given after it: the door closes
every open APPROVE gate of the task as `decline` naming the steer, applies the steer at that step
(`applied{approval}`), and wakes the step after its commit; the resumed run tells each pending
call it was held. A question or a connect block asks the person something the steer does not
answer, so it stays open.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import logfire
import pytest
from pydantic_ai import DeferredToolRequests, ToolDenied
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls
from sqlalchemy.ext.asyncio import AsyncSession

import prismoid.ai.agents.base as agents_base
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Forbidden
from prismoid.operations import dispatch, execution, gates, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import answers as ex_answers
from prismoid.operations.execution.deferrals import ApprovalDeferral
from prismoid.schemas.events import GateResolved, SteerApplied
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateAction, GateState, StepState, TaskStatus
from prismoid.schemas.steering import SafePoint
from tests.integration.steering.test_steer_inbox import (
    Stream,
    _call,  # pyright: ignore[reportPrivateUsage]
    _events,  # pyright: ignore[reportPrivateUsage]
    _run,  # pyright: ignore[reportPrivateUsage]
    _running_step_with_editor,  # pyright: ignore[reportPrivateUsage]
    _steer,  # pyright: ignore[reportPrivateUsage]
    _steers,  # pyright: ignore[reportPrivateUsage]
    _step,  # pyright: ignore[reportPrivateUsage]
    _told,  # pyright: ignore[reportPrivateUsage]
)
from tests.integration.steering.test_steer_inbox import real_agent as real_agent  # the real build, scripted
from tests.support.asks import question_set
from tests.support.cast import member
from tests.support.connector_rows import (
    ALL_ASK,
    adas_turn_with_bob_in_the_project,
    gmail_mount,
    patch_email_probe,
)
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


@pytest.fixture
def email_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_email_probe(monkeypatch)


DRAFT = "Don't post; draft it instead"
HELD = (
    "Held, not run: Bob steered while this waited for approval: 'Don't post; draft it instead'. "
    "Re-issue it if it still fits."
)


async def _waiting_on_approval(s: AsyncSession) -> tuple[Actor, Task, PlanStep, Gate]:
    """Ada's Project task, Bob an editor there; its step parked on approving two Slack posts, the
    way `_park_on_approval` leaves it (the run's mind in `resume_state`, the task `waiting`)."""
    ada, workspace = await member(s, "Ada")
    bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
    project = (await project_ops.create(s, ada, name=f"s-{uuid.uuid4().hex[:6]}")).project
    s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
    await s.flush()
    task = await task_ops.create_task(s, ada, goal="post the launch note", project_id=project.id)
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    step = PlanStep(task_id=task.id, ordinal=0, title="post it")
    s.add(step)
    await s.flush()
    calls = [
        ("call_0", "Slack", "slack_post", "#launch: hello"),
        ("call_1", "Slack", "slack_post", "#all: hi"),
    ]
    step.resume_state = {
        "history": "h",
        "approval_calls": {c: {"connector": n, "tool": t, "fingerprint": None} for c, n, t, _ in calls},
        "told": 0,
    }
    gate = await gates.open_approval(s, task, StepId(step.id), calls)
    task.transition(TaskStatus.WAITING)
    await s.commit()
    return bob, task, step, gate


async def test_a_steer_closes_an_open_approval_and_the_write_is_held(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    async with session_scope() as s:
        bob, task, step, gate = await _waiting_on_approval(s)
        row = await steering.steer(s, bob, task.id, DRAFT)

        await s.refresh(gate)
        assert gate.state == GateState.RESOLVED.value and gate.resolved_by == bob.id
        resolution = dict(gate.resolution or {})
        assert resolution["action"] == "decline"
        assert resolution["content"] == {"superseded_by": str(row.command_id)}
        assert resolution["resolved_by_id"] == str(bob.id)

        await s.refresh(row)
        assert (row.state, row.safe_point, row.applied_step_id) == ("applied", "approval", step.id)
        await s.refresh(step)
        assert step.epoch == row.epoch, "the step took the steer"

        events = await TaskEvent.replay(s, task.id, since=0)
        types = [e.type for e in events]
        assert types.index("plan.edited") < types.index("gate.resolved") < types.index("steer.applied")
        (closed,) = [e for e in events if isinstance(e, GateResolved)]
        assert closed.actor is not None and closed.actor.name == "Bob"
        assert (closed.action, closed.content) == (GateAction.DECLINE, {"superseded_by": str(row.command_id)})
        (applied,) = [e for e in events if isinstance(e, SteerApplied)]
        assert (applied.step_id, applied.safe_point.value, applied.author) == (step.id, "approval", "Bob")

        (wake,) = [(k, p) for k, p, t in recording_dispatcher.sends if t == gates.GATE_TOPIC]
        key, payload = wake
        assert key == dispatch.step_job_key(step.id), "the parked step's own run_step is woken"
        assert isinstance(payload, dict)
        woken: dict[str, Any] = dict(payload)  # pyright: ignore[reportUnknownArgumentType]
        assert woken["gate_id"] == str(gate.id) and woken["content"] == resolution["content"]

        await execution.store_answer(s, step.id, woken)
        resume = dict(step.resume_state or {})
        assert (resume["approval_action"], resume["cancelled"]) == ("superseded", False)
        parked = ApprovalDeferral(items=[(c, "", "", "") for c in ("call_0", "call_1")])
        verdicts = ex_answers.verdicts(parked, DeferredToolRequests(), resume)
        assert verdicts == {"call_0": ToolDenied(HELD), "call_1": ToolDenied(HELD)}


async def _beside_an_approval(
    sends: RecordingDispatcher, opens: Callable[[AsyncSession, Task, StepId], Awaitable[Gate]]
) -> None:
    """Another step of the task waits on `opens`' gate beside the approval; Bob steers. The other
    gate stays open and only the approval's step is woken."""
    async with session_scope() as s:
        bob, task, step, approval = await _waiting_on_approval(s)
        other = PlanStep(task_id=task.id, ordinal=1, title="other")
        s.add(other)
        await s.flush()
        beside = await opens(s, task, StepId(other.id))
        await s.commit()

        row = await steering.steer(s, bob, task.id, DRAFT)

        await s.refresh(beside)
        await s.refresh(approval)
        assert beside.is_open(), "a steer answers no question and connects nothing"
        assert approval.state == GateState.RESOLVED.value
        woken = {k for k, _p, t in sends.sends if t == gates.GATE_TOPIC}
        assert woken == {dispatch.step_job_key(step.id)}, "only the approval's step is woken"
        await s.refresh(row)
        assert row.applied_step_id == step.id


async def test_a_question_gate_stays_open_under_a_steer(recording_dispatcher: RecordingDispatcher) -> None:
    async def question(s: AsyncSession, task: Task, step_id: StepId) -> Gate:
        return await gates.open_question(s, task, step_id, question_set(("Which channel?", ["#launch"])))

    await _beside_an_approval(recording_dispatcher, question)


async def test_a_connect_gate_stays_open_under_a_steer(recording_dispatcher: RecordingDispatcher) -> None:
    async def connect(s: AsyncSession, task: Task, step_id: StepId) -> Gate:
        return await gates.open_connect(
            s, task, step_id, slug="notion", display="Notion", why="the launch brief lives in Notion"
        )

    await _beside_an_approval(recording_dispatcher, connect)


async def test_a_missing_steer_still_holds_the_write_and_says_so(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steer behind `superseded_by` is gone from the record: the call is still denied, as the
    plain decline, and the gap is logged."""
    warned: list[str] = []
    monkeypatch.setattr(logfire, "warning", lambda message, **_: warned.append(message))
    async with session_scope() as s:
        _bob, _task, step, gate = await _waiting_on_approval(s)
        content = {"superseded_by": str(uuid.uuid4())}
        await execution.store_answer(
            s, step.id, {"action": "decline", "content": content, "gate_id": str(gate.id)}
        )
        resume = dict(step.resume_state or {})
        assert (resume["approval_action"], resume["superseded_text"]) == (
            "superseded",
            execution.DENIED_BY_USER,
        )
        assert len(warned) == 1 and "not on record" in warned[0]


async def test_the_resumed_run_is_told_each_post_was_held_and_the_steer_in_full(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real run and its real resume: the model picks two posts that need approval and the step
    parks; Bob steers. The resume answers each post with the held text, the next request carries
    the steer in full, the step drafts instead - and the steer is applied once, at the approval."""
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    posted: list[str] = []
    real_build = agents_base.build

    def build_with_a_post(*args: Any, **kwargs: Any) -> Any:
        runner = real_build(*args, **kwargs)

        @runner.agent.tool_plain(requires_approval=True)
        def slack_post(channel: str) -> str:  # pyright: ignore[reportUnusedFunction]
            posted.append(channel)
            return "posted"

        return runner

    monkeypatch.setattr(agents_base, "build", build_with_a_post)
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(messages)
        n = len(requests)
        if n == 1:
            yield {
                0: DeltaToolCall(name="slack_post", json_args='{"channel": "#launch"}', tool_call_id="c0"),
                1: DeltaToolCall(name="slack_post", json_args='{"channel": "#all"}', tool_call_id="c1"),
            }
        elif n == 2:
            yield _call("write_file", {"path": "/work/memo.md", "content": "a draft"}, "w1")
        elif n == 3:
            yield _call("publish", {"path": "/work/memo.md", "type": "document", "title": "Draft"}, "p1")
        else:
            yield "drafted, not posted"

    real_agent(stream)
    assert (await _run(task_id, step_id)).startswith(execution.ASKING), "parked on the approval"

    await _steer(bob_id, task_id, DRAFT)
    (wake,) = [p for _k, p, t in recording_dispatcher.sends if t == gates.GATE_TOPIC]
    async with row_scope(Task, task_id) as s:  # the step's recv returns: `run_step` stores it
        await execution.store_answer(s, step_id, wake)  # pyright: ignore[reportArgumentType]
        await s.commit()
    await _run(task_id, step_id)

    assert posted == [], "neither post ran"
    resumed = requests[1]
    held = {
        p.tool_call_id: p.content
        for m in resumed
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }
    assert held == {"c0": HELD, "c1": HELD}
    assert _told(resumed).count("**Bob steered** while you worked: Don't post; draft it instead") == 1
    (applied,) = await _events(task_id, SteerApplied)
    assert (applied.safe_point, applied.step_id) == (SafePoint.APPROVAL, step_id), "applied once"
    (settled,) = await _steers(task_id)
    assert (settled.state, settled.safe_point) == ("applied", "approval")
    assert (await _step(task_id, step_id)).step_state is StepState.DONE


async def test_a_re_issued_write_on_a_teammates_steer_still_asks_the_creator(
    db: AsyncSession, actor: Actor, email_probe: None, silent_dispatcher: None
) -> None:
    """Bob's steer supersedes Ada's pending approval: the turn is borrowed from then on, so the
    write the step re-issues mounts on ask and parks for Ada alone."""
    bob, task = await adas_turn_with_bob_in_the_project(db, actor)
    step = PlanStep(task_id=task.id, ordinal=0, title="reply")
    db.add(step)
    await db.flush()
    pending = await gates.open_approval(
        db, task, StepId(step.id), [("call_0", "Gmail", "gmail_send_email", "to: board")]
    )
    await db.commit()

    await steering.steer(db, bob, task.id, "reply to the chair only")
    await db.refresh(pending)
    assert pending.state == GateState.RESOLVED.value

    assert await TaskEvent.teammate_input(db, task.id, actor.id) == "plan.edited"
    policies, _ = await gmail_mount(db, task)
    assert policies == ALL_ASK, "the re-issued write asks"

    again = await gates.open_approval(
        db, task, StepId(step.id), [("call_1", "Gmail", "gmail_send_email", "to: chair")]
    )
    await db.commit()
    again_id = again.id
    with pytest.raises(Forbidden, match="only the task's creator"):
        await gates.resolve(db, bob, again_id, GateAction.ACCEPT)
    await db.rollback()
    await db.refresh(actor)
    resolution = await gates.resolve(db, actor, again_id, GateAction.ACCEPT)
    assert resolution.resolved_by_id == actor.id
