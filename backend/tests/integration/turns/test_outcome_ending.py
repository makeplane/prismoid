# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The typed ending through the real ladder: a computer step's `done`
is proof-checked (one more request of the same run, then partial), a sign-in block on a Machine with a screen becomes
the hand-off, an approval block fails the step at once (never a retry), and so does `failed`.

The real `execute_step` against real rows, every frame a `FakeFrame` whose sidecar answers a page,
and the real `build()` with a scripted model (the `test_pause_park` pattern)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.conf import settings
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import CouldNotDo
from prismoid.operations import execution
from prismoid.operations.call_log import PgCallLog
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.machines import acquired
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import EscalationRaised, StepStateChanged
from prismoid.schemas.guard import BLOCKED_NEXT, FAILED_NEXT, FINISH_NOW, HANDED_OVER_NUDGE, HANDOFF_NUDGE
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.plan import GateKind, GateSpec, StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]
SITE = "https://admin.example.com/users"


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)


@pytest.fixture
def sidecar(fake_frames: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The Machine's sidecar: a snapshot answers the users page; anything else answers ok."""
    verbs: list[str] = []

    async def browser(self: Any, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        verbs.append(str(command.get("verb")))
        if command.get("verb") == "snapshot":
            tree = 'heading "Users"\n[e1.1] button "Save"'
            page = {"url": SITE, "title": "Users"}
            return {"ok": True, "epoch": 1, "text": tree, "written": tree, "reading": "Users Save", **page}
        if command.get("verb") == "inspect":
            return {"ok": True, "role": "", "name": "", "host": "admin.example.com", "path": "/users"}
        return {"ok": True}

    monkeypatch.setattr(fake_frames, "browser", browser)
    return verbs


@pytest.fixture
def real_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[Stream], None]:
    async def no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    monkeypatch.setattr(ex_harvest, "extract_previews", no_previews)

    def use(stream: Stream) -> None:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))

    return use


async def _computer_task() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = Task(
            id=uuid.uuid4(), goal="Save Mary's record", creator_actor_id=ada.id, criteria={}, inputs=[]
        )
        task.status = TaskStatus.RUNNING.value
        s.add(task)
        await s.flush()
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="save the record",
            spec=StepSpec(
                kind=StepKind.STEP,
                agent=AgentName("operator"),
                browser=True,
                title="save the record",
                contract=Contract(goal="Save Mary's record"),
            ).model_dump(mode="json"),
            attempt=0,
        )
        s.add(step)
        await s.commit()
        return task.id, step.id


async def _run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as s:
        return await execution.execute_step(s, task_id, step_id)


async def _step(task_id: uuid.UUID, step_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        s.expunge(step)
        return step


async def _frames(task_id: uuid.UUID) -> list[StepStateChanged]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, StepStateChanged)]


def _told(messages: list[ModelMessage]) -> str:
    """What the run was told: its prompt lines and the output checks' retried `done` returns."""
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart) or (isinstance(p, ToolReturnPart) and p.outcome == "retried")
    )


def _tool(name: str, call_id: str, **args: Any) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


