# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A steer reaches a running step at its next safe point (ADR-064): the call chosen before it is
held, the next request carries it, its state and the step's epoch move, the express lane too; a
park saves how far the run was told; each attempt's contract and the verify receipt carry the
steers the step took.

The real `execute_step` against real rows and every frame a `FakeFrame`. The tests that need the
real capability patch only the model resolver `build()` uses, so the real agent, the real
`Steering` capability and the real inbox run; the stamp tests stub the run (`test_machine_execution`'s
pattern), so nothing but the ladder touches the epoch."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from sqlalchemy import select

import prismoid.ai.agents.base as agents_base
import prismoid.ai.tools.connectors as connector_engine
from prismoid import storage
from prismoid.ai.context.thread import render_thread_md
from prismoid.ai.tools.ask import ASK_USER
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import connectors as connector_ops
from prismoid.operations import execution, planning, steering, verification
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.thread import MOUNT_THREAD_TOKENS, render_thread
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import PauseApplied, SteerApplied, ToolCalled
from prismoid.schemas.ids import AgentName, StepId, TaskId
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.park import park_of
from prismoid.schemas.plan import GateKind, StepKind, StepSpec, StepState, TaskStatus
from prismoid.schemas.steering import REJECT_FAILED, SafePoint
from tests.support.artifacts import deliverable
from tests.support.cast import member
from tests.support.connector_rows import connect_gmail
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]
Run = Callable[..., Awaitable[Any]]


def _told(messages: list[ModelMessage]) -> str:
    """Everything the run has been told in words: the prompt, injected steers, enqueued lines."""
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _spec(title: str) -> dict[str, Any]:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("maker"),
        title=title,
        contract=Contract(
            goal=title,
            acceptance=["it exists"],
            inputs=[],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=title),
        ),
    ).model_dump(mode="json")


async def _running_step_with_editor(name: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ada's Project task, `name` an editor there, one READY maker step drafted at the turn's epoch."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        editor, _ = await member(s, name, role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"i-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, ada, goal="a memo", project_id=project.id, clarify="never")
        task.transition(TaskStatus.PLANNING)
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="memo",
            spec=_spec("memo"),
            attempt=0,
            epoch=task.plan_epoch,
            turn_epoch=task.turn_epoch,
        )
        s.add(step)
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        return task.id, step.id, editor.id


async def _steer(actor_id: uuid.UUID, task_id: uuid.UUID, text: str) -> Steer:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        return await steering.steer(s, actor, task_id, text)


async def _run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _answer(task_id: uuid.UUID, step_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.resume_state
        step.resume_state = {**step.resume_state, "answer": "blue", "cancelled": False}
        await session.commit()


async def _step(task_id: uuid.UUID, step_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        s.expunge(step)
        return step


async def _steers(task_id: uuid.UUID) -> list[Steer]:
    async with row_scope(Task, task_id) as s:
        rows = await Steer.since(s, task_id, 0)
        for row in rows:
            s.expunge(row)
        return rows


async def _events[E](task_id: uuid.UUID, kind: type[E]) -> list[E]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, kind)]


def _call(name: str, args: dict[str, Any], call_id: str) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


@pytest.fixture
def real_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[Stream], None]:
    """The real `build()` with a scripted model (a step agent streams, so `stream_function`)."""
    monkeypatch.setattr(ex_harvest, "extract_previews", _no_previews)

    def use(stream: Stream) -> None:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))

    return use


@pytest.fixture
def stub_run(monkeypatch: pytest.MonkeyPatch) -> Callable[[Run], None]:
    """No agent at all: `build()` and the run are stubs, so no safe point ever fires."""
    monkeypatch.setattr(ex_harvest, "extract_previews", _no_previews)

    def use(run: Run) -> None:
        monkeypatch.setattr(agents_base, "build", scripted(run))

    return use


async def _publish(frame: Any) -> str:
    await frame.write_bytes("/work/memo.md", b"# memo")
    await frame.publish("/work/memo.md", "document", "memo", ())
    return "wrote /work/memo.md"


# ── the safe points, with the real capability ─────────────────────────────────────────────────


