# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a stop owes (ADR-078 decision 5): the cut command stays owed and is
told to the next park, a result that reached its worker is not told again, and the cut is reported in
this life too. The rig is `tests/support/rigs/device_stop_rig.py`."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.device import exec as device_exec
from prismoid.device.frames import (
    FrameRegistry,
    HelperStopping,
)
from prismoid.device.helper import KEY_HEADER
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import ExecIn, FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.markers import darwin_only
from tests.support.rigs.device_stop_rig import Helper, Spawned, until, written
from tests.support.rigs.device_stop_rig import home as home
from tests.support.rigs.device_stop_rig import spawned as spawned


@darwin_only
def test_a_finish_nobody_heard_is_told_to_the_next_park(home: Path, spawned: Spawned) -> None:
    """The stack stops the worker before the helper, so a command can end with its result going
    nowhere. The worker's connection goes for real here, and the journal says so."""
    helper = Helper(home, spawned)
    frame = helper.frame()
    ref, work = frame["ref"], Path(frame["paths"]["work"])
    gone = httpx.Client(
        transport=httpx.HTTPTransport(uds=str(home / "run" / "helper.sock")),
        base_url="http://helper",
        headers=dict(helper.door.headers),
        timeout=0.5,
    )
    command = "sleep 1; echo pushed > done; exit 5"
    with pytest.raises(httpx.ReadTimeout):
        gone.post(f"/frames/{ref}/exec", json={"exec_id": "gone", "command": command, "timeout": 30})
    gone.close()
    assert helper.door.get(f"/frames/{ref}/interrupted").json()["still_running"] is True
    until(lambda: (work / "done").exists(), 5, "it never finished")

    told = until(
        lambda: (
            (r := helper.door.get(f"/frames/{ref}/interrupted")).status_code == 200
            and r.json()["still_running"] is False
            and r.json()
        ),
        5,
        "its end was never told",
    )
    assert (told["exec_id"], told["exit_code"], told["command_head"]) == ("gone", 5, command)

    helper.stop()  # and across a helper life, until the worker has acted on it
    next_life = Helper(home, spawned)
    assert next_life.door.get(f"/frames/{ref}/interrupted").json()["exit_code"] == 5
    ack = next_life.door.post(f"/frames/{ref}/interrupted/ack", json={"exec_id": "gone"})
    assert ack.status_code == 204
    assert next_life.door.get(f"/frames/{ref}/interrupted").status_code == 204
    next_life.stop()


@darwin_only
def test_a_result_that_reached_its_worker_is_not_told_again(home: Path, spawned: Spawned) -> None:
    helper = Helper(home, spawned)
    ref = helper.frame()["ref"]
    assert helper.run(ref, "true").json()["exit_code"] == 0
    assert helper.door.get(f"/frames/{ref}/interrupted").status_code == 204
    helper.stop()


@darwin_only
def test_a_nan_timeout_runs_nothing_and_says_so(home: Path, spawned: Spawned) -> None:
    helper = Helper(home, spawned)
    frame = helper.frame()
    ref, work = frame["ref"], Path(frame["paths"]["work"])
    bad = b'{"exec_id": "e", "command": "touch ran", "timeout": NaN}'
    refused = helper.door.post(f"/frames/{ref}/exec", content=bad)
    assert refused.status_code == 422
    assert not (work / "ran").exists()
    assert helper.door.get(f"/frames/{ref}/interrupted").status_code == 204
    helper.stop()


@darwin_only
def test_a_nul_in_a_command_runs_no_part_of_it(home: Path, spawned: Spawned) -> None:
    """uvloop would hand the argv on cut at the NUL, and run what came before it."""
    helper = Helper(home, spawned)
    frame = helper.frame()
    ref, work = frame["ref"], Path(frame["paths"]["work"])
    out = helper.run(ref, "touch ran\0; rm -rf nothing").json()
    assert (out["exit_code"], out["stderr"]) == (126, "could not start the command: a NUL byte\n")
    assert not (work / "ran").exists()
    assert helper.door.get(f"/frames/{ref}/interrupted").status_code == 204
    helper.stop()


@darwin_only
async def test_the_cut_is_reported_in_this_life_too_and_nothing_new_starts(
    home: Path, spawned: Spawned
) -> None:
    """In-process, so the same life can be asked: the stop has happened, the helper still answers."""
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()
    registry: FrameRegistry = app.state.registry
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as door:
        made = (await door.post("/frames", json={"task_id": uuid.uuid4().hex[:8], "kind": "machine"})).json()
        ref, url = made["ref"], f"/frames/{made['ref']}/exec"
        done = await door.post(url, json={"exec_id": "done", "command": "true", "timeout": 10})
        assert done.status_code == 200  # a command that truly finished before the stop stays finished
        assert (await door.get(f"/frames/{ref}/interrupted")).status_code == 204

        pid_file = Path(made["paths"]["work"]) / "pid"
        body = {"exec_id": "cut", "command": "echo $$ > pid; sleep 300", "timeout": 20}
        cut = asyncio.create_task(door.post(url, json=body))
        await written(pid_file)
        spawned.note(int(pid_file.read_text()))
        await registry.stop()

        assert (await cut).status_code == 503
        said = (await door.get(f"/frames/{ref}/interrupted")).json()
        assert (said["exec_id"], said["still_running"]) == ("cut", False)

        late = await door.post(url, json={"exec_id": "late", "command": "true", "timeout": 10})
        assert late.status_code == 503
        said = (await door.get(f"/frames/{ref}/interrupted")).json()
        assert said["exec_id"] == "cut"  # the refused one never started: nothing to be told about it
    await app.state.egress.close()


