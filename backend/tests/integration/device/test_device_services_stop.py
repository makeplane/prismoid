# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task's servers stop: a server bound beyond loopback is
stopped and the agent told, a child that left its group is attributed by its parent's, and when the
Machine goes idle its leftovers and its containers stop. The rig is `tests/support/rigs/device_services_rig.py`."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import update

from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.device import docker_proxy, procs
from prismoid.device.ports import WIDE_REASON
from prismoid.operations import machines
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines.reconcile import sweep_once
from prismoid.schemas.machines import FrameDriverName, MachineState, SleepReason
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher
from tests.support.markers import darwin_only
from tests.support.rigs.device_services_rig import (
    IMAGE,
    Helper,
    alive,
    free_port,
    listening,
    services_docker,
    until,
)
from tests.support.rigs.device_services_rig import daemon as daemon
from tests.support.rigs.device_services_rig import folder as folder
from tests.support.rigs.device_services_rig import helper as helper
from tests.support.rigs.device_services_rig import stranger as stranger

pytestmark = darwin_only


async def test_a_foreground_server_beyond_loopback_is_stopped_while_it_runs(helper: Helper) -> None:
    """I5: a frame's very first command, in the foreground: no look waits for it to end."""
    ref = await helper.frame("tf")
    port = free_port()
    out = await helper.run(ref, f"exec /usr/bin/python3 -m http.server --bind 0.0.0.0 {port}", limit=20)
    assert out["exit_code"] == 137, out  # killed well before its 20 s
    assert f"*:{port}" in out["stderr"] and "127.0.0.1" in out["stderr"]


CHILD = """\
import os, socket, sys, time
pid = os.fork()
if pid:
    open(sys.argv[2], "w").write(str(pid))
    time.sleep(120)  # the parent stays: the ppid chain still proves the child
os.setsid()
s = socket.socket()
s.bind(("0.0.0.0", int(sys.argv[1])))
s.listen()
time.sleep(120)
"""


DAEMON = """\
import os, socket, sys, time
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
open(sys.argv[2], "w").write(str(os.getpid()))
s = socket.socket()
s.bind(("0.0.0.0", int(sys.argv[1])))
s.listen()
time.sleep(60)
"""


async def test_a_child_that_left_its_group_is_attributed_by_its_parents(helper: Helper) -> None:
    """I6: `setsid` leaves the group, not the family: the ppid chain still proves it the frame's."""
    ref = await helper.frame("tc")
    port = free_port()
    work = Path(helper.state(ref).paths.work)
    (work / "child.py").write_text(CHILD)
    await helper.spawn(ref, f"exec /usr/bin/python3 child.py {port} pid.txt")
    await until(lambda: listening(port), "the child's listener")
    child = int((work / "pid.txt").read_text())
    if (at := procs.started(child)) is not None:
        helper.started.append((child, at))
    await until(lambda: any(d["host"] == f"*:{port}" for d in helper.state(ref).denials), "the watch", 20)
    await until(lambda: not listening(port), "the child's end", 5)


async def test_a_daemon_nothing_can_prove_is_left_alone(helper: Helper) -> None:
    """I6's accepted limit: re-parented to launchd in a session of its own, it is nobody's that can
    be proved, and what cannot be proved is never killed."""
    ref = await helper.frame("tn")
    port = free_port()
    work = Path(helper.state(ref).paths.work)
    (work / "daemon.py").write_text(DAEMON)
    await helper.run(ref, f"/usr/bin/python3 daemon.py {port} pid.txt")
    await until(lambda: (work / "pid.txt").exists() and (work / "pid.txt").read_text(), "the daemon's pid")
    daemon = int((work / "pid.txt").read_text())
    at = procs.started(daemon)
    assert at is not None
    helper.started.append((daemon, at))
    await until(lambda: listening(port), "the daemon's listener")
    await asyncio.sleep(3.0)
    assert alive(daemon, at)
    assert not any(str(port) in d["host"] for d in helper.state(ref).denials)


