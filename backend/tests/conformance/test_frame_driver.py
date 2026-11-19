# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The FrameDriver contract, once, for every driver. Each test is one
clause of `ai/frames/driver.py`, run against a real provider: Docker in `just test`; sandbar is
listed too and skipped until a `sandbard` is configured (`PRISMOID_WORKER_SANDBAR_URL`, or
`PRISMOID_CONFORMANCE_DRIVERS=sandbar` against `just sandbar-dev`'s default address; its docker
backend, egress on; the browser clause also needs `PRISMOID_WORKER_SANDBAR_BROWSER_IMAGE`). Set,
`PRISMOID_CONFORMANCE_DRIVERS` names the drivers that run. A driver that
fails a clause here breaks the Machine where the policy tests (FakeFrame) cannot see. The
Docker-only properties (no route out, the toolchain, root's HOME) stay in
tests/integration/frames/test_frame.py.

A native driver's sleep has its own clause; which driver holds `Checkpoints` and `NativeSleep` is a
unit test."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn

from prismoid.ai.frames import ArchiveTooLarge, destroy_ref, ensure_ready, inventory, new_frame
from prismoid.ai.frames.driver import Checkpoints, FrameDriver, NativeSleep
from prismoid.ai.frames.layout import INPUTS, SKILLS, STEP_MARKER, WORK, input_path
from prismoid.ai.frames.preview import RUNNER_PATH as PREVIEW_RUNNER
from prismoid.ai.verification.pipeline import SUITE_DIR, artifact_frame_path
from prismoid.conf import settings
from prismoid.schemas.machines import LABEL_EPOCH, LABEL_KIND, LABEL_MACHINE, FrameKind
from tests.support.markers import cannot_run, darwin_only

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]

NAMED = [d.strip() for d in os.environ.get("PRISMOID_CONFORMANCE_DRIVERS", "").split(",") if d.strip()]
DRIVERS = [
    pytest.param(d, marks=darwin_only) if d == "device" else d
    for d in (NAMED or ["docker", "sandbar", "device"])
]
ALLOWED = "conformance.example"  # a sandbar sandbox born with no domains is airtight: give it one
PAGE = (
    "<!doctype html><title>Orders</title><h1>Orders</h1>"
    "<input type=password aria-label=Password value=hunter2>"
    "<button type=button onclick=\"document.title='Saved'\">Save changes</button>"
)


@pytest.fixture(params=DRIVERS)
def driver(request: pytest.FixtureRequest) -> str:
    name = str(request.param)
    # Named, sandbar is the caller's opt-in; unnamed, it runs only where its URL is configured
    # (env or .env), since the default address answers nowhere but beside `just sandbar-dev`.
    if name == "sandbar" and name not in NAMED and "sandbar_url" not in settings.worker.model_fields_set:
        pytest.skip("no sandbard: set PRISMOID_WORKER_SANDBAR_URL")
    return name