@darwin_only
async def test_a_command_spawned_under_the_stop_is_cut_too(
    home: Path, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn is an await: a stop that walked the groups just before it never saw this one."""
    app = create_app(home, device_name=lambda: "Test Mac")
    registry: FrameRegistry = app.state.registry
    frame = registry.create(FrameSpecIn(task_id=uuid.uuid4().hex[:8], kind=FrameKind.MACHINE))
    real = asyncio.create_subprocess_exec

    async def spawn_then_stop(*argv: Any, **kw: Any) -> asyncio.subprocess.Process:
        proc = await real(*argv, **kw)
        if not registry.stopping.is_set():  # the command's spawn, not the `caffeinate` after it
            spawned.note(proc.pid)
            await registry.stop()
        return proc

    monkeypatch.setattr(device_exec.asyncio, "create_subprocess_exec", spawn_then_stop)
    started = time.monotonic()
    with pytest.raises(HelperStopping):
        await device_exec.run(
            frame, ExecIn(exec_id="raced", command="sleep 300", timeout=20), proxy=app.state.egress
        )
    monkeypatch.undo()
    assert time.monotonic() - started < 10  # killed, not waited for up to its timeout
    cut = frame.journal.interrupted()
    assert cut is not None and (cut.exec_id, cut.still_running) == ("raced", False)
    await app.state.egress.close()


@darwin_only
async def test_a_command_may_clean_up_and_a_clean_exit_is_still_a_cut(home: Path, spawned: Spawned) -> None:
    """SIGTERM comes first, and exit 0 under it is no more a finish than a SIGKILL is."""
    app = create_app(home, device_name=lambda: "Test Mac")
    registry: FrameRegistry = app.state.registry
    frame = registry.create(FrameSpecIn(task_id=uuid.uuid4().hex[:8], kind=FrameKind.MACHINE))
    work = Path(frame.paths.work)
    command = "trap 'sleep 0.5; echo bye > asked; exit 0' TERM; echo $$ > root.pid; sleep 300 & wait"
    running = asyncio.create_task(
        device_exec.run(frame, ExecIn(exec_id="polite", command=command, timeout=30), proxy=app.state.egress)
    )
    await written(work / "root.pid")
    spawned.note(int((work / "root.pid").read_text()))
    await registry.stop()
    with pytest.raises(HelperStopping):
        await running
    assert (work / "asked").read_text() == "bye\n"
    cut = frame.journal.interrupted()
    assert cut is not None and (cut.exec_id, cut.still_running) == ("polite", False)
    await app.state.egress.close()


@darwin_only
@pytest.mark.parametrize("how", ["made", "adopted", "re-created"])
async def test_every_frame_hears_the_stop(home: Path, spawned: Spawned, how: str) -> None:
    """Whichever way a frame's state came to be, and a re-create replaces it under its command."""
    spec = FrameSpecIn(task_id=uuid.uuid4().hex[:8], kind=FrameKind.MACHINE)
    if how == "adopted":  # the last helper life's frame
        FrameRegistry(home).create(spec)
    app = create_app(home, device_name=lambda: "Test Mac")
    registry: FrameRegistry = app.state.registry
    frame = registry.frames.get(f"device-{spec.task_id}") or registry.create(spec)
    work = Path(frame.paths.work)
    req = ExecIn(exec_id="cut", command="echo $$ > root.pid; sleep 300", timeout=30)
    running = asyncio.create_task(device_exec.run(frame, req, proxy=app.state.egress))
    await written(work / "root.pid")
    spawned.note(int((work / "root.pid").read_text()))
    if how == "re-created":
        registry.create(spec)
    await registry.stop()
    with pytest.raises(HelperStopping):
        await running
    cut = registry.frames[frame.ref].journal.interrupted()
    assert cut is not None and (cut.exec_id, cut.still_running) == ("cut", False)
    await app.state.egress.close()


@darwin_only
async def test_a_failure_past_the_spawn_never_reads_as_never_ran(
    home: Path, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver reads a 422 as "the command never ran": only the fence's refusal may say that."""
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()

    async def fails(_: int) -> None:
        raise ValueError("past the spawn")

    monkeypatch.setattr(device_exec, "_hold_awake", fails)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as door:
        ref = (await door.post("/frames", json={"task_id": uuid.uuid4().hex[:8], "kind": "machine"})).json()[
            "ref"
        ]
        body = {"exec_id": "e", "command": "sleep 300", "timeout": 30}
        with pytest.raises(ValueError, match="past the spawn"):  # a 500, to the driver a helper gone
            await door.post(f"/frames/{ref}/exec", json=body)
    await app.state.egress.close()
