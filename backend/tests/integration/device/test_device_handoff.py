# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device's browser and its hand-off.

A device task's browser is the Prismoid Chrome window on the user's own Mac. `browser_handoff` there
opens a CONTROL gate as on a cloud Machine, but no screen relay and no control lease exist: the
person is at the real window, and the task's creator ends the gate with "Done" (an accept). Every
other CONTROL rule is unchanged: a cloud Machine's accept is still the 409, and a teammate's Done
is refused.

The engine tests reuse `test_control_gate`'s harness (the real `execute_step` and the REAL browser
toolset over a scripted model, every frame a fake). The darwin tests drive the real Google Chrome
through a real helper on its Unix socket, with the worker's own `DeviceFrame`."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCalls

from prismoid import egress_relay
from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, browser_toolset
from prismoid.ai.verification import change
from prismoid.api.endpoints.gates import get_gate, list_gates
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import Workspace, WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, Forbidden, PolicyDenied
from prismoid.operations import device_gates, execution, gates, machines, planning_steps
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.plan_shapes import one_step_plan
from prismoid.runner.device_door import create_app
from prismoid.schemas.ids import StepId
from prismoid.schemas.machines import FrameDriverName, MachineState, SleepReason
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, GateState
from prismoid.schemas.verification import CheckStatus
from tests.support.cast import bound_scope, member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.markers import darwin_only, needs_chrome
from tests.support.rigs.chrome_rig import (
    CHROME,
    Site,
    short_home,
)
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

DEVICE = FrameDriverName.DEVICE
SIGN_IN = "sign in to admin.example.com"


@pytest.fixture
def no_lease_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device Machine has no screen relay and no control lease: nothing on its hand-off path may
    read one."""

    async def refuse(task_id: uuid.UUID) -> None:
        raise AssertionError("a device task read a control lease")

    monkeypatch.setattr(machines.screen, "control_state", refuse)


async def _device_task() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """`navigator_task`, on the user's own computer: (task, step, Ada, workspace)."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.machine_driver = DEVICE
        await s.commit()
    return task_id, step_id, ada_id, workspace_id


def _hands_off(told: list[str]) -> Turn:
    async def turn(returns: list[ToolReturnPart]) -> str | DeltaToolCalls:
        if not returns:
            return tool_call("browser_handoff", "hand-1", reason=SIGN_IN)
        told.append(str(returns[-1].content))
        return "Signed in; carrying on."

    return turn


