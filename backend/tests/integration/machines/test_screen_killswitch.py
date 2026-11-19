# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The kill switch, made Machine-aware. With `PRISMOID_COMPUTER_USE_ENABLED=false`:
no new screens or leases, and no Renew (409 "Computer use is off."); an open screen closes 4410 at
its next proof, a holder's lease ending `disabled` first; and the sweeps' kill-switch arms end every
live lease (`disabled`), decline every open CONTROL gate and every open browser-sourced APPROVE
gate, and stop each warm browser Machine's Chromium once per epoch. The Machine keeps its idle
life; a connector's approval is not the switch's business.

The doors and the proof were built with the lease and the socket; this file
walks them once more, end to end on fake frames, beside the sweeps' arms."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, MutableMapping
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict
from prismoid.operations import auth as auth_ops
from prismoid.operations import gates, machines, tickets
from prismoid.operations import screen as screen_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import screen as machine_screen
from prismoid.schemas.events import MachineControlEnded
from prismoid.schemas.ids import StepId
from prismoid.schemas.machines import MachineProfile, MachineState, frame_name, step_holder
from prismoid.schemas.plan import GateState
from prismoid.schemas.screen import ControlEnd, decode
from tests.support.cast import bound_scope, member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration

OFF = "Computer use is off."


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)  # each test flips it off itself


class KwargsDispatcher(RecordingDispatcher):
    """`RecordingDispatcher`, plus each enqueue's kwargs by job id."""

    def __init__(self) -> None:
        super().__init__()
        self.kwargs: dict[str, dict[str, object]] = {}

    async def enqueue(self, job: str, key: str, *, delay_s: float = 0.0, **kwargs: object) -> None:
        await super().enqueue(job, key, delay_s=delay_s, **kwargs)
        self.kwargs[key] = dict(kwargs)


@pytest.fixture
def dispatcher() -> Iterator[KwargsDispatcher]:
    from prismoid.operations import dispatch

    d = KwargsDispatcher()
    dispatch.set_dispatcher(d)
    yield d
    dispatch.set_dispatcher(dispatch.NullDispatcher())


@pytest.fixture
def only_this_task(monkeypatch: pytest.MonkeyPatch) -> Callable[[uuid.UUID], None]:
    """The gate arm is platform-wide by design; under xdist other workers' open browser gates share
    this database, so the arm is narrowed to the test's own task - never declined under them."""

    def narrow(task_id: uuid.UUID) -> None:
        real = Gate.open_for_computer_use

        async def mine(session: AsyncSession) -> list[Gate]:
            return [g for g in await real(session) if g.task_id == task_id]

        monkeypatch.setattr(Gate, "open_for_computer_use", mine)

    return narrow


