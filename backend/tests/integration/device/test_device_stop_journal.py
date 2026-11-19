# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's frame file and journal across a stop (ADR-078 decision 5):
a frame file is written whole, a note outlives a life killed early, text no journal can hold is refused,
and a frame that raises stops nothing else. The rig is `tests/support/rigs/device_stop_rig.py`."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.device import chrome_pipe as chrome_module
from prismoid.device import exec as device_exec
from prismoid.device.frames import (
    FRAME_FILE,
    NOTICES_FILE,
    SWEEP_WAIT_S,
    FrameRegistry,
    HelperStopping,
)
from prismoid.device.helper import KEY_HEADER
from prismoid.device.plants import Planted
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import ExecIn, FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.markers import darwin_only
from tests.support.rigs.device_stop_rig import (
    PLANT,
    Helper,
    Running,
    Spawned,
    alive,
    grant,
    tree,
    until,
    written,
)
from tests.support.rigs.device_stop_rig import folder as folder
from tests.support.rigs.device_stop_rig import home as home
from tests.support.rigs.device_stop_rig import spawned as spawned


@darwin_only
def test_a_frame_file_is_written_whole(home: Path, spawned: Spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save that dies leaves the old file and no litter: a torn one is a frame the next helper
    cannot list, so its cut could not be asked for."""
    registry = FrameRegistry(home)
    frame = registry.create(FrameSpecIn(task_id=uuid.uuid4().hex[:8], kind=FrameKind.MACHINE))
    path = frame.base / FRAME_FILE
    before = path.read_text()
    frame.pinned_git = ("/a/repository/found/since",)

    def dies(*_: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", dies)
    with pytest.raises(OSError, match="no space"):
        frame.save()
    monkeypatch.undo()
    assert path.read_text() == before
    assert sorted(p.name for p in frame.base.iterdir() if p.name.startswith(f".{FRAME_FILE}")) == []
    registry.destroy(frame.ref)


def _saved_notes(running: Running | Path) -> list[str] | None:
    base = running.base if isinstance(running, Running) else running
    path = base / NOTICES_FILE
    return json.loads(path.read_text()) if path.exists() else None


@darwin_only
def test_a_note_outlives_a_life_killed_before_its_first_command(
    home: Path, spawned: Spawned, folder: Path
) -> None:
    """Loading it took nothing off the disk: it goes when a result that carried it was heard."""
    helper = Helper(home, spawned)
    command = f"{PLANT.format(d='cut')} echo $$ > root.pid; sleep 300"
    running = Running(helper, spawned, command, ("root",), grants=grant(folder))
    helper.stop()
    assert _saved_notes(running)

    second = Helper(home, spawned)
    second.proc.kill()
    second.proc.wait(timeout=10)

    third = Helper(home, spawned)
    told = third.run(running.ref, "true").json()["stderr"]
    assert "out-cut" in told
    assert _saved_notes(running) is None, "heard: gone"
    assert "out-cut" not in third.run(running.ref, "true").json()["stderr"], "told once"
    third.stop()


@darwin_only
def test_a_note_on_a_result_nobody_heard_waits_for_one_somebody_does(
    home: Path, spawned: Spawned, folder: Path
) -> None:
    helper = Helper(home, spawned)
    ref = helper.frame(grants=grant(folder))["ref"]
    base = home / "tasks" / ref.removeprefix("device-")
    gone = _client_that_leaves(helper)
    body = {"exec_id": "unseen", "command": f"sleep 1; {PLANT.format(d='x')}", "timeout": 30}
    with pytest.raises(httpx.ReadTimeout):
        gone.post(f"/frames/{ref}/exec", json=body)
    gone.close()
    until(lambda: _ended_unseen(helper, ref), 8, "no end")

    assert _saved_notes(base), "its result went nowhere, so its note did not go with it"
    assert "out-x" in helper.run(ref, "true").json()["stderr"]
    assert _saved_notes(base) is None
    helper.stop()


def _ended_unseen(helper: Helper, ref: str) -> bool:
    """Between its `finish` and its `unseen` the journal has nothing to tell: a 204."""
    said = helper.door.get(f"/frames/{ref}/interrupted")
    return said.status_code == 200 and said.json()["exit_code"] is not None


def _client_that_leaves(helper: Helper) -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(helper.home / "run" / "helper.sock")),
        base_url="http://helper",
        headers=dict(helper.door.headers),
        timeout=0.3,
    )


@darwin_only
def test_later_results_close_a_finish_nobody_heard(home: Path, spawned: Spawned) -> None:
    """Three commands on, it is not the last command any more."""
    helper = Helper(home, spawned)
    ref = helper.frame()["ref"]
    gone = _client_that_leaves(helper)
    body = {"exec_id": "old", "command": "sleep 1", "timeout": 30}
    with pytest.raises(httpx.ReadTimeout):
        gone.post(f"/frames/{ref}/exec", json=body)
    gone.close()
    until(lambda: _ended_unseen(helper, ref), 8, "no end")

    assert helper.run(ref, "true").json()["exit_code"] == 0

    assert helper.door.get(f"/frames/{ref}/interrupted").status_code == 204
    helper.stop()


@darwin_only
def test_text_no_journal_can_hold_is_refused_and_leaves_no_entry(home: Path, spawned: Spawned) -> None:
    """A lone surrogate, sent raw: a 126 like any command that cannot start, nothing to report, and
    the door still answers. Past the journal it would fail the spawn and poison `/interrupted`."""
    helper = Helper(home, spawned)
    ref = helper.frame()["ref"]
    for raw in (
        rb'{"exec_id": "s", "command": "echo \ud800", "timeout": 5}',
        rb'{"exec_id": "\ud800", "command": "true", "timeout": 5}',
    ):
        refused = helper.door.post(f"/frames/{ref}/exec", content=raw)
        assert refused.status_code == 200 and refused.json()["exit_code"] == 126
    assert helper.door.get(f"/frames/{ref}/interrupted").status_code == 204
    assert helper.run(ref, "true").json()["exit_code"] == 0
    helper.stop()


@darwin_only
def test_a_command_the_journal_cannot_write_is_refused_and_never_runs(home: Path, spawned: Spawned) -> None:
    helper = Helper(home, spawned)
    frame = helper.frame()
    ref, work = frame["ref"], Path(frame["paths"]["work"])
    assert helper.run(ref, "true").json()["exit_code"] == 0
    journal = home / "tasks" / ref.removeprefix("device-") / "journal.jsonl"
    journal.chmod(0o400)

    out = helper.run(ref, "touch ran").json()

    journal.chmod(0o600)
    assert out["exit_code"] == 126 and "could not record the command" in out["stderr"]
    assert not (work / "ran").exists()
    assert helper.door.get(f"/frames/{ref}/interrupted").status_code == 204
    helper.stop()


@darwin_only
async def test_a_walk_past_the_stops_bound_keeps_its_note(
    home: Path, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(home, device_name=lambda: "Test Mac")
    registry: FrameRegistry = app.state.registry
    frame = registry.create(FrameSpecIn(task_id=uuid.uuid4().hex[:8], kind=FrameKind.MACHINE))

    def slow(*_: Any, **__: Any) -> list[Planted]:
        time.sleep(SWEEP_WAIT_S + 0.5)
        return [Planted("/work/late", "link", True)]

    monkeypatch.setattr(frame.plants, "sweep", slow)
    await app.state.halt()
    assert _saved_notes(frame.base) is None, "the walk outran the stop's bound"
    assert frame.walk is not None
    await frame.walk
    await app.state.egress.close()
    notes = _saved_notes(frame.base)
    assert notes and "/work/late" in notes[0]


@darwin_only
async def test_a_frame_that_raises_stops_nothing_else(
    home: Path, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every frame is stopped and walked, and every closing step runs, whatever one of them raises."""
    app = create_app(home, device_name=lambda: "Test Mac")
    registry: FrameRegistry = app.state.registry
    bad = registry.create(FrameSpecIn(task_id=f"a{uuid.uuid4().hex[:8]}", kind=FrameKind.MACHINE))
    good = registry.create(FrameSpecIn(task_id=f"b{uuid.uuid4().hex[:8]}", kind=FrameKind.MACHINE))
    assert next(iter(registry.frames.values())) is bad
    work = Path(good.paths.work)
    # It needs the SIGKILL: a frame that fails must cost neither of the stop's two signals.
    command = "trap '' TERM; echo $$ > root.pid; sleep 300"
    req = ExecIn(exec_id="cut", command=command, timeout=30)
    walked: list[str] = []

    def raises(*_: Any) -> None:
        raise OSError("this frame's folder went away")

    async def walks() -> None:
        await asyncio.sleep(0.2)  # a failure elsewhere must not leave this walk behind
        walked.append(good.ref)

    async def raises_soon(*_: Any) -> None:
        raises()

    monkeypatch.setattr(bad.groups, "hold", raises)
    monkeypatch.setattr(bad.groups, "pids", raises)
    monkeypatch.setattr(bad, "sweep_within", raises_soon)
    monkeypatch.setattr(good, "sweep_within", walks)
    monkeypatch.setattr(app.state.dockers, "close_all", raises_soon)
    monkeypatch.setattr(app.state.ports, "close", raises_soon)
    closed: list[str] = []
    real_close = app.state.egress.close

    async def close() -> None:
        closed.append("egress")
        await real_close()

    monkeypatch.setattr(app.state.egress, "close", close)
    async with app.router.lifespan_context(app):
        running = asyncio.create_task(device_exec.run(good, req, proxy=app.state.egress))
        await written(work / "root.pid")
        pid = spawned.note(int((work / "root.pid").read_text()))
        for child in tree(pid):
            spawned.note(child)
    assert not alive(pid), "the bad frame cost the good one its stop"
    assert walked == [good.ref] and closed == ["egress"]
    with pytest.raises(HelperStopping):
        await running


@darwin_only
async def test_nothing_relaunches_chrome_once_the_stop_began(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launched(*_: Any) -> None:
        raise AssertionError("Chrome was launched")

    monkeypatch.setattr(chrome_module, "_launch", launched)
    app = create_app(home, device_name=lambda: "Test Mac", chrome_path="/bin/echo")
    key = (home / "helper.key").read_text().strip()
    await app.state.halt()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    ) as door:
        task = uuid.uuid4().hex[:8]
        frame = await door.post("/frames", json={"task_id": task, "kind": "machine", "profile": "browser"})
    assert frame.status_code == 503 and chrome_module.GONE in frame.text
    await app.state.egress.close()