async def _gate_out(workspace_id: uuid.UUID, actor_id: uuid.UUID, gate_id: uuid.UUID) -> Any:
    async with bound_scope(workspace_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        return await get_gate(gate_id, actor, s)


async def test_handoff_on_device_opens_control_gate(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
    no_lease_read: None,
) -> None:
    task_id, step_id, ada_id, workspace_id = await _device_task()
    engine(_hands_off([]))
    gate_id = parked_on(await run_step(task_id, step_id))

    gate = await gate_row(task_id, gate_id)
    spec = GateSpec.model_validate(gate.spec)
    assert gate.kind == GateKind.CONTROL.value and spec.source == "handoff" and spec.questions is None
    assert spec.title == f'The agent needs someone to "{SIGN_IN}"'
    assert sidecar.count("inspect") == 1, "the hand-off read where the page is"

    out = await _gate_out(workspace_id, ada_id, gate_id)
    assert out.machine_driver == DEVICE, "the web and the CLI offer Done off this"
    async with bound_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        queued = [g for g in await list_gates(ada, s) if g.id == gate_id]
    assert [g.machine_driver for g in queued] == [DEVICE]


async def test_a_coder_on_the_device_drives_the_window(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
    no_lease_read: None,
) -> None:
    """Through the real step path: a coder step on a device task boots the browser profile
    and is handed ports, and a hand-off parks on the window's own gate. The `engine` stubs `build`
    over the browser toolset, so what `build` grants is pinned in `test_device_coding_seat`."""
    task_id, step_id, _ada, _workspace = await _device_task()
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        step.spec = {**step.spec, "agent": "maker"}
        await s.commit()
    engine(_hands_off([]))
    gate_id = parked_on(await run_step(task_id, step_id))
    assert (await gate_row(task_id, gate_id)).kind == GateKind.CONTROL.value


async def test_a_coder_on_a_mac_without_chrome_fails_with_the_reason(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
    no_lease_read: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fallback: the step says what it needs, and never reaches a frame, so it never reads as
    the device gone away."""
    task_id, step_id, _ada, _workspace = await _device_task()
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        step.spec = {**step.spec, "agent": "maker"}
        await s.commit()
    monkeypatch.setattr(settings.local, "chrome_path", "/no/such/chrome")
    engine(_hands_off([]))
    with pytest.raises(PolicyDenied, match="Google Chrome"):
        await run_step(task_id, step_id)
    assert fake_frames.created == []


@pytest.mark.parametrize(("driver", "kind"), [(DEVICE, "change"), (None, "code")], ids=["device", "cloud"])
async def test_a_written_plan_wears_the_tasks_machine(
    silent_dispatcher: None, driver: FrameDriverName | None, kind: str
) -> None:
    """Every plan path lands in `write_steps`, so that is where the type is decided."""
    task_id, _step, _ada, _workspace = await _device_task()
    steps, edges, _criteria = one_step_plan("Fix it", agent="maker", produces=ArtifactType.CODE)
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.machine_driver = driver
        [row] = await planning_steps.write_steps(s, task, steps, edges, base_ordinal=1)
        assert row.spec["contract"]["output"]["type"] == kind
        await s.rollback()


async def test_creator_accept_done_resumes_step(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
    no_lease_read: None,
) -> None:
    task_id, step_id, ada_id, workspace_id = await _device_task()
    told: list[str] = []
    engine(_hands_off(told))
    gate_id = parked_on(await run_step(task_id, step_id))

    done = await resolve_gate(workspace_id, ada_id, gate_id, GateAction.ACCEPT)
    assert done.action is GateAction.ACCEPT
    assert done.content is not None
    assert (done.content["outcome"], done.content["holder"], done.content["reason"]) == (
        "accepted",
        "Ada",
        "released",
    )
    assert isinstance(done.content["held_s"], int) and done.content["held_s"] >= 0
    assert (await gate_row(task_id, gate_id)).state == GateState.RESOLVED.value

    said = await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert not said.startswith(execution.ASKING)
    assert len(told) == 1 and HANDED_BACK.match(told[0]), told
    again = await resolve_gate(workspace_id, ada_id, gate_id, GateAction.ACCEPT)
    assert again == done, "a second Done is handed the first"


async def test_non_creator_accept_refused(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
    no_lease_read: None,
) -> None:
    """A teammate never says Done: the window is on the creator's computer. (Device tasks are
    private in v1, so no teammate sees one; the task is filed in a Project here to prove the rule
    holds anyway.)"""
    task_id, step_id, ada_id, workspace_id = await _device_task()
    async with bound_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        workspace = await s.get(Workspace, workspace_id)
        assert ada is not None and workspace is not None
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"shared-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        task = await s.get(Task, task_id)
        assert task is not None
        task.project_id = project.id
        await s.commit()
        bob_id = bob.id
    engine(_hands_off([]))
    gate_id = parked_on(await run_step(task_id, step_id))

    with pytest.raises(Forbidden, match="creator"):
        await resolve_gate(workspace_id, bob_id, gate_id, GateAction.ACCEPT)
    assert (await gate_row(task_id, gate_id)).state == GateState.OPEN.value
    assert (await resolve_gate(workspace_id, ada_id, gate_id, GateAction.ACCEPT)).action is GateAction.ACCEPT


async def test_cloud_control_accept_still_conflict(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
) -> None:
    """On a cloud Machine nothing changed: the lease's ending answers the gate, a person's accept
    is the 409, and the gate names no driver."""
    task_id, step_id, ada_id, workspace_id = await navigator_task()
    engine(_hands_off([]))
    gate_id = parked_on(await run_step(task_id, step_id))

    with pytest.raises(Conflict, match=re.escape(device_gates.TAKE_CONTROL_INSTEAD)):
        await resolve_gate(workspace_id, ada_id, gate_id, GateAction.ACCEPT)
    assert (await gate_row(task_id, gate_id)).state == GateState.OPEN.value
    assert (await _gate_out(workspace_id, ada_id, gate_id)).machine_driver is None


async def test_a_device_control_gate_outlives_the_park_hold_and_done_still_resumes(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
    no_lease_read: None,
) -> None:
    """A device's CONTROL gate waits the away limit, and the park hold never
    sleeps its Machine meanwhile - the window and its page stay as the person left them - so the
    creator's Done, long after `park_hold_s`, resumes the same step on the same frame."""
    task_id, step_id, ada_id, workspace_id = await _device_task()
    told: list[str] = []
    engine(_hands_off(told))
    gate_id = parked_on(await run_step(task_id, step_id))
    gate = await gate_row(task_id, gate_id)
    assert gate.expires_at is not None
    waits = (gate.expires_at - gate.created_at).total_seconds()
    assert abs(waits - settings.local.away_limit_h * 3600) < 60, "the away limit, not control_wait_s"

    machine_id, state = await machine_row(task_id)
    assert state == MachineState.PARKED
    async with row_scope(Task, task_id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        row.state_since = row.state_since - dt.timedelta(seconds=settings.machines.park_hold_s + 60)
        await s.commit()
    await machines.sweep_once(dt.datetime.now(dt.UTC))
    assert not [key for _, key in recording_dispatcher.enqueues if str(machine_id) in key], (
        "a device Machine waiting on Done is never park-hold slept"
    )
    assert (await machine_row(task_id))[1] == MachineState.PARKED
    live = next(f for f in fake_frames.world.values() if f.task_id == str(task_id))
    assert "stop_services" not in live.calls, "the page and what it left running are untouched"

    await resolve_gate(workspace_id, ada_id, gate_id, GateAction.ACCEPT)
    said = await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    assert not said.startswith(execution.ASKING)
    assert len(told) == 1 and HANDED_BACK.match(told[0]), told


async def test_an_expired_device_gate_no_longer_holds_its_machine_awake(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
) -> None:
    """Fix round 2: a gate past its `expires_at` that the gate sweep has yet to close waits on
    nobody - the park hold applies to its Machine again."""
    task_id, step_id, _, _ = await _device_task()
    engine(_hands_off([]))
    gate_id = parked_on(await run_step(task_id, step_id))
    machine_id, _ = await machine_row(task_id)
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, gate_id)
        row = await Machine.for_task(s, task_id)
        assert gate is not None and row is not None
        gate.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        row.state_since = row.state_since - dt.timedelta(seconds=settings.machines.park_hold_s + 60)
        await s.commit()
    await machines.sweep_once(dt.datetime.now(dt.UTC))
    assert [job for job, key in recording_dispatcher.enqueues if str(machine_id) in key] == ["machine_sleep"]


async def test_a_device_gate_nobody_answers_says_how_long_it_waited(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    engine: Any,
    sidecar: list[str],
) -> None:
    task_id, step_id, _, _ = await _device_task()
    told: list[str] = []
    engine(_hands_off(told))
    gate_id = parked_on(await run_step(task_id, step_id))
    async with row_scope(Task, task_id) as s:
        gate = await s.get(Gate, gate_id)
        assert gate is not None
        gate.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await s.commit()
    async with session_scope() as s:
        assert gate_id in await gates.sweep(s)
    await resume_step(task_id, step_id, wake_of(recording_dispatcher, gate_id))
    hours = f"{settings.local.away_limit_h}h"
    assert told == [
        f"Nobody took over (no one came in {hours}). Continue without it if you can, or stop and say what is needed."
    ]


async def test_the_task_read_says_what_a_paused_step_waits_for(silent_dispatcher: None) -> None:
    """`prismoid tasks show` reads it: "Waiting for <device name>" while a device-away gate is open,
    and nothing once the task resumes."""
    task_id, step_id, ada_id, workspace_id = await _device_task()
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        await gates.open_device_away(s, task, StepId(step_id), "away", device_name="MacBook Pro")
        await s.commit()
    async with bound_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        assert (await task_ops.read_task(s, ada, task_id)).waiting_for == "Waiting for MacBook Pro"
        task = await s.get(Task, task_id)
        assert task is not None
        await gates.resolve_pause(s, task, ada)
        task.status = "running"
        await s.commit()
    async with bound_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        assert (await task_ops.read_task(s, ada, task_id)).waiting_for is None


# ── the real Prismoid Chrome, through a real helper (darwin) ──────────────────────────────────────


class Device:
    def __init__(self, home: Path, site: Site, app: Any) -> None:
        self.home, self.site, self.app = home, site, app

    def profile_of(self, ref: str) -> str:
        return str(self.app.state.registry.frames[ref].spec.profile)


@pytest.fixture
async def device(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Device]:
    """A helper on its Unix socket in a short home the worker's `DeviceFrame` reads
    (`settings.local.home`), and a local site `a.test` resolves to (the proxy's resolve-and-vet,
    the one seam patched)."""

    async def resolve(host: str, port: int) -> tuple[list[str], str | None]:
        return (["127.0.0.1"], None) if host.endswith(".test") else ([], "unresolvable")

    monkeypatch.setattr(egress_relay, "_resolve_and_vet", resolve)
    site = Site()
    server = await asyncio.start_server(site.handle, "127.0.0.1", 0)
    site.port = server.sockets[0].getsockname()[1]
    home = short_home()
    monkeypatch.setattr(settings.local, "home", home)
    app = create_app(home, device_name=lambda: "Test Mac", chrome_path=CHROME)
    uv = uvicorn.Server(
        uvicorn.Config(
            app, uds=str(home / "run" / "helper.sock"), log_level="critical", timeout_graceful_shutdown=1
        )
    )
    serving = asyncio.create_task(uv.serve())
    while not uv.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.02)
    try:
        yield Device(home, site, app)
    finally:
        uv.should_exit = True
        await serving
        server.close()
        await server.wait_closed()
        shutil.rmtree(home, ignore_errors=True)