def _driven(stream: Stream) -> Stream:
    """The run looks at the page before its scripted ending: an ending with no browser call is a
    guess `DRIVE_FIRST` answers first (output_checks.gave_up_undriven), not the wall these tests pin. The
    look is not counted among the stream's requests."""

    async def first_look(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        looked = any(
            isinstance(m, ModelResponse) and any(isinstance(p, ToolCallPart) for p in m.parts)
            for m in messages
        )
        if not looked:
            yield _tool("browser_snapshot", "look")
            return
        async for chunk in stream(messages, info):
            yield chunk

    return first_look


async def test_a_done_the_page_shows_is_done(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool(
            "done",
            "d1",
            status="done",
            summary="Saved Mary's record.",
            proof={"url": SITE, "page_text": "save"},
        )

    real_agent(stream)
    assert await _run(task_id, step_id) == "Saved Mary's record."
    assert len(requests) == 1 and "snapshot" in sidecar, "the system looked, the model was asked nothing more"
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
    assert [f.outcome for f in await _frames(task_id) if f.state is StepState.DONE] == ["done"]


async def test_a_done_the_page_does_not_show_gets_one_more_turn_then_reads_partial(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool(
            "done",
            f"d{len(requests)}",
            status="done",
            summary="Saved Mary's record.",
            proof={"url": SITE, "page_text": "Record saved"},
        )

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert len(requests) == 2, "one more turn with the reason, then the system's word"
    assert "'Record saved' is not on the page now" in _told(requests[1])
    assert said.startswith("**Partly done.** Saved Mary's record.") and "Not confirmed:" in said
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
    assert [f.outcome for f in await _frames(task_id) if f.state is StepState.DONE] == ["partial"]


class _Killed(BaseException):
    """A worker dying mid-request, in process: nothing in the engine catches it."""


async def test_a_proof_nudge_is_a_request_of_the_same_run_and_a_death_after_it_buys_nothing_twice(
    fake_frames: Any,
    sidecar: list[str],
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proof nudge is one more request of the run, in its one segment, and the proof check is a
    logged read: a worker that dies at the second check re-enters the step and replays the run, the
    first check and the nudge, buying none of them again."""
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []
    look, dies = fake_frames.browser, [_Killed()]

    async def dies_at_the_second_proof(self: Any, command: dict[str, Any], *rest: Any, **named: Any) -> Any:
        if command.get("verb") == "snapshot" and sidecar.count("snapshot") == 1 and dies:
            raise dies.pop()
        return await look(self, command, *rest, **named)

    monkeypatch.setattr(fake_frames, "browser", dies_at_the_second_proof)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool(
            "done",
            f"d{len(requests)}",
            status="done",
            summary="Saved Mary's record.",
            proof={"url": SITE, "page_text": "Record saved"},
        )

    real_agent(stream)
    with pytest.raises(_Killed):
        await _run(task_id, step_id)
    assert len(requests) == 2, "the run and its nudge answered before the death"
    said = await _run(task_id, step_id)
    assert said.startswith("**Partly done.**") and len(requests) == 2, "neither bought again"
    assert sidecar.count("snapshot") == 2, "the first check replayed from the log, the second looked"
    step = await _step(task_id, step_id)
    assert step.step_state is StepState.DONE and step.attempt == 1
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        rows = (await PgCallLog(task.workspace_id).latest(StepId(step_id))).entries
    payloads = [
        e.payload for (unit, phase), e in rows.items() if unit.startswith("segment#") and phase == "intent"
    ]
    keys = [cast(dict[str, Any], p)["key"] for p in payloads if isinstance(p, dict) and "key" in p]
    assert keys == ["prompt"], "the nudge is no segment of its own"


async def test_a_sign_in_block_on_a_machine_with_a_screen_becomes_the_hand_off(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            yield _tool(
                "done", "d1", status="blocked", blocked_by="login", summary="The admin needs a sign-in."
            )
        else:
            yield _tool("browser_handoff", "h1", reason="sign in to admin.example.com")

    real_agent(_driven(stream))
    said = await _run(task_id, step_id)
    assert said.startswith(execution.ASKING)
    assert HANDOFF_NUDGE in _told(requests[1])
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, uuid.UUID(said.removeprefix(execution.ASKING)))
        assert gate is not None
        spec = GateSpec.model_validate(gate.spec)
    assert spec.kind is GateKind.CONTROL and spec.source == "handoff"
    parked = [f for f in await _frames(task_id) if f.outcome == "blocked"]
    assert [(f.state, f.blocked_by) for f in parked] == [(StepState.RUNNING, "login")], (
        "the row says it while parked"
    )


async def test_an_approval_block_fails_the_step_at_once_never_a_retry(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool(
            "done", "d1", status="blocked", blocked_by="approval", summary="Deleting needs the owner's OK."
        )

    real_agent(stream)
    with pytest.raises(CouldNotDo, match="approval needed"):
        await _run(task_id, step_id)
    assert len(requests) == 1
    step = await _step(task_id, step_id)
    assert step.step_state is StepState.ESCALATED and step.attempt == 1
    (escalated,) = [f for f in await _frames(task_id) if f.state is StepState.ESCALATED]
    assert escalated.outcome == "blocked" and escalated.blocked_by == "approval"


async def test_a_failed_ending_fails_the_step_at_once_in_the_agents_own_words(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """`failed` is the agent's own verdict that it cannot be done this way: another attempt of the
    same conversation reaches it again, so the step ends now and the person reads why."""
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []
    said = "Nothing saved: the page has no Save button for this record."

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool("done", "d1", status="failed", summary=said)

    real_agent(_driven(stream))
    with pytest.raises(CouldNotDo, match="no Save button for this record"):
        await _run(task_id, step_id)
    assert len(requests) == 1, "never a second attempt of the same verdict"
    step = await _step(task_id, step_id)
    assert step.step_state is StepState.ESCALATED and step.attempt == 1
    frames = await _frames(task_id)
    assert not [f for f in frames if f.state in (StepState.RETRYING, StepState.DONE)]
    (escalated,) = [f for f in frames if f.state is StepState.ESCALATED]
    assert escalated.outcome == "failed" and escalated.detail == said
    async with row_scope(Task, task_id) as s:
        (raised,) = [
            e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, EscalationRaised)
        ]
    diagnosis = raised.diagnosis
    assert diagnosis.what_failed == "Could not be done" and diagnosis.why == said
    assert diagnosis.suggested_next == [FAILED_NEXT]
    assert not any("Run it again" in line for line in diagnosis.suggested_next)


# ── the review's pins ─────────────────────────────────────────────────────────────


async def test_a_sign_in_block_on_a_machine_without_a_screen_fails_with_the_summary(
    fake_frames: Any,
    sidecar: list[str],
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody can take this browser over: no nudge, no gate, no retry - and never a DONE."""
    monkeypatch.setattr(acquired, "has_screen", lambda profile, driver: False)
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool("done", "d1", status="blocked", blocked_by="login", summary="The admin needs a sign-in.")

    real_agent(_driven(stream))
    with pytest.raises(CouldNotDo, match=r"sign-in needed\): The admin needs a sign-in\."):
        await _run(task_id, step_id)
    assert len(requests) == 1, "no hand-off nudge without a screen"
    step = await _step(task_id, step_id)
    assert step.step_state is StepState.ESCALATED and step.attempt == 1
    frames = await _frames(task_id)
    assert not [f for f in frames if f.state is StepState.DONE]
    (escalated,) = [f for f in frames if f.state is StepState.ESCALATED]
    assert (escalated.outcome, escalated.blocked_by) == ("blocked", "login")
    assert "The admin needs a sign-in." in escalated.detail, "the summary is the diagnosis"
    async with row_scope(Task, task_id) as s:
        (raised,) = [
            e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, EscalationRaised)
        ]
    diagnosis = raised.diagnosis
    assert diagnosis.what_failed == "Blocked: sign-in needed"  # the wall, never the agent's id
    assert diagnosis.why == "The admin needs a sign-in."
    assert diagnosis.suggested_next == [BLOCKED_NEXT["login"]]
    assert "could not produce" not in diagnosis.what_failed and "each one failed" not in diagnosis.why


async def test_a_blocked_that_names_no_wall_still_fails_with_the_summary(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """`Outcome` does not tie `blocked_by` to `blocked`: a bare block is still a block, never DONE."""
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool("done", "d1", status="blocked", summary="The page is behind a wall I cannot name.")

    real_agent(_driven(stream))
    with pytest.raises(CouldNotDo, match="cannot name"):
        await _run(task_id, step_id)
    assert len(requests) == 1
    step = await _step(task_id, step_id)
    assert step.step_state is StepState.ESCALATED and step.attempt == 1
    frames = await _frames(task_id)
    assert not [f for f in frames if f.state is StepState.DONE]
    (escalated,) = [f for f in frames if f.state is StepState.ESCALATED]
    assert (escalated.outcome, escalated.blocked_by) == ("blocked", None)
    assert "cannot name" in escalated.detail


async def test_a_zero_action_done_never_reads_done(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """A `done` with no proof, no publish and no command: one more turn, then Partly done."""
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        yield _tool("done", f"d{len(requests)}", status="done", summary="Saved Mary's record.")

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert len(requests) == 2
    assert "nothing shows it: there is no proof" in _told(requests[1])
    assert said.startswith("**Partly done.**") and "Not confirmed:" in said
    assert [f.outcome for f in await _frames(task_id) if f.state is StepState.DONE] == ["partial"]


async def test_a_done_naming_a_wall_is_still_done(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """A stray `blocked_by` on a proven `done` is ignored: the row says done, not blocked."""
    task_id, step_id = await _computer_task()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        yield _tool(
            "done",
            "d1",
            status="done",
            blocked_by="login",
            summary="Saved Mary's record.",
            proof={"url": SITE, "page_text": "Users"},
        )

    real_agent(stream)
    assert await _run(task_id, step_id) == "Saved Mary's record."
    (done,) = [f for f in await _frames(task_id) if f.state is StepState.DONE]
    assert (done.outcome, done.blocked_by) == ("done", None)


async def test_a_done_the_hand_off_turn_claims_is_checked_with_no_turn_left(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """The nudge's one turn ends on an unproven `done` instead of the hand-off: Partly done, no third turn."""
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            yield _tool(
                "done", "d1", status="blocked", blocked_by="login", summary="The admin needs a sign-in."
            )
        else:
            yield _tool("done", "d2", status="done", summary="Signed in after all.")

    real_agent(_driven(stream))
    said = await _run(task_id, step_id)
    assert len(requests) == 2
    assert said.startswith("**Partly done.** Signed in after all.")
    assert [f.outcome for f in await _frames(task_id) if f.state is StepState.DONE] == ["partial"]


async def test_a_sign_in_block_past_the_hand_off_budget_escalates_without_a_nudge(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """The hand-off budget is spent: `browser_handoff` answers itself, and the block that follows is
    never nudged toward another hand-off (that loop never ends): it is told once to take another way, then
    escalates with the summary."""
    task_id, step_id = await _computer_task()
    async with row_scope(Task, task_id) as s:
        for _ in range(execution.MAX_STEP_HANDOFF_GATES):
            s.add(
                Gate(
                    task_id=task_id,
                    step_id=step_id,
                    kind=GateKind.CONTROL.value,
                    state="resolved",
                    spec={"source": "handoff"},
                )
            )
        await s.commit()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if len(requests) == 1:
            yield _tool("browser_handoff", "h1", reason="sign in to admin.example.com")
        else:
            yield _tool(
                "done", f"d{len(requests)}", status="blocked", blocked_by="login", summary="Still signed out."
            )

    real_agent(stream)
    with pytest.raises(CouldNotDo, match="sign-in needed"):
        await _run(task_id, step_id)
    assert len(requests) == 3, (
        "the hand-off answered itself once; one nudge to another way, never to hand over"
    )
    assert not any(HANDOFF_NUDGE in _told(r) for r in requests)
    assert HANDED_OVER_NUDGE in _told(requests[-1])
    step = await _step(task_id, step_id)
    assert step.step_state is StepState.ESCALATED and step.attempt == 1


async def test_a_stalled_sign_in_wall_can_still_hand_off(
    fake_frames: Any, sidecar: list[str], silent_dispatcher: None, real_agent: Callable[[Stream], None]
) -> None:
    """Two stalls tell the run to finish and hold its next look - but a hand-off ends the run as a
    park, a way to finish: the nudged `browser_handoff` is never held, the step parks on CONTROL."""
    task_id, step_id = await _computer_task()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        n = len(requests)
        if n <= 8:  # looks at a page that never moves: stalls at 4 and 7, the eighth is held
            yield _tool("browser_snapshot", f"s{n}")
        elif n == 9:
            yield _tool(
                "done", "d1", status="blocked", blocked_by="login", summary="The admin needs a sign-in."
            )
        else:
            yield _tool("browser_handoff", "h1", reason="sign in to admin.example.com")

    real_agent(stream)
    said = await _run(task_id, step_id)
    assert len(requests) == 10, "the nudged hand-off ran at once - never held into a loop"
    assert FINISH_NOW in _told(requests[7]), "the run was told to finish"
    assert sidecar.count("snapshot") == 7, "the eighth look was held"
    assert HANDOFF_NUDGE in _told(requests[9])
    assert said.startswith(execution.ASKING)
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, uuid.UUID(said.removeprefix(execution.ASKING)))
        assert gate is not None
        spec = GateSpec.model_validate(gate.spec)
    assert spec.kind is GateKind.CONTROL and spec.source == "handoff"


@pytest.mark.parametrize(
    ("ending", "success"),
    [
        ({"status": "blocked", "blocked_by": "approval", "summary": "Deleting needs the owner's OK."}, False),
        (
            {
                "status": "done",
                "summary": "Saved Mary's record.",
                "proof": {"url": SITE, "page_text": "save"},
            },
            True,
        ),
    ],
)
async def test_a_blocked_steps_recording_is_never_its_deliverable(
    ending: dict[str, Any],
    success: bool,
    fake_frames: Any,
    sidecar: list[str],
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recording is the deliverable only of a step that succeeded: a blocked step's trail is
    evidence of the wall (`working`), published as a failure's would be."""
    recorded: list[bool] = []

    async def record(*args: Any, success: bool, **kwargs: Any) -> list[Any]:
        recorded.append(success)
        return []

    monkeypatch.setattr(ex_harvest, "record_evidence", record)
    task_id, step_id = await _computer_task()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        yield _tool("done", "d1", **ending)

    real_agent(stream)
    if success:
        await _run(task_id, step_id)
    else:
        with pytest.raises(CouldNotDo, match="approval needed"):
            await _run(task_id, step_id)
    assert recorded == [success]