async def test_a_server_beyond_loopback_is_stopped_and_the_agent_told(
    helper: Helper, stranger: tuple[subprocess.Popen[bytes], int]
) -> None:
    """Bound on 0.0.0.0 (the fence cannot refuse the bind): killed at the end of the command that
    started it, or by the watch between commands, and the agent told to bind 127.0.0.1. A
    stranger's server on 0.0.0.0 is never touched."""
    process, stranger_port = stranger
    await until(lambda: listening(stranger_port), "the stranger's server")
    ref = await helper.frame("tw")
    port = free_port()
    out = await helper.run(
        ref,
        f"/usr/bin/python3 -m http.server --bind 0.0.0.0 {port} > /dev/null 2>&1 & "
        f"for i in $(seq 100); do nc -z 127.0.0.1 {port} && break; sleep 0.05; done",
    )
    assert f"stopped the server listening on *:{port}" in out["stderr"] and "127.0.0.1" in out["stderr"]
    assert not listening(port)
    assert any(d["host"] == f"*:{port}" and d["reason"] == WIDE_REASON for d in helper.state(ref).denials)

    late = free_port()  # starts after its command ended: the watch finds it
    await helper.run(
        ref, f"(sleep 1; exec /usr/bin/python3 -m http.server --bind 0.0.0.0 {late}) > /dev/null 2>&1 &"
    )
    await until(lambda: any(d["host"] == f"*:{late}" for d in helper.state(ref).denials), "the watch", 20)
    assert not listening(late)
    out = await helper.run(ref, "true")
    assert f"*:{late}" in out["stderr"]  # told with its next command

    assert process.poll() is None and listening(stranger_port)


async def test_stop_services_kills_leftovers_and_containers(
    helper: Helper, folder: Path, daemon: None
) -> None:
    ref = await helper.frame("td", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}], docker=True)
    try:
        pid = await helper.spawn(ref, "sleep 300")
        at = procs.started(pid)
        port = free_port()
        out = await helper.run(ref, f"docker run -d -p 127.0.0.1:{port}:80 {IMAGE} sleep 300", limit=240)
        assert out["exit_code"] == 0, out
        container = out["stdout"].strip().splitlines()[-1]
        # Its published port is the frame's own (reached through Docker, not a frame process).
        assert await helper.app.state.dockers.published(ref) == {("127.0.0.1", port)}
        assert await helper.app.state.ports.literals([helper.state(ref)], "localhost", port) == ["127.0.0.1"]

        async with helper.client() as c:
            assert (await c.post(f"/frames/{ref}/stop-services")).status_code == 204
        assert not alive(pid, at)
        state = json.loads(services_docker("inspect", container).stdout)[0]["State"]
        assert state["Running"] is False
        assert (await helper.run(ref, "echo still")).get("stdout") == "still\n"  # the frame stays
    finally:
        ids = services_docker(
            "ps", "-aq", "--filter", f"label={docker_proxy.FRAME_LABEL}={ref}", check=False
        ).stdout.split()
        if ids:
            services_docker("rm", "-f", *ids, check=False)


@pytest.mark.integration
async def test_machine_idle_sleep_stops_services(
    helper: Helper, recording_dispatcher: RecordingDispatcher
) -> None:
    """B5's sleep branch, end to end: a device Machine whose step left a server running goes idle,
    the sweep's idle arm sleeps it, and the sleep stops what it left running - on the real helper."""
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="serve it", clarify="never")
        task.machine_driver = FrameDriverName.DEVICE
        step = PlanStep(
            id=uuid.uuid4(), task_id=task.id, ordinal=0, kind="step", state="running", title="serve", spec={}
        )
        s.add(step)
        await s.commit()
        task_id, step_id = task.id, step.id
    async with session_scope(workspace_id=workspace.id) as session:
        t, st = await session.get(Task, task_id), await session.get(PlanStep, step_id)
        assert t is not None and st is not None
        st.attempt = 1
        handle = await machines.acquire(session, t, st, allow_domains=[], profile="base")
    port = free_port()
    out = await handle.frame.exec(
        f"/usr/bin/python3 -m http.server --bind 127.0.0.1 {port} > /dev/null 2>&1 & echo $!"
    )
    pid = int(out.stdout.split()[-1])
    at = procs.started(pid)
    assert at is not None
    helper.started.append((pid, at))
    await until(lambda: listening(port), "the step's server")
    await machines.release(handle, parked=False)

    async with session_scope(workspace_id=workspace.id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None
        machine_id, epoch = row.id, row.epoch
        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=settings.machines.idle_s + 60)
        await s.execute(update(Machine).where(Machine.id == machine_id).values(last_active_at=old))
        await s.commit()
    await sweep_once(dt.datetime.now(dt.UTC))
    assert any(
        job == "machine_sleep" and str(machine_id) in key for job, key in recording_dispatcher.enqueues
    )
    assert await machines.sleep(machine_id, epoch, SleepReason.IDLE) == "slept"  # what that job runs

    assert not alive(pid, at) and not listening(port)
    async with session_scope(workspace_id=workspace.id) as s:
        row = await Machine.for_task(s, task_id)
        assert row is not None and row.state == MachineState.ASLEEP
    assert helper.state(f"device-{task_id}") is not None  # the frame is kept: a wake binds it