def _recording(home: Path, task_id: str, tag: str = "1") -> Path:
    return home / "tasks" / task_id / "task" / "recordings" / f"recording-{tag}"


@darwin_only
@needs_chrome
async def test_browser_verbs_reach_device_window(device: Device) -> None:
    """The worker's browser verbs drive the task's own Prismoid Chrome window over the helper's relay,
    and the step's recording lands in the task's folder on the device."""
    port = device.site.port
    task_id = f"t{uuid.uuid4().hex[:8]}"
    frame = DeviceFrame(task_id, profile="browser")
    await frame.create(allow_domains=["a.test"], labels={"plane.kind": "machine"})
    try:
        opened = await frame.browser({"verb": "open", "url": f"http://a.test:{port}/hello"})
        assert opened.get("ok"), opened
        assert device.site.saw("a.test", "/hello"), "the page loaded in the Prismoid Chrome"
        shot = await frame.browser({"verb": "snapshot"})
        assert shot.get("ok") and "a.test/hello" in str(shot.get("title")), shot
        refused = await frame.browser({"verb": "open", "url": f"http://b.test:{port}/no"})
        assert not refused.get("ok"), refused
        assert not device.site.saw("b.test", "/no"), "another host stays fenced"

        ledger = device.home / "tasks" / task_id / "task" / ".browser-epochs"
        assert ledger.read_text() == "100", "the session's epochs were reserved before use"
        folder = _recording(device.home, task_id)
        assert (folder / "actions.json").is_file(), sorted(p.name for p in folder.parent.rglob("*"))
        done = await frame.browser({"verb": "finalize"})
        assert done.get("ok") and int(done.get("actions", 0)) >= 1, done
        assert (folder / "index.html").is_file()
        assert str(device.home) not in str(done), "nothing the driver returns carries a real path"
    finally:
        await frame.close()
        await frame.destroy()


