# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device helper: its door, its folders and its registry."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from prismoid.conf import settings
from prismoid.device.frames import FrameRegistry
from prismoid.device.helper import KEY_HEADER, HelperKeyError
from prismoid.runner.dev import dev
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind

NAME = "Test Mac"


def _client(home: Path, key: str | None = None) -> httpx.AsyncClient:
    app = create_app(home, device_name=lambda: NAME)
    key = key if key is not None else (home / "helper.key").read_text().strip()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The helper's home; the user's own folders (`tmp_path / "proj"`) live beside it, never in it."""
    return tmp_path / "home"


@pytest.fixture
async def helper(home: Path) -> AsyncIterator[httpx.AsyncClient]:
    create_app(home, device_name=lambda: NAME)
    async with _client(home) as c:
        yield c


async def test_requests_without_key_are_401(home: Path) -> None:
    create_app(home, device_name=lambda: NAME)
    async with _client(home, key="wrong") as c:
        for method, url in (("GET", "/healthz"), ("GET", "/frames"), ("DELETE", "/frames/x")):
            assert (await c.request(method, url)).status_code == 401
        c.headers.pop(KEY_HEADER)
        assert (await c.get("/healthz")).status_code == 401
    assert ((home / "helper.key").stat().st_mode & 0o777) == 0o600


async def test_healthz_names_the_device(helper: httpx.AsyncClient) -> None:
    assert (await helper.get("/healthz")).json() == {"ok": True, "device_name": NAME}


async def test_create_frame_makes_task_folders(helper: httpx.AsyncClient, home: Path, tmp_path: Path) -> None:
    out = (await helper.post("/frames", json={"task_id": "t1", "kind": "machine"})).json()
    assert out["ref"] == "device-t1" and out["device_name"] == NAME
    base = home / "tasks" / "t1"
    for d in ("task", "artifacts", "skills", "verify", "out", "work"):
        assert (base / d).is_dir()
    assert out["paths"]["work"] == str(base / "work")

    work = tmp_path / "proj"
    work.mkdir()
    grant = {"path": str(work), "mode": "rw"}
    out = (
        await helper.post(
            "/frames",
            json={"task_id": "t2", "kind": "machine", "grants": [{"path": "/ro", "mode": "ro"}, grant]},
        )
    ).json()
    assert out["paths"]["work"] == str(work.resolve())


async def test_seat_frame_lives_in_scratch(helper: httpx.AsyncClient, home: Path, tmp_path: Path) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    spec = {"task_id": "t1", "kind": "seat", "grants": [{"path": str(work), "mode": "rw"}]}
    out = (await helper.post("/frames", json=spec)).json()
    ref = out["ref"]
    assert ref.startswith("device-seat-") and len(ref) == len("device-seat-") + 8
    assert out["paths"]["work"] == str(home / "scratch" / ref / "work")
    assert (home / "scratch" / ref / "work").is_dir()
    assert not (home / "tasks" / "t1").exists()


async def test_delete_removes_prismoid_folder_never_work(
    helper: httpx.AsyncClient, home: Path, tmp_path: Path
) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    (work / "keep.txt").write_text("mine")
    spec = {"task_id": "t1", "kind": "machine", "grants": [{"path": str(work), "mode": "rw"}]}
    await helper.post("/frames", json=spec)
    assert (await helper.delete("/frames/device-t1")).status_code == 204
    assert (work / "keep.txt").read_text() == "mine"
    assert not (home / "tasks" / "t1").exists()
    assert (await helper.get("/frames/device-t1")).status_code == 404

    seat = (await helper.post("/frames", json={"task_id": "t1", "kind": "seat"})).json()["ref"]
    await helper.delete(f"/frames/{seat}")
    assert not (home / "scratch" / seat).exists()


async def test_list_frames_survives_restart(home: Path) -> None:
    create_app(home, device_name=lambda: NAME)
    async with _client(home) as c:
        await c.post("/frames", json={"task_id": "t1", "kind": "machine"})
        seat = (await c.post("/frames", json={"task_id": "t1", "kind": "seat"})).json()["ref"]
    async with _client(home) as c:  # a new process: the registry is rebuilt from disk
        refs = {f["ref"] for f in (await c.get("/frames")).json()}
        assert refs == {"device-t1", seat}
        assert (await c.get("/frames/device-t1")).json()["paths"]["task"].endswith("tasks/t1/task")
    assert "token" not in (home / "tasks" / "t1" / "frame.json").read_text()


async def test_frame_routes_answer(helper: httpx.AsyncClient) -> None:
    """B1's 501 stubs are B3's routes now (the fenced exec itself is `test_device_exec`'s, on a Mac)."""
    paths = (await helper.post("/frames", json={"task_id": "t1", "kind": "machine"})).json()["paths"]
    for method, route in (("POST", "exec"), ("POST", "files/read"), ("POST", "signal"), ("PUT", "egress")):
        assert (await helper.request(method, f"/frames/device-t1/{route}", json={})).status_code == 422
        assert (await helper.request(method, f"/frames/nope/{route}", json={})).status_code == 404
    data = base64.b64encode(b"hi").decode()
    put = await helper.post(
        "/frames/device-t1/files/write", json={"path": f"{paths['work']}/a", "data_b64": data}
    )
    assert put.status_code == 204
    assert (await helper.post("/frames/device-t1/files/list", json={"path": paths["work"]})).json() == {
        "names": ["a"]
    }
    bad = await helper.post(
        "/frames/device-t1/files/write", json={"path": f"{paths['work']}/b", "data_b64": "@@"}
    )
    assert bad.status_code == 422
    assert (
        await helper.put("/frames/device-t1/egress", json={"domains": ["x.example"], "version": 5})
    ).json()["version"] == 5
    assert (await helper.get("/frames/device-t1/denials")).json() == {"denials": []}
    assert (await helper.get("/frames/device-t1/interrupted")).status_code == 204
    assert (await helper.post("/frames/device-t1/signal", json={"signal": "stop"})).status_code == 204
    nul = await helper.post("/frames/device-t1/files/read", json={"path": f"{paths['work']}/a\u0000b"})
    assert nul.status_code == 422  # a bad path is the caller's error, never a 500
    ack = await helper.post("/frames/device-t1/interrupted/ack", json={"exec_id": "e1"})
    assert ack.status_code == 204
    assert (await helper.post("/frames/device-t1/interrupted/ack", json={})).status_code == 422


async def test_an_oversized_file_is_413(helper: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.device import files

    monkeypatch.setattr(files, "MAX_FILE_BYTES", 4)
    paths = (await helper.post("/frames", json={"task_id": "t1", "kind": "machine"})).json()["paths"]
    data = base64.b64encode(b"too big").decode()
    big = await helper.post(
        "/frames/device-t1/files/write", json={"path": f"{paths['work']}/b", "data_b64": data}
    )
    assert big.status_code == 413


def test_empty_key_file_refuses_to_start(home: Path) -> None:
    home.mkdir()
    (home / "helper.key").write_text("")
    (home / "helper.key").chmod(0o600)
    with pytest.raises(HelperKeyError):
        create_app(home, device_name=lambda: NAME)
    (home / "helper.key").write_text("k")
    (home / "helper.key").chmod(0o644)
    with pytest.raises(HelperKeyError):
        create_app(home, device_name=lambda: NAME)


async def test_missing_or_empty_header_is_401(helper: httpx.AsyncClient) -> None:
    helper.headers.pop(KEY_HEADER)
    assert (await helper.get("/healthz")).status_code == 401
    assert (await helper.get("/healthz", headers={KEY_HEADER: ""})).status_code == 401


async def test_grants_overlapping_the_home_are_422(helper: httpx.AsyncClient, home: Path) -> None:
    (home / "tasks").mkdir(exist_ok=True)
    for bad in (home, home / "tasks", home.parent, home / ".." / home.name):
        spec = {"task_id": "g", "kind": "machine", "grants": [{"path": str(bad), "mode": "rw"}]}
        assert (await helper.post("/frames", json=spec)).status_code == 422
    assert (await helper.get("/frames")).json() == []


async def test_traversal_task_id_is_422(helper: httpx.AsyncClient) -> None:
    assert (await helper.post("/frames", json={"task_id": "../x", "kind": "machine"})).status_code == 422


async def test_rebuild_ignores_a_forged_ref(home: Path) -> None:
    create_app(home, device_name=lambda: NAME)
    async with _client(home) as c:
        await c.post("/frames", json={"task_id": "t1", "kind": "machine"})
    marker = home / "tasks" / "t1" / "frame.json"
    data = json.loads(marker.read_text())
    data["ref"] = "device-evil"
    marker.write_text(json.dumps(data))
    async with _client(home) as c:
        assert [f["ref"] for f in (await c.get("/frames")).json()] == ["device-t1"]


def test_cli_startup_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real `prismoid device helper` path (uvicorn stubbed): run/ 0700, socket 0600, the process
    umask untouched so a frame folder made afterwards is traversable."""
    from typer.testing import CliRunner

    from prismoid.runner.main import app

    tmp_path = Path(tempfile.mkdtemp(prefix="ph", dir="/tmp"))  # a socket path must fit sun_path
    seen: list[object] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: seen.extend(sockets or []))
    before = os.umask(0o022)
    try:
        res = CliRunner().invoke(app, ["device", "helper", "--home", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert os.umask(0o022) == 0o022  # restored, not left narrowed
        assert (tmp_path / "run").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "run" / "helper.sock").stat().st_mode & 0o777 == 0o600
        FrameRegistry(tmp_path).create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE))
        assert (tmp_path / "tasks" / "t1").stat().st_mode & 0o700 == 0o700
        assert (tmp_path / "tasks" / "t1" / "work").stat().st_mode & 0o755 == 0o755
    finally:
        os.umask(before)
        shutil.rmtree(tmp_path, ignore_errors=True)
        for s in seen:
            s.close()  # pyright: ignore[reportAttributeAccessIssue]


async def test_reset_mounts_clears_what_the_last_step_mounted(home: Path, helper: httpx.AsyncClient) -> None:
    out = (await helper.post("/frames", json={"task_id": "t1", "kind": "machine"})).json()
    task, skills = Path(out["paths"]["task"]), Path(out["paths"]["skills"])
    (task / "inputs" / "src.md").write_text("given")
    (task / "inputs" / "dir").mkdir()
    (task / "inputs" / "dir" / "a").write_text("x")
    (skills / "draft").mkdir()
    (skills / "draft" / "SKILL.md").write_text("d")
    for name in ("answers.json", "context.md", "thread.md", "keep.txt"):
        (task / name).write_text("x")
    assert (await helper.post("/frames/device-t1/reset_mounts", json={})).status_code == 204
    assert sorted(os.listdir(task)) == ["inputs", "keep.txt"]
    assert os.listdir(task / "inputs") == [] and os.listdir(skills) == []
    assert (await helper.post("/frames/device-nope/reset_mounts", json={})).status_code == 404


async def test_a_frame_keeps_and_lists_its_labels(home: Path, helper: httpx.AsyncClient) -> None:
    await helper.post(
        "/frames", json={"task_id": "t1", "kind": "machine", "labels": {"plane.kind": "machine"}}
    )
    listed = (await helper.get("/frames/device-t1")).json()
    assert listed["labels"] == {"plane.kind": "machine"} and listed["created_at"]


async def test_a_wider_allowlist_forgets_the_old_denials(home: Path, helper: httpx.AsyncClient) -> None:
    await helper.post("/frames", json={"task_id": "t1", "kind": "machine"})
    from prismoid.device.egress import apply_allow

    state = FrameRegistry(home).frames["device-t1"]
    state.denials.append({"host": "x.example"})
    apply_allow(state, ["x.example"], None)
    assert list(state.denials) == []


def _short_home() -> Path:
    """A short, resolved folder: a socket path must fit sun_path, and `/tmp` is `/private/tmp`."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="pd", dir="/tmp")))


@pytest.fixture
async def served(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    """A real helper on its Unix socket (`prismoid dev device-ping` dials it as the worker does)."""
    home = _short_home()
    monkeypatch.setattr(settings.local, "home", home)
    app = create_app(home, device_name=lambda: NAME)
    server = uvicorn.Server(uvicorn.Config(app, uds=str(home / "run" / "helper.sock"), log_level="critical"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.02)
    try:
        yield home
    finally:
        server.should_exit = True
        await serving
        shutil.rmtree(home, ignore_errors=True)


def _said(stdout: str) -> dict[str, Any]:
    """The JSON: logfire, once an earlier test in this process instrumented httpx, prints a line first."""
    return json.loads(stdout[stdout.index("{") :])


async def test_device_ping_reports_a_helper_that_answers(served: Path) -> None:
    done = await asyncio.to_thread(CliRunner().invoke, dev, ["device-ping", "--json"])

    assert done.exit_code == 0
    said = _said(done.stdout)
    assert said["ok"] is True and said["device_name"] == NAME
    assert said["socket"] == str(served / "run" / "helper.sock")

    done = await asyncio.to_thread(CliRunner().invoke, dev, ["device-ping"])
    assert done.exit_code == 0 and NAME in done.stdout


async def test_device_ping_says_a_refused_key_is_a_sick_helper(served: Path) -> None:
    (served / "helper.key").write_text("not the key the helper holds")

    done = await asyncio.to_thread(CliRunner().invoke, dev, ["device-ping", "--json"])

    assert done.exit_code == 1
    assert _said(done.stdout) == {
        "ok": False,
        "device_name": None,
        "socket": str(served / "run" / "helper.sock"),
        "error": "the device helper answered 401",
    }


def test_device_ping_says_no_helper_is_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings.local, "home", tmp_path)

    done = CliRunner().invoke(dev, ["device-ping"])

    assert done.exit_code == 1 and "key is unreadable" in done.stderr
