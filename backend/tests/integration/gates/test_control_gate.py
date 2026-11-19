# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The agent yields: a person driving the browser parks the step's next browser call
on a CONTROL gate, `browser.handoff` asks for one, and the lease's ending answers the gate.

The real `execute_step` against real rows, every frame a `FakeFrame` whose sidecar answers a
snapshot, and the model a scripted streaming `FunctionModel` wired with the REAL browser toolset
(the `test_navigator_e2e.py` pattern) - so the deferral is raised inside the verb, where the hook
lives, and the resume's text is what the model actually reads. Leases are claimed and released
through the operations, so the gate is answered by the real ending."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls
from sqlalchemy import select

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, StepFailed
from prismoid.operations import device_gates, execution, gates, machines, steering
from prismoid.operations.machines import screen as machine_screen
from prismoid.schemas.events import MachineControlEnded, StepStateChanged
from prismoid.schemas.ids import StepId
from prismoid.schemas.machines import MachineState
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, GateState, StepState, TaskStatus
from prismoid.schemas.screen import ControlEnd, ControlState
from tests.support.cast import bound_scope
from tests.support.dispatchers import RecordingDispatcher
from tests.support.rigs.control_gate_rig import (
    HANDED_BACK,
    Turn,
    gate_row,
    machine_row,
    navigator_task,
    parked_on,
    resolve_gate,
    resume_step,
    run_step,
    tool_call,
    wake_of,
)
from tests.support.rigs.control_gate_rig import computer_use_on as computer_use_on
from tests.support.rigs.control_gate_rig import engine as engine
from tests.support.rigs.control_gate_rig import sidecar as sidecar

pytestmark = pytest.mark.integration

_D = r"\d+[hms]( \d+[hms])?"  # held_text: "45s", "1m 40s", "2h 3m"
TAKEN_OVER = re.compile(
    rf"^Ada drove the browser for {_D} and handed back control\. The page may have changed: take a "
    r"new snapshot before acting\.$"
)
NOBODY = "Nobody took over ({why}). Continue without it if you can, or stop and say what is needed."


def _calls(*calls: tuple[str, str, dict[str, Any]]) -> DeltaToolCalls:
    """Several tool calls in ONE model turn: (tool, call id, args) each."""
    return {
        i: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id=call_id)
        for i, (tool, call_id, args) in enumerate(calls)
    }