def _leftover(out: Path) -> None:
    (out / "recording-pub-0-deadbeef").mkdir()
    (out / "recording-pub-0-deadbeef" / "actions.json").write_text("[]")


def _staged(folder: Path) -> list[str]:
    """Every `recording-pub-*` under `folder` (the engine's staged recording copy)."""
    return sorted(p.name for p in folder.rglob("recording-pub-*"))


@darwin_only
@needs_chrome
@pytest.mark.parametrize("fails", [False, True], ids=["published", "publish-fails-partway"])
async def test_a_device_recording_never_lands_in_the_users_folder(
    device: Device, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    """The engine's recording publish stages its copy in the task's own
    `out/`, never `/work` (the user's real folder) - and leaves nothing behind there when the
    publish dies partway either."""
    task_id = f"t{uuid.uuid4().hex[:8]}"
    frame = DeviceFrame(task_id, profile="browser")
    await frame.create(allow_domains=["a.test"], labels={"plane.kind": "machine"})
    work, out = Path(frame.paths.work), Path(str(frame.paths.out))
    _leftover(out)  # an earlier crash's copy (fix round 2): this publish clears it
    seen: list[list[str]] = []
    try:
        assert (await frame.browser({"verb": "open", "url": f"http://a.test:{device.site.port}/rec"})).get(
            "ok"
        )
        if fails:
            real_publish = frame.publish

            async def dies(path: str, *args: Any, **kw: Any) -> None:
                seen.append(_staged(out))  # the copy is there, in out/
                await real_publish(path, *args, **kw)
                raise RuntimeError("the worker died mid-publish")

            monkeypatch.setattr(frame, "publish", dies)
        facts: list[str] = []

        async def fact(phase: str, label: str, *, started: float | None = None) -> None:
            facts.append(label)

        published = await ex_harvest.publish_recording(
            frame, object(), success=True, own_output=False, fact=fact
        )
        assert _staged(work) == [], "nothing staged in the user's folder"
        assert _staged(out) == [], "the staged copy is removed"
        if fails:
            assert published is None and seen == [[seen[0][0]]] and seen[0][0].startswith("recording-pub-1-")
        else:
            assert published is not None, facts
            assert (Path(frame.paths.artifacts) / published[0].rsplit("/", 1)[-1]).is_file()
    finally:
        await frame.close()
        await frame.destroy()


@darwin_only
@needs_chrome
async def test_a_change_is_made_in_out_with_a_real_screenshot_and_published(device: Device) -> None:
    """On the real helper, fence and Chrome: the agent's command writes the change in
    `/out`, a real screenshot lands beside it through the frame's door, a nested folder tars, and
    the suite reads what crossed. A save aimed at anything else is refused."""
    task_id = f"t{uuid.uuid4().hex[:8]}"
    frame = DeviceFrame(task_id, profile="browser")
    await frame.create(allow_domains=["a.test"], labels={"plane.kind": "machine"})
    try:
        assert (await frame.browser({"verb": "open", "url": f"http://a.test:{device.site.port}/c"})).get("ok")
        ports = BrowserPorts(browse=frame.browser, allow=frame.allow, save=frame.save_output)
        toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
        assert toolset is not None
        shot: Any = toolset.tools["browser_screenshot"].function
        ctx: Any = SimpleNamespace(tool_call_id="c1", messages=[])
        real = f"{frame.paths.out}/change/screenshots/home.png"
        assert await shot(ctx, save_as="/out/change/screenshots/home.png") == real
        assert (await shot(ctx, save_as="/task/a.png")).startswith("refused:")

        made = await frame.exec(
            "mkdir -p /out/change && cd /out/change && printf '# Done\\n' > report.md"
            " && printf -- '--- a/x\\n+++ b/x\\n@@ -0,0 +1 @@\\n+y\\n' > patch.diff"
            " && echo '1 passed' | tee test-output.txt".replace("/out", str(frame.paths.out))
        )
        assert made.ok, made.stderr

        await frame.publish(f"{frame.paths.out}/change", "change", "The change")

        published = await frame.read_bytes("/artifacts/change")
        checks = {c.name: c for c in change.check_change(published)}
        assert {c.status for c in checks.values()} == {CheckStatus.PASS}, checks
        assert [e["type"] for e in (await frame.manifest())["entries"]] == ["change"]
        assert os.listdir(frame.paths.work) == [], "nothing in the user's folder"
    finally:
        await frame.close()
        await frame.destroy()


@darwin_only
@needs_chrome
async def test_a_base_device_machine_moves_to_the_browser_before_the_relay_is_dialled(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device task whose first step was `base` and whose next step needs the
    browser goes through the profile-change wake (B5): the frame is re-created over the same ref
    `device-{task_id}` with the browser profile and keeps its folder, and only then does the first
    browser verb dial `/cdp`."""
    monkeypatch.setattr(settings.machines, "wait_s", 20)
    dialled: list[str] = []
    real_dial = DeviceFrame._dial  # pyright: ignore[reportPrivateUsage]

    async def dial(self: DeviceFrame) -> Any:
        dialled.append(device.profile_of(self.ref))  # what the helper holds when the worker dials
        return await real_dial(self)

    monkeypatch.setattr(DeviceFrame, "_dial", dial)
    sleeps = _run_machine_sleeps()
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="look something up")
        task.machine_driver = DEVICE
        step = PlanStep(task_id=task.id, ordinal=0, kind="step", state="running", title="look", spec={})
        s.add(step)
        await s.commit()
        task_id, step_id, workspace_id = task.id, step.id, workspace.id
    try:
        first = await _acquire(workspace_id, task_id, step_id, profile="base")
        assert first.frame.ref == f"device-{task_id}" and device.profile_of(first.frame.ref) == "base"
        await first.frame.write_bytes("/work/kept.md", b"the user's own file")
        assert await first.frame.browser({"verb": "snapshot"}) == {
            "ok": False,
            "error": "no browser in this frame",
        }
        assert dialled == [], "a base frame never dials the relay"
        await machines.release(first, parked=False)

        second = await _acquire(
            workspace_id, task_id, step_id, attempt=2, profile="browser", allow_domains=["a.test"]
        )
        assert sleeps == [SleepReason.UPGRADE], "the profile change went through the upgrade sleep"
        assert second.woke and second.frame.ref == first.frame.ref
        assert device.profile_of(second.frame.ref) == "browser"
        assert await second.frame.read_bytes("/work/kept.md") == b"the user's own file"
        async with row_scope(Task, task_id) as s:
            row = await Machine.for_task(s, task_id)
            assert row is not None and (row.profile, row.frame_ref) == ("browser", first.frame.ref)

        opened = await second.frame.browser({"verb": "open", "url": f"http://a.test:{device.site.port}/w"})
        assert opened.get("ok"), opened
        assert dialled == ["browser"], "the relay was dialled once, after the re-create"
        await machines.release(second, parked=False)
    finally:
        from prismoid.operations import dispatch

        dispatch.set_dispatcher(dispatch.NullDispatcher())


async def _acquire(
    workspace_id: uuid.UUID, task_id: uuid.UUID, step_id: uuid.UUID, *, attempt: int = 1, **kw: Any
) -> Any:
    async with session_scope(workspace_id=workspace_id) as session:
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        step.attempt = attempt
        kw.setdefault("allow_domains", [])
        return await machines.acquire(session, task, step, **kw)


def _run_machine_sleeps() -> list[SleepReason]:
    """A dispatcher that runs `machine_sleep` at once, as the worker would (the upgrade path
    `acquire` only dispatches); the reason of each sleep it ran, in order."""
    from prismoid.operations import dispatch

    ran: list[SleepReason] = []

    class Inline:
        async def enqueue(self, job: str, key: str, **kw: Any) -> None:
            if job != "machine_sleep":
                return
            claim = (
                machines.SleepClaim.from_wire(
                    claim_at=kw["claim_at"], after_s=kw["after_s"], since=kw["since"]
                )
                if kw.get("claimed")
                else None
            )
            ran.append(SleepReason(kw["reason"]))
            await machines.sleep(
                uuid.UUID(kw["machine_id"]), kw["epoch"], SleepReason(kw["reason"]), claim=claim
            )

        async def send(self, *args: Any, **kw: Any) -> None:
            return None

    dispatch.set_dispatcher(Inline())
    return ran
