# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""DeviceFrame: the worker-side driver, the helper faked at the
HTTP edge with `httpx.MockTransport`. What the helper really does is B1-B3's tests and the device
conformance run; here is what the driver sends, what it makes of the answers, and what it refuses
to let out (a real path in anything a tool or the journal records). This file is create, exec, files,
publish and bind; the rig is `tests/support/rigs/device_driver_rig.py`."""

from __future__ import annotations

import base64
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.frames.driver import FrameDriver, FrameUnreachable
from prismoid.ai.frames.layout import OutsideFrame
from prismoid.errors import PolicyDenied
from tests.support.rigs.device_driver_rig import BASE, PATHS, Helper, b64, made
from tests.support.rigs.device_driver_rig import helper as helper
from tests.support.rigs.device_driver_rig import helper_home as helper_home


async def test_create_sends_the_spec_and_reads_back_the_roots(helper: Helper) -> None:
    frame = await made(helper)
    spec = helper.bodies("/frames")[0]
    assert spec["task_id"] == "t1" and spec["kind"] == "machine"
    assert spec["grants"] == [] and spec["allow_domains"] == ["a.example"] and spec["egress_version"] == 1
    assert frame.ref == "device-t1" and frame.device_name == "MacBook Pro"
    assert frame.paths.on_host and frame.paths.work == "/Users/me/code/demo" and frame.paths.out == PATHS.out
    assert frame.driver == "device" and frame.sleep_kind == "none"
    assert isinstance(frame, FrameDriver)


async def test_exec_maps_virtual_cwd_and_paths(helper: Helper) -> None:
    helper.on(
        "POST",
        "/frames/device-t1/exec",
        httpx.Response(200, json={"exit_code": 0, "stdout": "ok\n", "stderr": "", "truncated": False}),
    )
    frame = await made(helper)
    out = await frame.exec("cat /work/a.txt /task/plan.md", timeout=5)
    assert out.ok and out.stdout == "ok\n"
    sent = helper.bodies("/frames/device-t1/exec")[0]
    assert sent["command"] == f"cat /Users/me/code/demo/a.txt {BASE}/task/plan.md" and sent["timeout"] == 5
    assert sent["system"] is False and sent["exec_id"]
    await frame.exec("ls", cwd="/work/sub")  # virtual cwd
    assert helper.bodies("/frames/device-t1/exec")[1]["command"] == "cd /Users/me/code/demo/sub && ls"
    await frame.exec("ls", cwd="/Users/me/code/demo/sub")  # the real spelling, as is
    assert helper.bodies("/frames/device-t1/exec")[2]["command"] == "cd /Users/me/code/demo/sub && ls"
    with pytest.raises(OutsideFrame):
        await frame.exec("ls", cwd="/Users/me/.ssh")


async def test_exec_truncation_and_spawn_errors(helper: Helper) -> None:
    answers = iter(
        [
            httpx.Response(200, json={"exit_code": 0, "stdout": "x", "stderr": "", "truncated": True}),
            httpx.Response(
                200,
                json={
                    "exit_code": 126,
                    "stdout": "",
                    "stderr": f"could not start the command: {PATHS.work}/run: denied\n",
                    "truncated": False,
                },
            ),
            httpx.Response(422, json={"detail": f"the fence refused this frame: {PATHS.artifacts} is bad"}),
        ]
    )
    helper.on("POST", "/frames/device-t1/exec", lambda _b: next(answers))
    frame = await made(helper)
    cut = await frame.exec("yes")
    assert cut.stdout == "x" and cut.stderr == "[output truncated by the device]\n"
    started = await frame.exec("./run")
    assert started.exit_code == 126 and PATHS.work not in started.stderr and "/work/run" in started.stderr
    refused = await frame.exec("ls")
    assert refused.exit_code == 126 and BASE not in refused.stderr and "/artifacts" in refused.stderr


async def test_files_accept_either_spelling_and_refuse_the_rest(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/files/write", httpx.Response(204))
    helper.on(
        "POST",
        "/frames/device-t1/files/read",
        lambda b: httpx.Response(200, json={"data_b64": b64(b"h\xc3\xa9llo")}),
    )
    helper.on("POST", "/frames/device-t1/files/list", httpx.Response(200, json={"names": ["a", "d/"]}))
    frame = await made(helper)
    await frame.write_file("/work/n.md", "x")
    await frame.write_bytes("/Users/me/code/demo/m.bin", b"\x00", mode=0o444, owner="system")
    writes = helper.bodies("/frames/device-t1/files/write")
    assert [w["path"] for w in writes] == ["/Users/me/code/demo/n.md", "/Users/me/code/demo/m.bin"]
    assert writes[1]["mode"] == 0o444 and writes[1]["owner"] == "system" and writes[0]["owner"] == "agent"
    assert await frame.read_file("/work/n.md") == "héllo"
    assert await frame.read_bytes("/Users/me/code/demo/n.md") == "héllo".encode()
    assert await frame.list_dir("/work") == ["a", "d/"]
    assert await frame.list_dir(PATHS.work) == ["a", "d/"]
    reads = helper.bodies("/frames/device-t1/files/read") + helper.bodies("/frames/device-t1/files/list")
    assert {r["path"] for r in reads} == {"/Users/me/code/demo"} | {"/Users/me/code/demo/n.md"}
    for verb in (frame.read_file, frame.read_bytes, frame.list_dir):
        with pytest.raises(OutsideFrame):
            await verb("/Users/me/.ssh/id_rsa")
    with pytest.raises(OutsideFrame):
        await frame.write_file("/etc/passwd", "x")
    with pytest.raises(OutsideFrame):
        await frame.read_file("/work/../../.ssh/id")  # normalised before the root test


async def test_a_helper_refusal_carries_only_virtual_roots(helper: Helper) -> None:
    secret = f"{PATHS.work}/.env"
    helper.on(
        "POST", "/frames/device-t1/files/read", httpx.Response(403, json={"detail": f"{secret} is protected"})
    )
    helper.on(
        "POST",
        "/frames/device-t1/files/write",
        httpx.Response(403, json={"detail": f"{BASE}/skills/x is not writable"}),
    )
    helper.on("POST", "/frames/device-t1/files/list", httpx.Response(404, json={"detail": "gone"}))
    frame = await made(helper)
    with pytest.raises(PermissionError) as read:
        await frame.read_file("/work/.env")
    assert "/work/.env is protected" in str(read.value) and "/Users" not in str(read.value)
    with pytest.raises(PermissionError) as write:
        await frame.write_file("/work/y", "x")
    assert "/skills/x" in str(write.value) and "/Users" not in str(write.value)
    assert (
        frame.virtual_text(f"/elsewhere{PATHS.work}/x and {PATHS.work}/y")
        == f"/elsewhere{PATHS.work}/x and /work/y"
    )
    assert await frame.list_dir("/work/absent") == []  # every driver: a directory not there lists as []


async def test_missing_file_is_file_not_found(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/files/read", httpx.Response(404, json={"detail": f"{PATHS.work}/q"}))
    frame = await made(helper)
    with pytest.raises(FileNotFoundError) as e:
        await frame.read_bytes("/work/q")
    assert "/Users" not in str(e.value)


async def test_manifest_paths_are_virtual(helper: Helper) -> None:
    stored = {
        "entries": [
            {"path": f"{PATHS.artifacts}/r.md", "sources": [f"{PATHS.inputs}/s.md", "kept"], "title": "R"}
        ]
    }
    helper.on(
        "POST",
        "/frames/device-t1/files/read",
        httpx.Response(200, json={"data_b64": b64(json.dumps(stored).encode())}),
    )
    frame = await made(helper)
    entry = (await frame.manifest())["entries"][0]
    assert entry["path"] == "/artifacts/r.md" and entry["sources"] == ["/task/inputs/s.md", "kept"]
    assert helper.bodies("/frames/device-t1/files/read")[0]["path"] == f"{PATHS.artifacts}/manifest.json"


async def test_manifest_is_empty_when_nothing_was_published(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/files/read", httpx.Response(404, json={"detail": "x"}))
    assert await (await made(helper)).manifest() == {"entries": []}


async def test_publish_writes_through_the_system_door_and_the_manifest_is_virtual(helper: Helper) -> None:
    files: dict[str, bytes] = {f"{PATHS.work}/r.md": b"# R\n"}

    def read(b: dict[str, Any]) -> httpx.Response:
        if b["path"] in files:
            return httpx.Response(200, json={"data_b64": b64(files[b["path"]])})
        return httpx.Response(404, json={"detail": "no"})

    def write(b: dict[str, Any]) -> httpx.Response:
        files[b["path"]] = base64.b64decode(b["data_b64"])
        return httpx.Response(204)

    helper.on("POST", "/frames/device-t1/files/read", read)
    helper.on("POST", "/frames/device-t1/files/write", write)
    helper.on("POST", "/frames/device-t1/files/list", httpx.Response(200, json={"names": []}))
    frame = await made(helper)
    await frame.publish("/work/r.md", "document", "R", sources=[f"{PATHS.inputs}/s.md"])
    await frame.publish(f"{PATHS.work}/r.md", "document", "R2")
    assert files[f"{PATHS.artifacts}/r.md"] == b"# R\n"
    writes = helper.bodies("/frames/device-t1/files/write")
    assert all(w["owner"] == "system" for w in writes)
    entries = json.loads(files[f"{PATHS.artifacts}/manifest.json"])["entries"]
    assert [e["path"] for e in entries] == ["/artifacts/r.md", "/artifacts/r.md"]
    assert entries[0]["sources"] == ["/task/inputs/s.md"] and entries[0]["title"] == "R"
    assert entries[0]["sha256"] and entries[0]["bytes"] == 4 and "/Users" not in json.dumps(entries)
    with pytest.raises(PolicyDenied, match="no such file"):
        await frame.publish("/work/absent.md", "document", "A")
    with pytest.raises(PolicyDenied, match="not a valid"):
        await frame.publish("/work/r.md", "spreadsheet", "A")


async def test_publish_tars_a_directory(helper: Helper) -> None:
    files: dict[str, bytes] = {
        f"{PATHS.work}/site/index.html": b"<h1>",
        f"{PATHS.work}/site/css/a.css": b"b{}",
    }
    listing = {f"{PATHS.work}/site": ["css/", "index.html"], f"{PATHS.work}/site/css": ["a.css"]}

    helper.on(
        "POST",
        "/frames/device-t1/files/list",
        lambda b: httpx.Response(200, json={"names": listing.get(b["path"], [])}),
    )
    helper.on(
        "POST",
        "/frames/device-t1/files/read",
        lambda b: (
            httpx.Response(200, json={"data_b64": b64(files[b["path"]])})
            if b["path"] in files
            else httpx.Response(404, json={"detail": "n"})
        ),
    )
    helper.on(
        "POST",
        "/frames/device-t1/files/write",
        lambda b: files.__setitem__(b["path"], base64.b64decode(b["data_b64"])) or httpx.Response(204),
    )
    frame = await made(helper)
    await frame.publish("/work/site", "webapp", "Site")
    with tarfile.open(fileobj=io.BytesIO(files[f"{PATHS.artifacts}/site"])) as tar:
        names = tar.getnames()
    assert sorted(names) == ["site", "site/css", "site/css/a.css", "site/index.html"]


async def test_bind_unreachable_is_frame_unreachable_not_runtime_error(helper: Helper) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no socket")

    frame = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(down))
    with pytest.raises(FrameUnreachable):
        await frame.bind()


async def test_bind_unknown_ref_is_runtime_error(helper: Helper) -> None:
    helper.routes.pop(("GET", "/frames/device-t1"))
    frame = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    with pytest.raises(RuntimeError) as e:
        await frame.bind()
    assert not isinstance(e.value, FrameUnreachable)


async def test_bind_known_ref_adopts_paths_and_device_name(helper: Helper) -> None:
    frame = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    assert await frame.bind() is True
    assert frame.paths.work == PATHS.work and frame.device_name == "MacBook Pro" and frame.ref == "device-t1"


async def test_a_wrong_key_or_a_server_error_is_unreachable_not_gone(helper: Helper) -> None:
    helper.on("GET", "/frames/device-t1", httpx.Response(401, json={"detail": "unauthorized"}))
    frame = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    with pytest.raises(FrameUnreachable):
        await frame.bind()
    helper.on("GET", "/frames/device-t1", httpx.Response(500, text="boom"))
    with pytest.raises(FrameUnreachable):
        await frame.bind()


async def test_missing_key_file_is_unreachable(helper: Helper, tmp_path: Path) -> None:
    (tmp_path / "helper.key").unlink()
    with pytest.raises(FrameUnreachable):
        await DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper)).bind()


async def test_freeze_thaw_signal_and_destroy(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/signal", httpx.Response(204))
    helper.on("DELETE", "/frames/device-t1", httpx.Response(204))
    frame = await made(helper)
    await frame.freeze()
    await frame.thaw()
    assert [b["signal"] for b in helper.bodies("/frames/device-t1/signal")] == ["stop", "cont"]
    await frame.destroy()
    helper.on("DELETE", "/frames/device-t1", httpx.Response(404, json={"detail": "no such frame"}))
    await frame.destroy()  # a missing frame is already destroyed