async def _claim(
    task_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID, *, private: bool = False
) -> None:
    async with bound_scope(workspace_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        await machine_screen.claim(s, actor, task_id, conn="ada_tab_0001", private=private)


async def _release(task_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    async with bound_scope(workspace_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        await machine_screen.release(s, actor, task_id)


async def _knocked(task_id: uuid.UUID, gate_id: uuid.UUID) -> bool:
    return bool(await _knocks(task_id, gate_id))


async def _knocks(task_id: uuid.UUID, gate_id: uuid.UUID) -> list[tuple[str, str]]:
    """(title, body) of every inbox item the gate knocked."""
    async with row_scope(Task, task_id) as s:
        rows = await s.execute(select(InboxItem.title, InboxItem.body).where(InboxItem.gate_id == gate_id))
        return [(title, body) for title, body in rows.tuples()]


async def _machine_state(task_id: uuid.UUID) -> str:
    return (await machine_row(task_id))[1]


async def _kinds(task_id: uuid.UUID) -> list[str]:
    async with row_scope(Task, task_id) as s:
        return [str(e.type) for e in await TaskEvent.replay(s, task_id)]


async def _spent(task_id: uuid.UUID, step_id: uuid.UUID, *, source: str, n: int) -> None:
    """`n` earlier CONTROL gates of `source` on this step, already resolved."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        for _ in range(n):
            gate = await gates.open_control(
                s, task, StepId(step_id), source=source, reason="sign in to admin.example.com", holder="Ada"
            )
            gate.state = GateState.RESOLVED.value
        await s.commit()


def _gate_wakes(dispatcher: RecordingDispatcher) -> list[object]:
    return [payload for _, payload, topic in dispatcher.sends if topic == gates.GATE_TOPIC]


async def _only_gate(task_id: uuid.UUID) -> Gate:
    async with row_scope(Task, task_id) as s:
        [gate] = await Gate.for_task(s, task_id)
        s.expunge(gate)
        return gate


async def _last_ending(task_id: uuid.UUID) -> MachineControlEnded:
    async with row_scope(Task, task_id) as s:
        ended = await TaskEvent.latest_of_type(s, task_id, "machine.control_ended")
    assert isinstance(ended, MachineControlEnded)
    return ended


async def _lease_columns(task_id: uuid.UUID, **columns: dt.timedelta) -> None:
    """Move the lease's clock columns to Postgres now() plus the given offsets."""
    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None and row.control_since is not None
        now = await Machine.db_now(s)
        for name, offset in columns.items():
            setattr(row, name, now + offset)
        await s.commit()


def _ends_right_after_the_hook(monkeypatch: pytest.MonkeyPatch, end: Callable[[], Awaitable[None]]) -> None:
    """The race: the browser hook reads a LIVE lease (the real read), and the lease
    ends before the CONTROL gate commits - so its ending finds no gate to answer."""
    real = machine_screen.control_state

    async def racing(task: uuid.UUID) -> ControlState | None:
        seen = await real(task)
        if seen is not None:
            await end()
        return seen

    monkeypatch.setattr(machines.screen, "control_state", racing)


async def _resumed_lines(task_id: uuid.UUID) -> list[str]:
    """Every `step.state{running}` anchor's detail that says the step resumed."""
    async with row_scope(Task, task_id) as s:
        events = await TaskEvent.replay(s, task_id)
    return [
        e.detail
        for e in events
        if isinstance(e, StepStateChanged) and e.state is StepState.RUNNING and e.detail.startswith("resumed")
    ]


async def test_a_claim_parks_the_next_browser_call_and_the_release_resumes_it(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_snapshot", "snap-1")
        if len(returns) == 1:
            await _claim(task_id, workspace_id, ada_id)  # Ada takes control between the agent's calls
            return tool_call("browser_snapshot", "snap-2")
        told.append(str(returns[-1].content))
        return "Done: Mary Jackson is open."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    assert sidecar.count("snapshot") == 1, "the held call never reached the sidecar"
    assert "hold" in sidecar, "the park pinned Chromium alive"
    assert await _machine_state(task_id) == MachineState.PARKED

    gate = await gate_row(task_id, gate_id)
    spec = GateSpec.model_validate(gate.spec)
    assert gate.kind == GateKind.CONTROL.value and spec.source == "takeover" and spec.questions is None
    assert spec.title == "Ada took control of the browser"
    assert gate.expires_at is not None
    assert abs((gate.expires_at - utcnow()).total_seconds() - settings.computer_use.control_wait_s) < 60
    assert not await _knocked(task_id, gate_id), "a takeover knocks no inbox: the people concerned are there"

    await _release(task_id, workspace_id, ada_id)
    wake = wake_of(recording_dispatcher, gate_id)
    assert wake["action"] == "accept"
    assert wake["content"]["outcome"] == "accepted" and wake["content"]["holder"] == "Ada"
    assert wake["content"]["reason"] == "released"

    said = await resume_step(task_id, step_id, wake)
    assert not said.startswith(execution.ASKING)
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told
    assert sidecar.count("snapshot") == 1, "the parked call never ran, not even on resume"
    assert await _machine_state(task_id) == MachineState.IDLE
    kinds = await _kinds(task_id)
    for kind in ("machine.control_claimed", "gate.opened", "machine.control_ended", "gate.resolved"):
        assert kind in kinds, kinds


async def test_a_handoff_opens_a_knocking_gate_and_a_private_sign_in_answers_it(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    spec = GateSpec.model_validate((await gate_row(task_id, gate_id)).spec)
    assert spec.kind is GateKind.CONTROL and spec.source == "handoff"
    # The agent's words stay quoted as the agent's; where the page is, the sidecar says.
    assert spec.title == 'The agent needs someone to "sign in to admin.example.com"'
    assert spec.reason == "sign in to admin.example.com"
    knocks = await _knocks(task_id, gate_id)
    assert knocks, "a handoff knocks the inbox, as an ask does"
    assert {body for _, body in knocks} == {"The browser is on accounts.example.org."}
    assert "handoff" not in sidecar and sidecar.count("inspect") == 1, "a handoff only reads the page"

    await _claim(task_id, workspace_id, ada_id, private=True)
    await _release(task_id, workspace_id, ada_id)
    said = await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert not said.startswith(execution.ASKING)
    assert len(told) == 1 and HANDED_BACK.match(told[0]), told


async def test_skip_declines_with_the_nobody_text(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        told.append(str(returns[-1].content))
        return "I could not sign in: it needs credentials."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    resolution = await resolve_gate(workspace_id, ada_id, gate_id, GateAction.DECLINE)
    assert resolution.action is GateAction.DECLINE
    assert resolution.content == {"outcome": "declined", "by": "Ada"}
    await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert told == [NOBODY.format(why="Ada skipped")]


async def test_the_ttl_declines_with_the_nobody_came_text(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, _, _ = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        told.append(str(returns[-1].content))
        return "Nobody came; stopping."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, gate_id)
        assert gate is not None
        gate.expires_at = utcnow() - dt.timedelta(minutes=1)
        await s.commit()
    async with session_scope() as s:
        assert gate_id in await gates.sweep(s)
    wake = wake_of(recording_dispatcher, gate_id)
    assert wake["action"] == "decline" and wake["content"] == {"outcome": "timeout"}
    await resume_step(task_id, step_id, wake)
    minutes = settings.computer_use.control_wait_s // 60
    assert told == [NOBODY.format(why=f"no one came in {minutes} minutes")]


async def test_the_ttl_waits_while_someone_still_drives(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    """A lease renews without limit, so a long sign-in can outlive the gate's TTL. The clock must
    not tell the agent nobody came while Ada is still at the browser (Skip refuses exactly that):
    the gate's expiry moves on by `control_wait_s`, and her hand-back answers it."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    await _claim(task_id, workspace_id, ada_id, private=True)
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, gate_id)
        assert gate is not None
        gate.expires_at = utcnow() - dt.timedelta(minutes=1)
        await s.commit()
    async with session_scope() as s:
        assert gate_id not in await gates.sweep(s)
    gate = await gate_row(task_id, gate_id)
    assert gate.state == GateState.OPEN.value and gate.expires_at is not None
    wait = dt.timedelta(seconds=settings.computer_use.control_wait_s)
    assert gate.expires_at > utcnow() + wait - dt.timedelta(minutes=1)
    assert _gate_wakes(recording_dispatcher) == []

    await _release(task_id, workspace_id, ada_id)
    await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert len(told) == 1 and HANDED_BACK.match(told[0]), told


async def test_the_kill_switch_ending_declines_the_gate_never_accepts_it(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    """A lease the switch ends (`disabled`) DECLINES the open CONTROL gate with the
    switch's reason - nobody handed anything back, so the resume never says someone drove."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Stopped: computer use is off."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    machine_id, _ = await machine_row(task_id)
    assert await machine_screen.end_live(machine_id, ada_id, reason=ControlEnd.DISABLED)

    gate = await gate_row(task_id, gate_id)
    assert gate.state == GateState.RESOLVED.value and gate.resolution is not None
    assert gate.resolution["action"] == "decline"
    assert gate.resolution["content"] == {"outcome": "disabled", "reason": machine_screen.OFF}
    wake = wake_of(recording_dispatcher, gate_id)
    assert wake["action"] == "decline"
    await resume_step(task_id, step_id, wake)
    assert told == [NOBODY.format(why="computer use is off")]
    assert sidecar.count("snapshot") == 0


async def test_the_fourth_handoff_in_a_step_answers_itself(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, _, _ = await navigator_task()
    await _spent(task_id, step_id, source="handoff", n=execution.MAX_STEP_HANDOFF_GATES)
    told: dict[str, str] = {}

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            why = {"reason": "sign in to admin.example.com"}
            return _calls(("browser_handoff", "hand-4", why), ("browser_handoff", "hand-5", why))
        told.update({r.tool_call_id: str(r.content) for r in returns})
        return "Stopping: someone must sign in to admin.example.com."

    engine(turn)
    said = await run_step(task_id, step_id)
    assert not said.startswith(execution.ASKING), "past the budget the verb answers itself, in place"
    # One handoff is refused with the budget's words; the other was never run for a person - it
    # shared the turn with the refused one, and says so (every pending id answered).
    assert sorted(told.values()) == sorted([execution.OUT_OF_HANDOFFS, execution.NOT_RUN_BESIDE_HANDOFF])
    async with row_scope(Task, task_id) as s:
        count = await Gate.count_for_step(s, step_id, kind=GateKind.CONTROL.value, source="handoff")
        assert count == execution.MAX_STEP_HANDOFF_GATES, "no fourth gate"
        assert not await Gate.open_for_task(s, task_id)


async def test_a_person_taking_control_spends_no_handoff_budget(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, _, _ = await navigator_task()
    await _spent(task_id, step_id, source="takeover", n=execution.MAX_STEP_HANDOFF_GATES)

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    assert GateSpec.model_validate((await gate_row(task_id, gate_id)).spec).source == "handoff"


async def test_nobody_accepts_a_control_gate_by_hand_and_skip_waits_for_the_driver(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    task_id, step_id, ada_id, workspace_id = await navigator_task()

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    with pytest.raises(Conflict, match=re.escape(device_gates.TAKE_CONTROL_INSTEAD)):
        await resolve_gate(workspace_id, ada_id, gate_id, GateAction.ACCEPT)
    await _claim(task_id, workspace_id, ada_id)
    with pytest.raises(Conflict, match=re.escape("Ada is driving the browser.")):
        await resolve_gate(workspace_id, ada_id, gate_id, GateAction.DECLINE)
    await _release(task_id, workspace_id, ada_id)
    late = await resolve_gate(workspace_id, ada_id, gate_id, GateAction.DECLINE)
    assert late.action is GateAction.ACCEPT, "the hand-back answered first; a late Skip is handed it"


async def test_a_lease_that_ends_before_the_gate_commits_settles_it_in_place_without_a_wake(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race: the hook read Ada's live lease, and she handed back before the CONTROL
    gate committed - so her ending found no gate. The park asks how that lease ended (the recorded
    `machine.control_ended`), answers the gate with exactly that, sends NO wake (the parked
    workflow is the one resolving it: a send there would be journaled inside `run_step`), and the
    same `execute_step` call resumes the run with the takeover text."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    _ends_right_after_the_hook(monkeypatch, lambda: _release(task_id, workspace_id, ada_id))
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    said = await run_step(task_id, step_id)
    assert not said.startswith(execution.ASKING), "a settled race resumes in the same execute_step call"
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told
    assert _gate_wakes(recording_dispatcher) == [], "no DBOS message: the workflow resolved its own gate"
    assert sidecar.count("snapshot") == 0, "the parked call never ran"

    gate = await _only_gate(task_id)
    ended = await _last_ending(task_id)
    assert gate.kind == GateKind.CONTROL.value and gate.state == GateState.RESOLVED.value
    assert gate.resolution is not None and gate.resolution["action"] == "accept"
    assert gate.resolution["resolved_by"] == gates.CONTROL_ENDED_BY
    content = gate.resolution["content"]
    assert content["holder"] == "Ada"
    assert (
        (content["reason"], content["held_s"]) == (ended.reason, ended.held_s) == ("released", ended.held_s)
    )
    assert await _machine_state(task_id) == MachineState.IDLE
    kinds = await _kinds(task_id)
    assert kinds.count("gate.opened") == 1 and kinds.count("gate.resolved") == 1
    assert await _resumed_lines(task_id) == ["resumed: the browser was handed back"]


async def test_a_lease_that_runs_out_before_the_gate_commits_is_said_by_its_own_clock(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race's other shape: the lease's clock ran out after the hook's read and nothing has
    ended it yet (the lease-end job is a sweep away). The gate says what that job will record:
    `expired`, held until its `until` (`held_seconds`), never until now."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    _ends_right_after_the_hook(
        monkeypatch, lambda: _lease_columns(task_id, control_until=-dt.timedelta(seconds=1))
    )
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            await _lease_columns(task_id, control_since=-dt.timedelta(seconds=40))  # she drove 40 s
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    said = await run_step(task_id, step_id)
    assert not said.startswith(execution.ASKING)
    assert _gate_wakes(recording_dispatcher) == []
    gate = await _only_gate(task_id)
    assert gate.resolution is not None and gate.resolution["action"] == "accept"
    content = gate.resolution["content"]
    assert content["reason"] == "expired" and 38 <= content["held_s"] <= 40, content
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told


async def test_the_race_settle_knows_the_lease_by_its_claim_never_by_its_holders_name(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook one-lines the holder's name for the model; the recorded ending carries the name
    as the account has it. A name with a control character (no filter keeps one out) must not
    hide the ending: the settle matches the lease's `since`, and resumes in place."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    async with bound_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        ada.display_name = "Ada\x85Lovelace"
        await s.commit()
    _ends_right_after_the_hook(monkeypatch, lambda: _release(task_id, workspace_id, ada_id))
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    said = await run_step(task_id, step_id)
    assert not said.startswith(execution.ASKING), "the ending was recorded: the race settles in place"
    ended = await _last_ending(task_id)
    gate = await _only_gate(task_id)
    assert gate.resolution is not None and gate.resolution["action"] == "accept"
    assert ended.lease_since is not None
    assert (gate.resolution["content"]["reason"], gate.resolution["content"]["held_s"]) == (
        ended.reason,
        ended.held_s,
    )
    assert _gate_wakes(recording_dispatcher) == [] and len(told) == 1


async def test_a_race_whose_ending_was_never_recorded_still_settles(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ending's record is best-effort, and its gate answer already ran before the gate
    committed. The row holds no live lease, so the hook's lease is over: the settle says what it
    knows instead of leaving the agent parked for the whole TTL."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    real_record = machine_screen.record_after

    async def lossy(task: uuid.UUID, event_type: type[Any], **fields: Any) -> None:
        if event_type is not MachineControlEnded:
            await real_record(task, event_type, **fields)

    monkeypatch.setattr(machine_screen, "record_after", lossy)
    _ends_right_after_the_hook(monkeypatch, lambda: _release(task_id, workspace_id, ada_id))
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    said = await run_step(task_id, step_id)
    assert not said.startswith(execution.ASKING)
    gate = await _only_gate(task_id)
    assert gate.resolution is not None and gate.resolution["action"] == "accept"
    assert gate.resolution["content"]["holder"] == "Ada"
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told


async def test_an_unrecorded_race_under_the_kill_switch_declines_never_hands_back(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch ends the lease in the race window and its record is lost: the settle cannot
    know the reason, but it must not guess a hand-back that resumes a browser computer use no
    longer allows - with the switch off it declines, as the switch's own ending would have."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    real_record = machine_screen.record_after

    async def lossy(task: uuid.UUID, event_type: type[Any], **fields: Any) -> None:
        if event_type is not MachineControlEnded:
            await real_record(task, event_type, **fields)

    async def switched_off() -> None:
        monkeypatch.setattr(settings.computer_use, "enabled", False)
        machine_id, _ = await machine_row(task_id)
        assert await machine_screen.end_live(machine_id, ada_id, reason=ControlEnd.DISABLED)

    monkeypatch.setattr(machine_screen, "record_after", lossy)
    _ends_right_after_the_hook(monkeypatch, switched_off)
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Stopped: computer use is off."

    engine(turn)
    with pytest.raises(StepFailed):  # computer use is off: the step cannot finish, by design
        await run_step(task_id, step_id)
    gate = await _only_gate(task_id)
    assert gate.resolution is not None and gate.resolution["action"] == "decline"
    assert gate.resolution["content"]["outcome"] == "disabled"
    assert told in ([], [NOBODY.format(why="computer use is off")])
    assert sidecar.count("snapshot") == 0


async def test_every_other_held_call_of_the_turn_is_answered_on_resume(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    """Two browser calls in one turn under a live lease: ONE parks the step, the other rides
    `held_calls`. On the hand-back the resume answers every pending id - pydantic-ai refuses a
    partial result set - and the run goes on to its final text."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: dict[str, str] = {}

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return _calls(
                ("browser_snapshot", "snap-1", {}), ("browser_scroll", "scroll-1", {"direction": "down"})
            )
        told.update({r.tool_call_id: str(r.content) for r in returns})
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    await _release(task_id, workspace_id, ada_id)
    said = await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert not said.startswith(execution.ASKING), "the full result set was accepted and the run finished"
    assert set(told) == {"snap-1", "scroll-1"}
    (parked,) = [c for c, text in told.items() if TAKEN_OVER.match(text)]
    (held,) = [c for c in told if c != parked]
    assert told[held] == execution.NOT_RUN_FOR_CONTROL
    assert "snapshot" not in sidecar and "scroll" not in sidecar, "neither held call ever ran"


async def test_a_lease_that_ran_out_before_a_handoff_never_answers_it(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    """A run-out lease stays on the row until something ends it. Ada's ran out, THEN the agent
    asked for a person; Ada presses Take control, and the claim ends her old lease first
    (`expired`). That late ending was over before the handoff opened, so it never answers it - her
    new lease's hand-back does."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            # Ran out ten seconds ago, and nothing has ended it yet (the sweep is a tick away).
            await _lease_columns(
                task_id, control_since=-dt.timedelta(seconds=50), control_until=-dt.timedelta(seconds=10)
            )
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    assert GateSpec.model_validate((await gate_row(task_id, gate_id)).spec).source == "handoff"

    await _claim(task_id, workspace_id, ada_id)  # ends the run-out lease first, by its own clock
    assert (await _last_ending(task_id)).reason == "expired"
    assert (await gate_row(task_id, gate_id)).state == GateState.OPEN.value, "the stale ending left it open"
    assert _gate_wakes(recording_dispatcher) == []

    await _release(task_id, workspace_id, ada_id)
    wake = wake_of(recording_dispatcher, gate_id)
    assert wake["action"] == "accept" and wake["content"]["reason"] == "released"
    await resume_step(task_id, step_id, wake)
    assert len(told) == 1 and HANDED_BACK.match(told[0]), told


async def test_a_cancel_after_the_settle_leaves_as_the_recv_path_does(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settled race resumes in place - unless the task was cancelled between the settle's
    commit and the un-park. Then the step leaves exactly as the recv path's cancel exit does: the
    park given back, "(cancelled)", and the run never resumed."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    _ends_right_after_the_hook(monkeypatch, lambda: _release(task_id, workspace_id, ada_id))
    real = gates.resolve_control

    async def settle_then_cancel(task: uuid.UUID, **kw: Any) -> bool:
        closed = await real(task, **kw)
        if closed and kw.get("wake") is False:  # the settle's own resolve: Ada cancels right after
            async with bound_scope(workspace_id) as s:
                ada = await s.get(Actor, ada_id)
                assert ada is not None
                await steering.cancel(s, ada, task_id)
        return closed

    monkeypatch.setattr(gates, "resolve_control", settle_then_cancel)
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    assert await run_step(task_id, step_id) == "(cancelled)"
    assert told == [], "the run never resumed onto a cancelled task"
    assert await _machine_state(task_id) == MachineState.IDLE, "the park was given back"
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.status == TaskStatus.CANCELLED.value


async def test_a_death_before_the_settle_is_settled_on_recovery(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker died between the takeover gate's commit and its race settle, after the lease had
    already ended: nobody will ever wake that gate. The workflow's replay re-enters
    `execute_step`, whose recovery arm settles the open takeover (DBOS-free) and continues the
    same run instead of re-parking forever."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    _ends_right_after_the_hook(monkeypatch, lambda: _release(task_id, workspace_id, ada_id))
    real = execution.wake.settle_lease_race
    settles: list[str] = []

    async def died_first(task: uuid.UUID, *, holder: str, since: str) -> bool:
        settles.append(holder)
        if len(settles) == 1:
            return False  # the worker died here: the settle never ran
        return await real(task, holder=holder, since=since)

    monkeypatch.setattr(execution.wake, "settle_lease_race", died_first)
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    assert (await gate_row(task_id, gate_id)).state == GateState.OPEN.value

    said = await run_step(task_id, step_id)  # the replay
    assert not said.startswith(execution.ASKING), "settled on recovery, never re-parked"
    assert settles == ["Ada", "Ada"]
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told
    assert _gate_wakes(recording_dispatcher) == []
    assert await _resumed_lines(task_id) == ["resumed: the browser was handed back"]


async def test_an_ending_closes_only_its_own_takeover_and_reports_a_close_whose_wake_failed(
    fake_frames: Any, recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A takeover gate is its lease's: another lease's ending leaves it open. And a close whose
    wake then fails is still a close - True, so a settle never mistakes it for "nobody answered"
    and parks on a recv no wake will reach."""
    task_id, step_id, _, _ = await navigator_task()
    since = utcnow() - dt.timedelta(seconds=30)
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        gate = await gates.open_control(
            s,
            task,
            StepId(step_id),
            source="takeover",
            reason="",
            holder="Ada",
            lease_since=since.isoformat(),
        )
        gate_id = gate.id
        await s.commit()

    other = await gates.resolve_control(
        task_id,
        holder="Bob",
        held_s=5,
        reason=ControlEnd.RELEASED,
        lease_since=since - dt.timedelta(minutes=5),
        ended_at=utcnow(),
    )
    assert other is False and (await gate_row(task_id, gate_id)).state == GateState.OPEN.value

    async def broken(key: str, payload: object, topic: str, *, missing_ok: bool = False) -> None:
        raise RuntimeError("the poke lane is down")

    monkeypatch.setattr(recording_dispatcher, "send", broken)
    closed = await gates.resolve_control(
        task_id, holder="Ada", held_s=30, reason=ControlEnd.RELEASED, lease_since=since, ended_at=utcnow()
    )
    assert closed is True
    assert (await gate_row(task_id, gate_id)).state == GateState.RESOLVED.value


async def test_a_switch_to_private_still_answers_the_takeover_it_parked(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    """Ada drives in the open and the agent parks on her takeover; she then switches to Private -
    a fresh claim with a new `since` and no ending. Her hand-back still answers the
    gate the open lease parked: a LATER lease's ending answers a takeover, only an earlier one's
    never does."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    opened_on = GateSpec.model_validate((await gate_row(task_id, gate_id)).spec).lease_since

    await _claim(task_id, workspace_id, ada_id, private=True)  # the switch: a new `since`, no ending
    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None and row.control_since is not None and row.control_private
        assert row.control_since > dt.datetime.fromisoformat(opened_on), "the private lease is a later one"
    assert (await gate_row(task_id, gate_id)).state == GateState.OPEN.value

    await _release(task_id, workspace_id, ada_id)
    wake = wake_of(recording_dispatcher, gate_id)
    assert wake["action"] == "accept" and wake["content"]["holder"] == "Ada"
    said = await resume_step(task_id, step_id, wake)
    assert not said.startswith(execution.ASKING)
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told


async def test_a_twin_that_settled_first_is_seen_on_recovery(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin: both copies read the takeover gate open; the twin's settle closes it on its
    own session, so this copy's settle finds nothing to close. The recovery arm re-reads the row
    rather than trusting its stale copy - it resumes from the twin's answer, never re-parks on a
    gate no wake will ever reach."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        told.append(str(returns[-1].content))
        return "Done."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))  # Ada still drives: a plain park

    async def the_twin_won(task: uuid.UUID, step: PlanStep, gate: Gate) -> bool:
        since = GateSpec.model_validate(gate.spec).lease_since
        assert await gates.resolve_control(
            task,
            holder="Ada",
            held_s=12,
            reason=ControlEnd.RELEASED,
            lease_since=dt.datetime.fromisoformat(since),
            ended_at=None,
            wake=False,
        )
        return False  # this copy's own settle found the gate already closed

    monkeypatch.setattr(execution.wake, "settle_parked", the_twin_won)
    said = await run_step(task_id, step_id)  # this copy's replay
    assert not said.startswith(execution.ASKING), "re-read the row: the twin's answer, never a re-park"
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told
    assert (await gate_row(task_id, gate_id)).state == GateState.RESOLVED.value


# ── #12 x #14: the control park under the pause and the workflow's hand-back record ─────────


async def _pause(task_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    async with bound_scope(workspace_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        await steering.pause(s, actor, task_id)


async def _task_status(task_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        return task.status


async def _record(task_id: uuid.UUID, step_id: uuid.UUID) -> list[str]:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        return list((step.resume_state or {}).get(execution.HANDBACKS, []))


async def test_a_paused_task_stays_paused_under_a_control_park_and_its_answer(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
) -> None:
    """Q14-25: Ada pauses the task mid-step and then takes the browser. The control park leaves the
    task `paused`, never `waiting` (`Task.stays_paused`, read in `advance`); the lease's ending
    answers the gate and the run goes on to the end of its step, and still only a person's resume
    lifts the pause."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    told: list[str] = []

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_snapshot", "snap-1")
        if len(returns) == 1:
            await _pause(task_id, workspace_id, ada_id)
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-2")
        told.append(str(returns[-1].content))
        return "Done: Mary Jackson is open."

    engine(turn)
    gate_id = parked_on(await run_step(task_id, step_id))
    gate = await gate_row(task_id, gate_id)
    assert gate.kind == GateKind.CONTROL.value and gate.is_open()
    assert await _task_status(task_id) == TaskStatus.PAUSED.value, "the park never un-pauses the task"
    assert await _record(task_id, step_id) == [f"park:{gate_id}"], "the control park is a recorded hand-back"

    await _release(task_id, workspace_id, ada_id)
    said = await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert not said.startswith(execution.ASKING)
    assert len(told) == 1 and TAKEN_OVER.match(told[0]), told
    assert await _task_status(task_id) == TaskStatus.PAUSED.value, "the gate's answer keeps the pause"

    async with bound_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        assert await steering.resume(s, ada, task_id) == TaskStatus.RUNNING.value


async def test_a_race_settled_in_place_is_no_hand_back_and_a_replay_reissues_the_parks_taken(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Callable[[Turn], None],
    sidecar: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hand-back record under the race: the takeover park settled in place was
    never handed to the `run_step` workflow (no recv), so it never enters the record; the next park
    (a handoff) does. A replay re-issues exactly the parks the workflow took, in order - never the
    settled one, whose recv the journal does not hold."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    _ends_right_after_the_hook(monkeypatch, lambda: _release(task_id, workspace_id, ada_id))

    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            await _claim(task_id, workspace_id, ada_id)
            return tool_call("browser_snapshot", "snap-1")
        if len(returns) == 1:
            return tool_call("browser_handoff", "hand-1", reason="sign in to admin.example.com")
        return "Done."

    engine(turn)
    handoff_id = parked_on(await run_step(task_id, step_id))
    async with row_scope(Task, task_id) as s:
        gates_now = sorted(await Gate.for_task(s, task_id), key=lambda g: g.created_at)
        seen = [(g.id, g.kind, g.state) for g in gates_now]
    [(_, settled_kind, settled_state), (handoff_row, _, handoff_state)] = seen
    assert settled_kind == GateKind.CONTROL.value and settled_state == GateState.RESOLVED.value
    assert handoff_row == handoff_id and handoff_state == GateState.OPEN.value
    assert await _record(task_id, step_id) == [f"park:{handoff_id}"], "the settled park is no hand-back"

    async with row_scope(Task, task_id) as s:
        replayed = await execution.execute_step(s, task_id, step_id, handed_back=0)
    assert parked_on(replayed) == handoff_id, "a replay re-issues the handoff, the one park taken"