@pytest.mark.usefixtures("on_the_machine")
async def test_a_steer_mid_step_holds_the_chosen_call_and_reaches_the_next_request(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    requests: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        if len(requests) == 1:
            await _steer(bob_id, task_id, "write it in euros")  # Bob steers while the model chooses
            yield _call("write_file", {"path": "/work/a.md", "content": "$"}, "w1")
        elif len(requests) == 2:
            yield _call("write_file", {"path": "/work/memo.md", "content": "EUR 10"}, "w2")
        elif len(requests) == 3:
            yield _call("publish", {"path": "/work/memo.md", "type": "document", "title": "Memo"}, "p1")
        else:
            yield "done in euros"

    real_agent(stream)
    await _run(task_id, step_id)

    assert "**Bob steered** while you worked: write it in euros" in requests[1]
    assert "write it in euros" not in requests[0]
    assert all("/work/a.md" not in f.files for f in fake_frames.world.values()), "the held write never ran"
    assert any("/work/memo.md" in f.files for f in fake_frames.world.values())
    (row,) = await _steers(task_id)
    step = await _step(task_id, step_id)
    assert row.state == "applied" and row.safe_point == "before_tool" and row.applied_step_id == step_id
    assert step.epoch == row.epoch and step.epoch_actor_id == bob_id
    assert step.step_state is StepState.DONE
    (applied,) = await _events(task_id, SteerApplied)
    assert (applied.command_id, applied.author, applied.step_title, applied.safe_point) == (
        row.command_id,
        "Bob",
        "memo",
        SafePoint.BEFORE_TOOL,
    )
    assert applied.step_id == step_id


async def test_review_focus_a_steer_reaches_an_express_turn(
    silent_dispatcher: None, real_agent: Callable[[Stream], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_scope() as s:
        asker, _ = await member(s, "Asker")
        await s.flush()
        task = await task_ops.create_task(s, asker, goal="What's the time?", clarify="never")
        task.transition(TaskStatus.PLANNING)
        await s.flush()
        view = await planning.draft_plan(s, task.id)
        await s.commit()
        task_id, asker_id = task.id, asker.id
    assert view.steps[0].lane_role == "express"
    step_id = uuid.UUID(str(view.steps[0].id))
    async with row_scope(Task, task_id) as s:
        await execution.advance(s, task_id, TaskStatus.RUNNING)
        await s.commit()

    requests: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        if len(requests) == 1:
            await _steer(asker_id, task_id, "answer in French")  # lands while the answer is written
            yield "It is 14:02 UTC."
        else:
            yield "Il est 14 h 02 UTC."

    real_agent(stream)
    outcome = await _run(task_id, step_id)

    assert len(requests) == 2, "the ending turned into one more request"
    assert "**Asker steered** before you finished: answer in French" in requests[1]
    assert outcome == "Il est 14 h 02 UTC."
    (row,) = await _steers(task_id)
    assert row.state == "applied" and row.safe_point == "final_answer" and row.applied_step_id == step_id


async def test_a_park_saves_told_and_the_resume_restores_it(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """Amended (plan review): the steer is injected at a before-model point, a LATER response asks
    and parks; the park saves the injected epoch, and the resumed run is never told it again - a
    steer that landed during the park is told once."""
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        ada_id = task.creator_actor_id
    requests: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        n = len(requests)
        if n == 1:
            await _steer(bob_id, task_id, "write it in euros")
            yield _call("write_file", {"path": "/work/a.md", "content": "$"}, "w1")  # held
        elif n == 2:
            question = {"question": "Which colour?", "options": ["blue", "red"]}
            yield _call(ASK_USER, {"questions": [question]}, "ask-1")
        elif n == 3:
            yield _call("write_file", {"path": "/work/memo.md", "content": "EUR 10"}, "w2")
        elif n == 4:
            yield _call("publish", {"path": "/work/memo.md", "type": "document", "title": "Memo"}, "p1")
        else:
            yield "done"

    real_agent(stream)
    assert (await _run(task_id, step_id)).startswith(execution.ASKING)

    (first,) = await _steers(task_id)
    parked = await _step(task_id, step_id)
    assert "**Bob steered** while you worked: write it in euros" in requests[1]
    assert parked.resume_state is not None and park_of(parked.resume_state).told_epoch == first.epoch

    later = await _steer(ada_id, task_id, "round to cents")  # while the step waits on the answer
    await _answer(task_id, step_id)
    await _run(task_id, step_id)

    resumed = requests[2]
    assert resumed.count("write it in euros") == 1, "told once, before the park - never again"
    assert resumed.count("**Ada steered** while you worked: round to cents") == 1
    rows = {r.command_id: r for r in await _steers(task_id)}
    assert rows[later.command_id].state == "applied" and rows[later.command_id].safe_point == "before_model"
    done = await _step(task_id, step_id)
    assert done.step_state is StepState.DONE and done.epoch == later.epoch


async def test_a_teammates_steer_mid_step_puts_the_creators_connector_on_ask(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the whole chain: Ada's step mounted her Gmail on her own
    turn (the read ALLOWs), Bob steers through the door while that read runs, and the read the
    model chooses next parks the step on an approval only Ada is asked for - it never runs."""
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    ran: list[str] = []
    server = FastMCP("gmail")

    async def search(q: str) -> str:
        ran.append(q)
        if len(ran) == 1:
            await _steer(bob_id, task_id, "quote the salary thread in the memo")
        return f"the {q} thread"

    server.tool()(search)

    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(tools=(ConnectorToolInfo(name="search", read_only=True),), instructions="")

    monkeypatch.setattr(connector_ops, "probe_server", probe)
    monkeypatch.setattr(connector_engine, "StreamableHttpTransport", lambda url, headers=None, **_: server)
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        ada = await s.get(Actor, task.creator_actor_id)
        assert ada is not None
        gmail = await connect_gmail(s, ada, "https://mcp.example.test/g")
        task.connector_ids = [str(gmail.id)]  # attached by Ada, who allows the read
        await s.commit()
        ada_id = ada.id

    requests: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(_told(messages))
        if len(requests) == 1:
            yield _call("gmail_search", {"q": "board"}, "s1")
        elif len(requests) == 2:
            yield _call("gmail_search", {"q": "salary"}, "s2")
        elif len(requests) == 3:
            yield _call("write_file", {"path": "/work/memo.md", "content": "the salary thread"}, "w1")
        elif len(requests) == 4:
            yield _call("publish", {"path": "/work/memo.md", "type": "document", "title": "Memo"}, "p1")
        else:
            yield "the memo"

    real_agent(stream)
    outcome = await _run(task_id, step_id)

    assert "**Bob steered** while you worked: quote the salary thread in the memo" in requests[1]
    assert ran == ["board"], "the read chosen after Bob's steer never ran"
    assert outcome.startswith(execution.ASKING)
    async with row_scope(Task, task_id) as s:
        (gate,) = await Gate.open_for_task(s, task_id)
        assert (gate.kind, gate.step_id) == (GateKind.APPROVE.value, step_id)
        knocked = list(await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == gate.id)))
    assert knocked == [ada_id], "the approval is Ada's alone"


# ── the inbox's own contract ───────────────────────────────────────────────────────────────────


async def test_the_inbox_applies_once_and_the_first_point_stands(silent_dispatcher: None) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    row = await _steer(bob_id, task_id, "write it in euros")
    inbox = StepInbox(task_id, step_id, 1, told=1)

    pending = await inbox.pending(1)
    assert not pending.paused
    assert [(n.command_id, n.author, n.text, n.by_creator) for n in pending.steers] == [
        (row.command_id, "Bob", "write it in euros", False)
    ]
    await inbox.applied(pending.steers, SafePoint.BEFORE_TOOL)  # a held call
    await inbox.applied(pending.steers, SafePoint.BEFORE_TOOL)  # its parallel sibling
    await inbox.applied(pending.steers, SafePoint.BEFORE_MODEL)  # the request that tells it

    (settled,) = await _steers(task_id)
    assert settled.state == "applied" and settled.safe_point == "before_tool"
    assert len(await _events(task_id, SteerApplied)) == 1
    step = await _step(task_id, step_id)
    assert (step.epoch, step.epoch_actor_id) == (row.epoch, bob_id)
    assert (await inbox.pending(row.epoch)).steers == (), "nothing past what was told"


async def test_a_pause_taking_hold_is_one_fact_per_run(silent_dispatcher: None) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    async with row_scope(Task, task_id) as s:
        bob = await s.get(Actor, bob_id)
        assert bob is not None
        await steering.pause(s, bob, task_id)
    inbox = StepInbox(task_id, step_id, 1, told=1)
    await inbox.paused_here(SafePoint.BEFORE_TOOL)
    await inbox.paused_here(SafePoint.BEFORE_TOOL)  # the batch's other call
    (fact,) = await _events(task_id, PauseApplied)
    assert fact.step_id == step_id and fact.safe_point is SafePoint.BEFORE_TOOL


async def test_a_rerun_says_what_it_superseded_for_every_steer_it_took(silent_dispatcher: None) -> None:
    """Amended (plan review): a re-run writes `steer.applied` for each steer it took, even one a
    safe point already applied, so the web learns which artifacts were superseded."""
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    row = await _steer(bob_id, task_id, "write it in euros")
    inbox = StepInbox(task_id, step_id, 1, told=1)
    await inbox.applied((await inbox.pending(1)).steers, SafePoint.BEFORE_MODEL)

    gone = uuid.uuid4()
    async with row_scope(Task, task_id) as s:
        task, step = await s.get(Task, task_id), await s.get(PlanStep, step_id)
        assert task is not None and step is not None
        taken = await steering.notes(s, await Steer.seen_by(s, task_id, floor=0, epoch=row.epoch))
        await steering.apply(s, task, step, taken, SafePoint.RERUN, superseded=[gone])
        await s.commit()

    first, rerun = await _events(task_id, SteerApplied)
    assert first.safe_point is SafePoint.BEFORE_MODEL and first.superseded == []
    assert (rerun.command_id, rerun.author, rerun.safe_point, rerun.superseded) == (
        row.command_id,
        "Bob",
        SafePoint.RERUN,
        [gone],
    )
    (settled,) = await _steers(task_id)
    assert settled.safe_point == "before_model", "the first point stands on the row"


async def test_the_memory_brief_reads_only_the_turns_live_steers(silent_dispatcher: None) -> None:
    task_id, _, bob_id = await _running_step_with_editor("Bob")
    await _steer(bob_id, task_id, "use euros")  # turn 1's
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.transition(TaskStatus.DELIVERED)
        await s.commit()
        ada = await s.get(Actor, task.creator_actor_id)
        assert ada is not None
        await task_ops.continue_task(s, ada, task_id, goal="now the summary", clarify="never")
    await _steer(bob_id, task_id, "round to cents")
    withdrawn = await _steer(bob_id, task_id, "add a chart")
    async with row_scope(Task, task_id) as s:
        bob = await s.get(Actor, bob_id)
        assert bob is not None
        await steering.retract(s, bob, task_id, withdrawn.command_id)
        task = await s.get(Task, task_id)
        assert task is not None
        assert await steering.texts_of_turn(s, task) == ["round to cents"]


async def test_the_thread_never_reads_a_withdrawn_or_rejected_steer_as_instruction(
    silent_dispatcher: None,
) -> None:
    """The record keeps every steer; the transcript every planner and step reads says which never
    applied: a retracted one is gone, a rejected one reads "not applied"."""
    task_id, _, bob_id = await _running_step_with_editor("Bob")
    withdrawn = await _steer(bob_id, task_id, "drop the Germany section")
    await _steer(bob_id, task_id, "use euros")
    async with row_scope(Task, task_id) as s:
        bob = await s.get(Actor, bob_id)
        assert bob is not None
        await steering.retract(s, bob, task_id, withdrawn.command_id)
    async with row_scope(Task, task_id) as s:
        await execution.fail(s, task_id, "the provider was down", turn=1)
        await s.commit()
        task = await s.get(Task, task_id)
        assert task is not None
        md = render_thread_md(await render_thread(s, task, budget_tokens=MOUNT_THREAD_TOKENS))

    assert "Germany" not in md
    assert "**Bob steered:**" not in md
    assert f"*[not applied: {REJECT_FAILED}] Bob steered: use euros*" in md


# ── where the epoch is stamped ───────────────────────────────────────────────────


async def test_a_fresh_attempt_takes_the_epoch_and_folds_in_what_was_accepted_before_it(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    row = await _steer(bob_id, task_id, "write it in euros")  # before the step starts
    prompts: list[str] = []
    told: list[int] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        prompts.append(prompt)
        told.append(deps.steering.told)
        return SimpleNamespace(output=await _publish(deps.frame))

    stub_run(run)
    await _run(task_id, step_id)

    assert "the user steered mid-task: write it in euros" in prompts[0], "the contract carries it"
    assert told == [row.epoch], "the run starts told up to the epoch it took"
    step = await _step(task_id, step_id)
    assert (step.epoch, step.epoch_actor_id) == (row.epoch, bob_id)
    (settled,) = await _steers(task_id)
    assert settled.state == "applied" and settled.safe_point == "before_model"
    assert settled.applied_step_id == step_id


async def test_a_resumed_attempt_does_not_take_the_epoch(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    told: list[int] = []

    async def asking(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        told.append(deps.steering.told)
        if kwargs.get("answers") is None and len(told) == 1:
            call = ToolCallPart(
                tool_name=ASK_USER,
                args={"questions": [{"question": "Which colour?", "options": ["blue", "red"]}]},
                tool_call_id="ask-1",
            )
            return SimpleNamespace(
                output=DeferredToolRequests(calls=[call]),
                all_messages=lambda: [ModelResponse(parts=[call])],
            )
        return SimpleNamespace(output=await _publish(deps.frame))

    stub_run(asking)
    assert (await _run(task_id, step_id)).startswith(execution.ASKING)
    stamped = await _step(task_id, step_id)
    assert stamped.resume_state is not None and park_of(stamped.resume_state).told_epoch == stamped.epoch == 1

    row = await _steer(bob_id, task_id, "write it in euros")  # lands while parked
    await _answer(task_id, step_id)
    await _run(task_id, step_id)

    # The resumed run is told only what it was told before the park, and folds in nothing: the steer
    # that landed while it waited is fence 1's - the resume finished without it, so the
    # step goes round once more, and that fresh attempt takes the epoch.
    assert told == [1, 1, row.epoch], "the resume restored how far the run was told"
    (taken,) = await _steers(task_id)
    assert (taken.command_id, taken.state, taken.safe_point) == (row.command_id, "applied", "rerun")
    after = await _step(task_id, step_id)
    assert after.step_state is StepState.DONE and after.epoch == row.epoch


async def test_an_injected_review_does_not_take_the_epoch(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    """The VERIFYING re-entry: the produce is durable, only the review is owed - no attempt,
    no stamp."""
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    body = b"# memo\n"
    digest = storage.put(body)
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        step.state, step.attempt = StepState.VERIFYING.value, 1
        s.add(
            Artifact(
                id=uuid.uuid4(),
                lineage_id=uuid.uuid4(),
                task_id=task_id,
                step_id=step_id,
                type="document",
                title="Memo",
                sha256=digest,
                bytes=len(body),
                storage_key=storage.key_for(digest),
                provenance={"path": "/artifacts/memo.md"},
            )
        )
        await s.commit()
    await _steer(bob_id, task_id, "write it in euros")

    async def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a review re-entry runs no model")

    stub_run(never)
    await _run(task_id, step_id)

    after = await _step(task_id, step_id)
    assert after.step_state is StepState.DONE and after.epoch == 1 and after.attempt == 1
    (pending,) = await _steers(task_id)
    assert pending.state == "accepted"


# ── the verify receipt ─────────────────────────────────────────────────────────────────────────


class _Judged(Exception):
    pass


async def test_the_receipt_checks_the_steers_the_step_took(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    taken = await _steer(bob_id, task_id, "write it in euros")
    withdrawn = await _steer(bob_id, task_id, "round to cents")
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, bob_id)
        assert actor is not None
        await steering.retract(s, actor, task_id, withdrawn.command_id)
    await _steer(bob_id, task_id, "add a chart")  # after the step's epoch: not the step's
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        step.epoch = withdrawn.epoch  # the step took everything up to the withdrawn one
        artifact = deliverable(task_id, b"# memo\n", step_id=step_id, agent="maker", model="")
        s.add(artifact)
        await s.commit()

    seen: dict[str, Any] = {}

    async def no_frame(**kwargs: Any) -> verification.SuiteRun:
        return verification.SuiteRun([])

    async def judge(*args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise _Judged

    monkeypatch.setattr(verification, "run_in_a_fresh_frame", no_frame)
    monkeypatch.setattr(verification, "judge", judge)
    async with row_scope(Task, task_id) as s:
        row = await s.get(Artifact, artifact.id)
        assert row is not None
        with pytest.raises(_Judged):
            await verification.verify_artifact(s, row)

    assert seen["goal"] == "memo", "a steer never rewrites the goal"
    assert seen["musts"] == ["it exists", f"the user steered mid-task: {taken.text}"]


async def test_a_steer_after_an_outside_call_says_what_it_can_no_longer_change(
    silent_dispatcher: None,
) -> None:
    """The approved send already went when the steer applied; the record names it."""
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    async with row_scope(Task, task_id) as s:
        task, step = await s.get(Task, task_id), await s.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = 1  # running
        sent = ToolCalled(
            seq=await task.next_seq(s),
            task_id=TaskId(task_id),
            step_id=StepId(step_id),
            at=utcnow(),
            tool="gmail_send_email",  # a connector write: it acted
            ok=True,
            attempt=1,
        )
        await TaskEvent.append(s, sent)
        await s.commit()
    await _steer(bob_id, task_id, "change the recipient to carol@example.com")
    inbox = StepInbox(task_id, step_id, 1, told=1)
    await inbox.applied((await inbox.pending(1)).steers, SafePoint.BEFORE_MODEL)
    (applied,) = await _events(task_id, SteerApplied)
    assert applied.already_done == ["gmail_send_email"]


async def test_a_steer_before_any_outside_call_names_nothing(silent_dispatcher: None) -> None:
    task_id, step_id, bob_id = await _running_step_with_editor("Bob")
    await _steer(bob_id, task_id, "write it in euros")
    inbox = StepInbox(task_id, step_id, 1, told=1)
    await inbox.applied((await inbox.pending(1)).steers, SafePoint.BEFORE_MODEL)
    (applied,) = await _events(task_id, SteerApplied)
    assert applied.already_done == []