@pytest.fixture
def sidecar(fake_frames: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every sidecar command, with the frame it reached."""
    said: list[tuple[str, dict[str, Any]]] = []

    async def browser(self: Any, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        said.append((self.name, dict(command)))
        return {"ok": True}

    monkeypatch.setattr(fake_frames, "browser", browser)
    return said


async def _warm_browser_machine(
    fake_frames: Any,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ada's personal task with one step and a warm (idle) browser Machine whose frame is live.
    Returns (task, step, machine, Ada, workspace)."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="update the admin app")
        step = PlanStep(task_id=task.id, ordinal=0, title="update the record")
        s.add(step)
        machine_id = uuid.uuid4()
        s.add(
            Machine(
                id=machine_id,
                workspace_id=task.workspace_id,
                task_id=task.id,
                driver="docker",
                profile=MachineProfile.BROWSER,
                state=MachineState.IDLE,
                state_since=utcnow(),
                last_active_at=utcnow(),
                epoch=1,
                frame_ref=frame_name(machine_id, 1),
            )
        )
        await s.flush()
        task_id, step_id, ada_id, workspace_id = task.id, step.id, ada.id, workspace.id
        await s.commit()
    await fake_frames(str(task_id), profile="browser", name=frame_name(machine_id, 1)).create()
    return task_id, step_id, machine_id, ada_id, workspace_id


async def _open_gates(task_id: uuid.UUID, step_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A parked CONTROL gate, a browser danger ask and a connector approval - all open."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        control = await gates.open_control(
            s, task, StepId(step_id), source="takeover", reason="", holder="Ada"
        )
        browser = await gates.open_approval(
            s,
            task,
            StepId(step_id),
            [("c1", "admin.example.com", "browser.click", 'click button "Save" at admin.example.com/users')],
            source="browser",
        )
        connector = await gates.open_approval(
            s, task, StepId(step_id), [("c2", "Notion", "notion_create_page", "args: title")]
        )
        await s.commit()
        return control.id, browser.id, connector.id


async def _row(machine_id: uuid.UUID) -> Machine:
    async with row_scope(Machine, machine_id) as s:
        row = await Machine.current(s, machine_id)
        assert row is not None
        s.expunge(row)
        return row


async def _since(machine_id: uuid.UUID) -> dt.datetime | None:
    return (await _row(machine_id)).control_since


async def _gate(task_id: uuid.UUID, gate_id: uuid.UUID) -> Gate:
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, gate_id)
        assert gate is not None
        s.expunge(gate)
        return gate


async def _endings(task_id: uuid.UUID) -> list[str]:
    async with row_scope(Task, task_id) as s:
        return [e.reason for e in await TaskEvent.replay(s, task_id) if isinstance(e, MachineControlEnded)]


@contextlib.asynccontextmanager
async def _as(workspace_id: uuid.UUID, actor_id: uuid.UUID) -> AsyncGenerator[tuple[AsyncSession, Actor]]:
    async with bound_scope(workspace_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        yield s, actor


async def _claim_as(
    workspace_id: uuid.UUID, actor_id: uuid.UUID, task_id: uuid.UUID, conn: str, *, private: bool
) -> None:
    async with _as(workspace_id, actor_id) as (s, actor):
        await machine_screen.claim(s, actor, task_id, conn=conn, private=private)


async def _extend_as(workspace_id: uuid.UUID, actor_id: uuid.UUID, task_id: uuid.UUID) -> None:
    async with _as(workspace_id, actor_id) as (s, actor):
        await machine_screen.extend(s, actor, task_id)


def _mine(dispatcher: KwargsDispatcher, machine_id: uuid.UUID) -> list[str]:
    """The lease-end and browser-stop job ids the sweep dispatched for this Machine."""
    return [
        k
        for k in dispatcher.kwargs
        if k.startswith(("machine-lease-", "machine-browser-stop-")) and str(machine_id) in k
    ]


async def test_the_switch_ends_leases_declines_gates_and_stops_browsers(
    fake_frames: Any,
    dispatcher: KwargsDispatcher,
    sidecar: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    only_this_task: Callable[[uuid.UUID], None],
) -> None:
    task_id, step_id, machine_id, ada_id, workspace_id = await _warm_browser_machine(fake_frames)
    control_gate, browser_gate, connector_gate = await _open_gates(task_id, step_id)
    only_this_task(task_id)
    await _claim_as(workspace_id, ada_id, task_id, "ada_tab_0001", private=True)
    since = await _since(machine_id)
    assert since is not None

    # On: a live lease is left alone, and nobody's browser is stopped; no gate is the switch's.
    await machines.sweep_once(utcnow())
    assert _mine(dispatcher, machine_id) == []
    async with session_scope() as s:
        swept = await gates.sweep(s)
    assert not {control_gate, browser_gate, connector_gate} & set(swept)

    monkeypatch.setattr(settings.computer_use, "enabled", False)

    with pytest.raises(Conflict, match=OFF):  # no new leases (the claim's own refusal, walked here)
        await _claim_as(workspace_id, ada_id, task_id, "ada_tab_0002", private=False)
    with pytest.raises(Conflict, match=OFF):  # nor a Renew of the live one
        await _extend_as(workspace_id, ada_id, task_id)

    # The machine sweep ends the live lease, `disabled`, under the lease-end job's tick-scoped id - and offers
    # the browser stop once per Machine per epoch (no tick in its id).
    tick = utcnow()
    await machines.sweep_once(tick)
    lease_key = f"machine-lease-{machine_id}-{since.isoformat()}~{tick.isoformat()}"
    stop_key = f"machine-browser-stop-{machine_id}-1"
    assert sorted(_mine(dispatcher, machine_id)) == sorted([lease_key, stop_key])
    assert dispatcher.kwargs[lease_key] == {
        "machine_id": str(machine_id),
        "since": since.isoformat(),
        "reason": "disabled",
    }
    assert dispatcher.kwargs[stop_key] == {"machine_id": str(machine_id), "epoch": 1}
    jobs = {key: job for job, key in dispatcher.enqueues}
    assert (jobs[lease_key], jobs[stop_key]) == ("machine_lease_end", "machine_browser_stop")

    # The gate sweep declines the CONTROL gate and the browser ask, naming the switch.
    async with session_scope() as s:
        swept = await gates.sweep(s)
    assert control_gate in swept and browser_gate in swept and connector_gate not in swept
    for gate_id in (control_gate, browser_gate):
        gate = await _gate(task_id, gate_id)
        assert gate.state == GateState.RESOLVED.value and gate.resolution is not None
        assert gate.resolution["action"] == "decline"
        assert gate.resolution["content"] == {"outcome": "disabled", "reason": OFF}
        assert gate.resolution["resolved_by"] == "system (computer use off)"
    assert (await _gate(task_id, connector_gate)).state == GateState.OPEN.value
    woken = {
        str(payload.get("gate_id"))
        for _, payload, topic in dispatcher.sends
        if topic == gates.GATE_TOPIC and isinstance(payload, dict)
    }
    assert {str(control_gate), str(browser_gate)} <= woken, "the parked steps wake to meet the switch"
    async with session_scope() as s:  # a closed gate is not declined twice
        swept = await gates.sweep(s)
    assert not {control_gate, browser_gate} & set(swept)

    # The lease-end job's body: the lease ends `disabled`, on the record.
    assert await machine_screen.end_lease(machine_id, since=since, reason=ControlEnd.DISABLED)
    assert await _since(machine_id) is None
    assert await _endings(task_id) == ["disabled"]

    # The browser-stop job's body: one shutdown on THIS epoch's frame; another epoch is not ours.
    assert await machines.stop_browser(machine_id, 1) == "stopped"
    assert sidecar == [(frame_name(machine_id, 1), {"verb": "shutdown"})]
    assert await machines.stop_browser(machine_id, 2) == "skipped"
    assert len(sidecar) == 1
    row = await _row(machine_id)
    assert row.state == MachineState.IDLE and row.frame_ref == frame_name(machine_id, 1), "idle life goes on"

    # On again: nothing left to end or stop.
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    dispatcher.kwargs.clear()
    await machines.sweep_once(utcnow())
    assert _mine(dispatcher, machine_id) == []


async def test_a_running_steps_browser_is_never_stopped(
    fake_frames: Any,
    dispatcher: KwargsDispatcher,
    sidecar: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser-stop id carries no tick, so it is offered only for an idle or parked Machine: a
    running step's Chromium is its own, and spending the epoch's id on it would leave the Machine's
    browser running once the step lets go. A frame that is gone is the reconciler's."""
    _, _, machine_id, _, _ = await _warm_browser_machine(fake_frames)
    async with row_scope(Machine, machine_id) as s:
        row = await Machine.locked(s, machine_id)
        assert row is not None
        row.state, row.held_by = MachineState.RUNNING, step_holder(str(uuid.uuid4()), 1)
        await s.commit()
    monkeypatch.setattr(settings.computer_use, "enabled", False)

    await machines.sweep_once(utcnow())
    assert _mine(dispatcher, machine_id) == []
    assert await machines.stop_browser(machine_id, 1) == "skipped"

    async with row_scope(Machine, machine_id) as s:  # the step lets go: idle, and offered now
        row = await Machine.locked(s, machine_id)
        assert row is not None
        row.state, row.held_by = MachineState.IDLE, None
        await s.commit()
    await machines.sweep_once(utcnow())
    assert _mine(dispatcher, machine_id) == [f"machine-browser-stop-{machine_id}-1"]

    fake_frames.world.pop(frame_name(machine_id, 1))  # the frame went under the job
    assert await machines.stop_browser(machine_id, 1) == "skipped"
    assert sidecar == []


# ── an open screen, and the three doors ───────────────────────────────────────────────────────


@pytest.fixture
async def client(
    recording_dispatcher: RecordingDispatcher, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.api, "stream_reauth_seconds", 0.3)
    monkeypatch.setattr(tickets, "PROCESS_STARTED", time.time() - 60)  # never a fresh process's stale ticket
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()
    await hub.stop()  # the screen socket subscribed on the hub; stop its LISTEN before the engine goes


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _person(fake_frames: Any) -> tuple[str, uuid.UUID, uuid.UUID]:
    """A signed-up user's task with a warm browser Machine. Returns (token, task, machine)."""
    async with session_scope() as s:
        result = await auth_ops.signup(
            s, email=f"k-{uuid.uuid4().hex[:8]}@example.com", name="Ada", password="a-long-enough-password"
        )
        actor = await s.get(Actor, result.user.actor_id)
        assert actor is not None
        task = await task_ops.create_task(s, actor, goal="update the admin app")
        machine_id = uuid.uuid4()
        s.add(
            Machine(
                id=machine_id,
                workspace_id=task.workspace_id,
                task_id=task.id,
                driver="docker",
                profile=MachineProfile.BROWSER,
                state=MachineState.IDLE,
                state_since=utcnow(),
                last_active_at=utcnow(),
                epoch=1,
                frame_ref=frame_name(machine_id, 1),
            )
        )
        token, task_id = result.token, task.id
        await s.commit()
    await fake_frames(str(task_id), profile="browser", name=frame_name(machine_id, 1)).create()
    return token, task_id, machine_id


async def _socket(task_id: uuid.UUID, ticket: str, sent: list[dict[str, Any]]) -> None:
    """Drive the ASGI app as a screen client that says nothing after connecting (the
    `test_presence.py` harness); every message the server sends lands in `sent`."""
    app = create_app()
    connected = False
    silence = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await silence.wait()  # never set: the client says nothing more
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    path = f"/v1/tasks/{task_id}/screen"
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": f"ticket={ticket}".encode(),
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
    }
    await app(scope, receive, send)