def _device_home() -> Path:
    """A short, resolved folder: a socket path must fit sun_path, and `/tmp` is `/private/tmp`."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="ph", dir="/tmp")))


@pytest.fixture(autouse=True)
async def device_helper(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[None]:
    """For the `device` driver: a real helper on a tmp home, served in-process on its Unix socket
    (the same app `prismoid device helper` serves), so the worker's driver speaks to it for real."""
    params = getattr(request.node, "callspec", None)
    if params is None or params.params.get("driver") != "device":
        yield
        return
    from prismoid.runner.device_door import create_app

    home = _device_home()
    monkeypatch.setattr(settings.local, "home", home)
    app = create_app(home, device_name=lambda: "Conformance Mac")
    server = uvicorn.Server(uvicorn.Config(app, uds=str(home / "run" / "helper.sock"), log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        await serving
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(autouse=True)
def own_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's frames wear a dev instance of their own. A worker running against the same
    providers reconciles its instance's frames every 60 s and destroys those its Postgres does not
    know - these, under the default instance: a live worker would delete them mid-test, which
    surfaces as a failed exec."""
    monkeypatch.setattr(settings.worker, "instance", f"conformance-{uuid.uuid4().hex[:8]}")


@asynccontextmanager
async def made(
    driver: str, *, profile: str = "base", epoch: int = 1, mid: uuid.UUID | None = None
) -> AsyncGenerator[FrameDriver]:
    """A Machine-labelled frame on `driver`, destroyed by reference whatever the test did to it."""
    if profile == "browser" and driver == "sandbar" and not settings.worker.sandbar_browser_image:
        pytest.skip("sandbar's browser image is not configured (just sandbar-browser-image)")
    mid = mid or uuid.uuid4()
    await asyncio.to_thread(ensure_ready, profile, driver)
    frame = new_frame(str(mid), driver=driver, profile=profile, name=f"machine-{mid}-{epoch}")
    await frame.create(
        allow_domains=[ALLOWED],
        labels={LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: str(mid), LABEL_EPOCH: str(epoch)},
    )
    try:
        yield frame
    finally:
        await destroy_ref(frame.ref, driver=driver)


def _stop(driver: str, ref: str) -> None:
    """A host or daemon restart: the frame is still there, stopped. Docker's container is the
    frame; sandbar's docker backend names its container `sandbar-<id>`."""
    from prismoid.ai.frames.docker_driver import docker_client

    docker_client().containers.get(ref if driver == "docker" else f"sandbar-{ref}").kill()


async def test_create_names_the_driver_and_the_inventory_lists_the_frame(driver: str) -> None:
    mid = uuid.uuid4()
    async with made(driver, mid=mid) as frame:
        assert frame.driver == driver and frame.sleep_kind in ("archive", "native", "none")
        listed = {f.ref: f for f in await inventory([driver])}
        assert listed[frame.ref].driver == driver
        assert listed[frame.ref].labels[LABEL_KIND] == FrameKind.MACHINE
        assert listed[frame.ref].labels[LABEL_MACHINE] == str(mid)
        assert frame.recording_dir.rsplit("/", 1)[1].startswith("recording-")
        again = new_frame(str(mid), driver=driver, name=frame.ref)
        await again.bind()
        assert (await again.exec("echo hi")).stdout.strip() == "hi"


async def test_exec_runs_in_work_as_the_agent(driver: str) -> None:
    async with made(driver) as frame:
        out = await frame.exec("pwd && id -u")
        # a device frame runs as the user, in the folder its roots really are
        uid = str(os.getuid()) if driver == "device" else "1000"
        assert out.ok and out.stdout.split() == [frame.paths.render(WORK), uid]


async def test_exec_reads_no_startup_file_the_agent_planted(driver: str) -> None:
    async with made(driver) as frame:
        hook = 'echo "$BASH_EXECUTION_STRING" >> /tmp/hooked\n'
        for startup in (".profile", ".bash_profile", ".bashrc"):
            if driver == "device":  # the file door itself refuses a file that runs code later
                with pytest.raises(PermissionError):
                    await frame.write_file(f"{WORK}/{startup}", hook)
                continue
            await frame.write_file(f"{WORK}/{startup}", hook)
        await frame.exec("true")
        assert (await frame.exec("cat /tmp/hooked 2>/dev/null; true")).stdout == ""


async def test_an_exec_past_its_timeout_exits_124(driver: str) -> None:
    async with made(driver) as frame:
        assert (await frame.exec("sleep 10", timeout=1)).exit_code == 124


async def test_text_and_binary_files_round_trip_and_list(driver: str) -> None:
    payload = bytes(range(256))
    async with made(driver) as frame:
        await frame.write_file(f"{WORK}/notes.md", "héllo\n")
        await frame.write_bytes(f"{WORK}/raw.bin", payload)
        assert await frame.read_file(f"{WORK}/notes.md") == "héllo\n"
        assert await frame.read_bytes(f"{WORK}/raw.bin") == payload
        # and in the frame itself: a write and a read that mangled alike would still agree above
        assert hashlib.sha256(payload).hexdigest() in (await frame.exec(f"sha256sum {WORK}/raw.bin")).stdout
        assert {"notes.md", "raw.bin"} <= set(await frame.list_dir(WORK))
        assert await frame.list_dir(f"{WORK}/absent") == []


async def test_a_system_file_is_read_only_to_the_agent(driver: str) -> None:
    async with made(driver) as frame:
        source = input_path("src.md")
        await frame.write_bytes(source, b"the truth", mode=0o444, owner="system")
        assert not (await frame.exec(f"echo a lie > {source}")).ok
        assert not (await frame.exec(f"rm -f {source} && test ! -e {source}")).ok
        assert await frame.read_bytes(source) == b"the truth"


async def test_reset_mounts_takes_away_the_last_steps_mounts(driver: str) -> None:
    async with made(driver) as frame:
        await frame.write_bytes(input_path("old.md"), b"turn one", mode=0o444, owner="system")
        # Docker: the agent's directory, the system's file; where /skills is a system root the
        # mkdir fails and the system write makes the parents
        await frame.exec(f"mkdir -p {SKILLS}/draft")
        await frame.write_bytes(f"{SKILLS}/draft/SKILL.md", b"a draft", mode=0o444, owner="system")
        await frame.reset_mounts()
        left = await frame.exec(f"ls -A {INPUTS} {SKILLS}")
        assert left.ok and left.stdout.split() == [
            frame.paths.render(f"{SKILLS}:"),
            frame.paths.render(f"{INPUTS}:"),
        ]


async def test_freeze_stops_the_agents_processes_and_thaw_resumes_them(driver: str) -> None:
    async with made(driver) as frame:
        await frame.exec(
            "nohup sh -c 'while true; do echo t >> /work/tick; sleep 0.1; done' >/dev/null 2>&1 &"
        )
        await asyncio.sleep(0.5)
        await frame.freeze()
        first = (await frame.exec("wc -l < /work/tick")).stdout.strip()
        await asyncio.sleep(0.6)
        assert (await frame.exec("wc -l < /work/tick")).stdout.strip() == first
        await frame.thaw()
        await asyncio.sleep(0.6)
        assert int((await frame.exec("wc -l < /work/tick")).stdout.strip()) > int(first)


async def test_work_round_trips_through_archive_and_unpack(driver: str) -> None:
    if driver == "device":
        pytest.skip("device never archives: /work is the user's own folder (sleep_kind none)")
    mid = uuid.uuid4()
    buf = io.BytesIO()
    async with made(driver, mid=mid, epoch=1) as first:
        assert isinstance(first, Checkpoints)
        await first.exec(
            "mkdir -p /work/sub /work/work && echo kept > /work/sub/a && echo inner > /work/work/x "
            "&& ln -s a /work/sub/link && ln -s /usr/bin/env /work/sub/out && ln -s ../../etc /work/sub/up"
        )
        await first.freeze()
        await first.archive(WORK, buf)
    async with made(driver, mid=mid, epoch=2) as second:
        assert isinstance(second, Checkpoints)
        buf.seek(0)
        await second.unpack(WORK, buf)
        out = await second.exec(
            "cat /work/sub/a && readlink /work/sub/link && stat -c %u /work/sub/a && cat /work/work/x"
        )
        assert out.stdout.split() == ["kept", "a", "1000", "inner"]
        # A link out of /work, absolute or climbing: Docker and sandbar carry it.
        outside = await second.exec(
            "for l in out up; do if [ -L /work/sub/$l ]; then readlink /work/sub/$l; else echo dropped; fi; done"
        )
        assert outside.stdout.split() == ["/usr/bin/env", "../../etc"]


async def test_an_archive_past_its_limit_stops(driver: str) -> None:
    if driver == "device":
        pytest.skip("device never archives: /work is the user's own folder (sleep_kind none)")
    async with made(driver) as frame:
        assert isinstance(frame, Checkpoints)
        await frame.exec("head -c 3000000 /dev/urandom > /work/big.bin")
        with pytest.raises(ArchiveTooLarge) as big:
            await frame.archive(WORK, io.BytesIO(), limit=1_000_000)
        assert big.value.seen > 1_000_000
        whole = io.BytesIO()
        await frame.archive(WORK, whole)  # no limit: the same /work arrives whole
        assert whole.tell() > 3_000_000


async def test_allow_newer_wins_and_a_stale_push_adopts_the_version_held(driver: str) -> None:
    mid = uuid.uuid4()
    async with made(driver, mid=mid) as frame:  # born at v1
        assert await frame.allow([ALLOWED, "a.example"], 7) is True
        assert await frame.allow([ALLOWED], 6) is False
        assert sorted(frame.egress_hosts) == sorted([ALLOWED, "a.example"]) and frame.egress_version == 7
        other = new_frame(str(mid), driver=driver, name=frame.ref)
        await other.bind()
        assert await other.allow([ALLOWED], 9) is True  # the provider holds v9; `frame` believes v7
        assert await frame.allow([ALLOWED, "b.example"]) is False  # v8: stale, and v9 adopted
        assert frame.egress_version == 9
        assert sorted(frame.egress_hosts) == sorted([ALLOWED, "a.example"])  # a refused push is not held
        assert await frame.allow([ALLOWED, "b.example"]) is True and frame.egress_version == 10


async def test_denials_name_a_host_the_frame_was_refused(driver: str) -> None:
    async with made(driver) as frame:
        await frame.exec("curl -s -o /dev/null --max-time 5 https://example.com || true")
        assert any(d.get("host") == "example.com" for d in await frame.denials())
        # widening registers the host and clears this frame's denial history: none names it now
        await frame.allow([ALLOWED, "example.com"])
        await frame.exec("curl -s -o /dev/null --max-time 5 https://example.com || true")
        assert [d for d in await frame.denials() if d.get("host") == "example.com"] == []


async def test_bind_starts_a_stopped_frame_with_its_files(driver: str) -> None:
    if driver == "device":
        pytest.skip("a device frame has no stopped state: it is a folder, and bind never starts anything")
    if driver == "sandbar" and settings.worker.sandbar_backend != "docker":
        pytest.skip("only sandbar's docker backend can be stopped from here")
    mid = uuid.uuid4()
    async with made(driver, mid=mid) as frame:
        await frame.write_bytes(f"{WORK}/notes.md", b"unsaved")
        await asyncio.to_thread(_stop, driver, frame.ref)
        if driver == "docker":  # a stopped container is still listed: only its processes were lost
            assert frame.ref in {f.ref for f in await inventory([driver])}
        again = new_frame(str(mid), driver=driver, name=frame.ref)
        assert await again.bind() is True
        assert await again.read_bytes(f"{WORK}/notes.md") == b"unsaved"
        assert await new_frame(str(mid), driver=driver, name=frame.ref).bind() is False  # running now


@pytest.mark.xdist_group("frames-d")  # every builder of the browser image shares this group
async def test_the_browser_opens_snapshots_and_clicks_by_reference(driver: str) -> None:
    if driver == "device" and not os.access(settings.local.chrome_path, os.X_OK):
        cannot_run(f"no Google Chrome at {settings.local.chrome_path}")
    async with made(driver, profile="browser") as frame:
        await frame.write_file("/work/site/index.html", PAGE)
        await frame.exec(
            "cd /work/site && (python3 -m http.server 8088 --bind 127.0.0.1 >/dev/null 2>&1 &) && "
            "for i in $(seq 1 40); do curl -sf http://127.0.0.1:8088/index.html >/dev/null && break; sleep 0.1; done"
        )
        opened = await frame.browser({"verb": "open", "url": "http://127.0.0.1:8088/index.html"})
        assert opened["ok"] and opened["title"] == "Orders"
        snap = await frame.browser({"verb": "snapshot"})
        assert snap["ok"] and snap["epoch"] == 1  # a new frame's sidecar counts from its first snapshot
        assert "## Orders" in snap["text"] and "hunter2" not in snap["text"]
        line = next(line for line in snap["text"].splitlines() if 'button "Save changes"' in line)
        ref = re.findall(r"\[(e\d+\.\d+)\]", line[: line.index('button "Save changes"')])[-1]
        clicked = await frame.browser({"verb": "click", "ref": ref})
        assert clicked["ok"] and clicked["title"] == "Saved"
        shot = await frame.browser({"verb": "screenshot"})
        assert shot["ok"] and base64.b64decode(shot["png_b64"]).startswith(b"\x89PNG")
        # A later step's session (a new frame object on the same frame) never repeats an epoch:
        # the daemon's counter runs on, and a remote driver's ledger reserves past the last session.
        await frame.close()
        again = new_frame("t", driver=driver, profile="browser", name=frame.ref)  # the task id only labels it
        await again.bind()
        later = await again.browser({"verb": "snapshot"})
        assert later["ok"] and later["epoch"] > snap["epoch"]
        await again.close()


async def test_destroy_is_final_and_a_second_destroy_is_no_error(driver: str) -> None:
    mid = uuid.uuid4()
    async with made(driver, mid=mid) as frame:  # `made` destroys it once more on the way out
        await frame.destroy()
        with pytest.raises(RuntimeError):
            await new_frame(str(mid), driver=driver, name=frame.ref).bind()


# Every path Prismoid writes as the system: a step's mounts, the step marker, a skill, the preview
# runner, the verify suite (one script executable) and the artifact the verify job checks, put
# where it was published (`/artifacts`, operations/verification). Each must land, root's and
# read-only to the agent, on every driver.
SYSTEM_WRITES = [
    (input_path("src.md"), 0o444),
    (STEP_MARKER, 0o444),
    (f"{SKILLS}/s/SKILL.md", 0o444),
    (PREVIEW_RUNNER, 0o444),
    (f"{SUITE_DIR}/check.py", 0o555),
    (artifact_frame_path("/work/report.md"), 0o444),
]


async def test_every_system_write_prismoid_makes_lands_read_only_to_the_agent(driver: str) -> None:
    async with made(driver) as frame:
        for path, mode in SYSTEM_WRITES:
            await frame.write_bytes(path, b"given", mode=mode, owner="system")
            assert await frame.read_bytes(path) == b"given", path
            assert not (await frame.exec(f"echo lie > {path}")).ok, path


async def test_a_native_sleep_keeps_the_frames_processes_and_files(driver: str) -> None:
    async with made(driver) as frame:
        if not isinstance(frame, NativeSleep):
            pytest.skip(f"{driver} does not sleep natively (sleep_kind {frame.sleep_kind})")
        await frame.write_bytes(f"{WORK}/notes.md", b"unsaved")
        await frame.exec(
            "nohup sh -c 'while true; do echo t >> /work/tick; sleep 0.1; done' >/dev/null 2>&1 &"
        )
        await asyncio.sleep(0.5)
        await frame.suspend()
        await frame.suspend()  # idempotent: DBOS replays a step
        await frame.resume()
        await frame.resume()
        assert await new_frame("t", driver=driver, name=frame.ref).bind() is False, "memory kept: no restart"
        before = int((await frame.exec("wc -l < /work/tick")).stdout.strip())
        await asyncio.sleep(0.6)
        assert int((await frame.exec("wc -l < /work/tick")).stdout.strip()) > before, "the loop survived"
        assert await frame.read_bytes(f"{WORK}/notes.md") == b"unsaved"


async def test_a_released_frame_runs_on(driver: str) -> None:
    """`close` is not a lifecycle verb: the frame a step let go of is the next step's, unchanged."""
    mid = uuid.uuid4()
    async with made(driver, mid=mid) as frame:
        await frame.write_bytes(f"{WORK}/notes.md", b"kept")
        await frame.close()
        again = new_frame(str(mid), driver=driver, name=frame.ref)
        restarted = await again.bind()
        if driver != "device":  # a device frame always answers True: bound, push the list
            assert restarted is False
        assert await again.read_bytes(f"{WORK}/notes.md") == b"kept"