async def _until(done: Callable[[], bool], within: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not done():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def test_an_open_screen_closes_4410_and_the_doors_answer_409(
    client: AsyncClient, fake_frames: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_browser(machine_id: uuid.UUID) -> machine_screen.Opened:
        raise machine_screen.NoScreen("no_browser")  # this test is about the proof, not frames

    monkeypatch.setattr(screen_ops, "relay", screen_ops.Relay(open_upstream=no_browser))
    token, task_id, machine_id = await _person(fake_frames)
    ticket = await client.post(
        f"/v1/tasks/{task_id}/screen/ticket", headers=_h(token), json={"conn": "ada_tab_0001"}
    )
    assert ticket.status_code == 200, ticket.text
    claimed = await client.post(
        f"/v1/tasks/{task_id}/screen/control",
        headers=_h(token),
        json={"conn": "ada_tab_0001", "private": False},
    )
    assert claimed.status_code == 200, claimed.text

    sent: list[dict[str, Any]] = []
    sock = asyncio.create_task(_socket(task_id, str(ticket.json()["ticket"]), sent))
    try:
        await _until(lambda: any(m["type"] == "websocket.accept" for m in sent))
        await _until(lambda: screen_ops.relay.held(machine_id) is not None)  # attached, the lease read
        monkeypatch.setattr(settings.computer_use, "enabled", False)
        await _until(lambda: any(m["type"] == "websocket.close" for m in sent))
    finally:
        sock.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sock
    close = next(m for m in sent if m["type"] == "websocket.close")
    assert close["code"] == 4410
    headers = [decode(m["bytes"])[0] for m in sent if m["type"] == "websocket.send" and m.get("bytes")]
    assert [h.get("reason") for h in headers if h.get("t") == "bye"] == ["computer use is off"]
    assert await _since(machine_id) is None, "the holder's lease ended with the screen"
    async with row_scope(Task, task_id) as s:
        ended = [e for e in await TaskEvent.replay(s, task_id) if isinstance(e, MachineControlEnded)]
    assert [e.reason for e in ended] == ["disabled"]

    for path, body in (
        (f"/v1/tasks/{task_id}/screen/ticket", {"conn": "ada_tab_0002"}),
        (f"/v1/tasks/{task_id}/screen/control", {"conn": "ada_tab_0002", "private": False}),
        (f"/v1/tasks/{task_id}/screen/control/renew", None),
    ):
        r = await client.post(path, headers=_h(token), json=body)
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "conflict" and err["message"] == OFF
