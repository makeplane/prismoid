# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/ai/frames/image_browser/test_browser_sidecar_daemon.py
"""The daemon: socket protocol, recording, stale references, restart on a dead browser."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.fakes.cdp_scripts import FakeCdp
from tests.support.paths import BACKEND

SIDECAR = BACKEND / "prismoid" / "ai" / "frames" / "image_browser"
sys.path.insert(0, str(SIDECAR))

from sidecar.browser import Browser  # noqa: E402
from sidecar.daemon import Daemon  # noqa: E402
from sidecar.verbs import StaleReference  # noqa: E402


class FakeBrowser(Browser):
    """No Chromium. Records what it was asked and answers like the real one would."""

    def __init__(self) -> None:
        super().__init__(profile_dir="/nonexistent", proxy=None)
        self.calls: list[dict[str, Any]] = []
        self.alive = True
        self.epoch = 1
        self.refs: dict[str, int] = {}
        self.record_fails = False  # simulates Browser.handle() swallowing a recording-capture error

    async def start(self) -> None:
        self.alive = True
        self.refs = {}  # a (re)start invalidates every reference from a previous life

    async def stop(self) -> None:
        self.alive = False

    async def handle(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(cmd)
        if not self.alive:
            raise ConnectionError("pipe closed")
        verb = cmd["verb"]
        if verb == "snapshot":
            return {
                "ok": True,
                "epoch": self.epoch,
                "text": '[e1.1] button "Go"',
                "_targets": {"e1.1": ("button", "Go")},
                "url": "http://x/",
                "title": "X",
            }
        if verb == "click":
            if cmd["ref"] not in self.refs:
                raise StaleReference(cmd["ref"])
            record = (
                {"capture": "failed"}
                if self.record_fails
                else {"target": 'button "Go"', "jpeg": b"\xff\xd8fake"}
            )
            # The look's names travel as facts beside its text, which never reaches the recording's names.
            look = {"epoch": 2, "text": '[e2.1] link "Back"', "_targets": {"e2.1": ("link", "Next")}}
            page = look if cmd.get("snapshot") else {}
            return {"ok": True, "url": "http://x/2", "title": "X2", "_record": record, **page}
        if verb == "die":
            self.alive = False
            raise ConnectionError("pipe closed")
        if verb == "screenshot":
            return {
                "ok": True,
                "png_b64": base64.b64encode(b"\x89PNGfake").decode(),
                "url": "http://x/",
                "title": "X",
            }
        return {"ok": True}


async def _send(sock_path: str, cmd: dict[str, Any]) -> dict[str, Any]:
    r, w = await asyncio.open_unix_connection(sock_path)
    w.write(json.dumps(cmd).encode() + b"\n")
    await w.drain()
    line = await r.readline()
    w.close()
    return json.loads(line)


async def _send_raw(sock_path: str, raw: bytes) -> dict[str, Any]:
    r, w = await asyncio.open_unix_connection(sock_path)
    w.write(raw)
    await w.drain()
    line = await r.readline()
    w.close()
    return json.loads(line)


async def _connect_and_send(sock_path: str, cmd: dict[str, Any], attempts: int = 200) -> dict[str, Any]:
    """Retry-connect: the socket file does not exist until the daemon has finished starting the
    browser, so this cannot itself race dispatch()'s own start() call - that is the point."""
    for _ in range(attempts):
        try:
            return await _send(sock_path, cmd)
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.02)
    raise TimeoutError("socket never appeared")


DaemonFixture = tuple[str, FakeBrowser, Path, Daemon]


@pytest.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[DaemonFixture]:
    # AF_UNIX paths are capped at ~104 bytes on macOS (sockaddr_un). pytest's tmp_path lives
    # under a long per-test directory that overflows that limit there, so the socket (only the
    # socket - recording_dir is unaffected) gets its own short-named directory under /tmp.
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    fake = FakeBrowser()
    d = Daemon(browser=fake, recording_dir=str(tmp_path / "rec"))
    sock = str(Path(sock_dir) / "s.sock")
    task = asyncio.create_task(d.serve(sock, idle_s=60.0))
    for _ in range(50):
        if Path(sock).exists():  # noqa: ASYNC240 - test polling, no trio/anyio in this repo
            break
        await asyncio.sleep(0.02)
    yield sock, fake, tmp_path / "rec", d
    task.cancel()
    shutil.rmtree(sock_dir, ignore_errors=True)


async def test_ping_reports_the_browser_and_snapshot_round_trips(daemon: DaemonFixture) -> None:
    sock, _fake, _, _ = daemon
    assert (await _send(sock, {"verb": "ping"}))["chromium"] == "running"
    snap = await _send(sock, {"verb": "snapshot"})
    assert snap["ok"] and snap["epoch"] == 1 and "[e1.1]" in snap["text"]


async def test_an_acting_verb_records_a_frame_and_an_action_row(daemon: DaemonFixture) -> None:
    sock, fake, rec, _ = daemon
    # start() clears refs on every (re)start, including the daemon's own startup - seed it fresh.
    fake.refs = {"e1.1": 11}
    res = await _send(sock, {"verb": "click", "ref": "e1.1"})
    assert res["ok"] and "_record" not in res  # the record never leaves the daemon
    assert (rec / "0001-click.jpg").read_bytes().startswith(b"\xff\xd8")
    rows = json.loads((rec / "actions.json").read_text())
    assert rows[0]["verb"] == "click" and rows[0]["target"] == 'button "Go"' and rows[0]["host"] == "x"
    assert "text" not in rows[0]


async def test_an_action_that_returns_the_page_is_recorded_against_the_page_it_acted_on(
    daemon: DaemonFixture,
) -> None:
    """The recording names the target from the tree the model acted on; the tree the action
    returns is what the NEXT action's target is named from."""
    sock, fake, rec, d = daemon
    fake.refs = {"e1.1": 11}
    await _send(sock, {"verb": "snapshot"})
    res = await _send(sock, {"verb": "click", "ref": "e1.1", "snapshot": True})
    assert res["ok"] and res["text"] == '[e2.1] link "Back"' and "_targets" not in res
    rows = json.loads((rec / "actions.json").read_text())
    assert rows[0]["target"] == 'button "Go"'
    assert d.recorder.targets == {"e2.1": 'link "Next"'}


async def test_a_failed_recording_capture_never_fails_the_action(daemon: DaemonFixture) -> None:
    sock, fake, rec, _ = daemon
    fake.refs = {"e1.1": 11}
    fake.record_fails = True  # e.g. a detached frame right after a click-triggered navigation
    res = await _send(sock, {"verb": "click", "ref": "e1.1"})
    assert res["ok"] and "_record" not in res
    assert not (rec / "0001-click.jpg").exists()
    # The ACTION happened (the journal counts it ok) - a placeholder row keeps the recording's
    # count truthful; only the picture is missing, and the row says so.
    rows = json.loads((rec / "actions.json").read_text())
    assert len(rows) == 1
    assert rows[0]["verb"] == "click" and rows[0]["file"] == "" and rows[0]["capture"] == "failed"
    done = await _send(sock, {"verb": "finalize"})
    assert done["ok"] and done["actions"] == 1 and done["acting"] == 1
    assert "(capture failed)" in (rec / "index.html").read_text()


async def _idle_daemon(tmp_path: Path) -> tuple[str, FakeBrowser, asyncio.Task[None], str]:
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    fake = FakeBrowser()
    d = Daemon(browser=fake, recording_dir=str(tmp_path / "rec"))
    sock = str(Path(sock_dir) / "s.sock")
    task = asyncio.create_task(d.serve(sock, idle_s=0.05))
    for _ in range(50):
        if Path(sock).exists():  # noqa: ASYNC240 - test polling, no trio/anyio in this repo
            break
        await asyncio.sleep(0.02)
    return sock, fake, task, sock_dir


async def test_a_held_daemon_keeps_chromium_past_the_idle_deadline(tmp_path: Path) -> None:
    """IMPORTANT (whole-branch review): a parked gate lives up to 24h, the idle stop is 10 min.
    `hold` (sent by the engine right before a browser step parks) suspends the idle stop; the
    next real verb (the resume's) releases it, and idle stop works again."""
    sock, fake, task, sock_dir = await _idle_daemon(tmp_path)
    try:
        assert (await _send(sock, {"verb": "hold"})) == {"ok": True, "held": True}
        await asyncio.sleep(0.3)  # six idle deadlines
        assert fake.alive, "a held daemon must not stop Chromium"
        pinged = await _send(sock, {"verb": "ping"})
        assert pinged["chromium"] == "running" and pinged["held"] is True  # ping never releases
        await asyncio.sleep(0.2)
        assert fake.alive
        assert (await _send(sock, {"verb": "snapshot"}))["ok"]  # a real verb releases the hold
        await asyncio.sleep(0.3)
        assert not fake.alive, "released, the idle stop applies again"
        assert not (tmp_path / "rec" / "actions.json").read_text().strip("[]")  # hold is never recorded
    finally:
        task.cancel()
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_release_ends_a_hold_explicitly(tmp_path: Path) -> None:
    sock, fake, task, sock_dir = await _idle_daemon(tmp_path)
    try:
        await _send(sock, {"verb": "hold"})
        assert (await _send(sock, {"verb": "release"})) == {"ok": True, "held": False}
        await asyncio.sleep(0.3)
        assert not fake.alive
    finally:
        task.cancel()
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_record_moves_a_running_daemon_to_the_next_steps_folder(daemon: DaemonFixture) -> None:
    """A warm Machine keeps its sidecar across steps, but every step (and every retry) records into
    its own folder: `record` moves the running daemon there - the warm page kept, the count started
    again - so the next step's `finalize` answers with ITS folder, and the first's trail is left as
    it was."""
    sock, fake, first, _ = daemon
    fake.refs = {"e1.1": 11}
    await _send(sock, {"verb": "click", "ref": "e1.1"})
    second = first.parent / "rec-2"
    assert (await _send(sock, {"verb": "record", "dir": str(second)})) == {"ok": True}
    await _send(sock, {"verb": "click", "ref": "e1.1"})
    done = await _send(sock, {"verb": "finalize"})
    assert done == {"ok": True, "actions": 1, "acting": 1, "path": str(second)}
    assert (second / "0001-click.jpg").exists()
    assert len(json.loads((first / "actions.json").read_text())) == 1
    assert fake.alive and fake.refs == {"e1.1": 11}, "the warm page survives the step boundary"


async def test_a_refused_folder_records_nothing_and_says_so(daemon: DaemonFixture) -> None:
    """The agent can plant the next step's folder name as a symlink: the move refuses it, says so,
    and never falls back to writing the last step's folder."""
    sock, fake, first, _ = daemon
    elsewhere = first.parent / "elsewhere"
    elsewhere.mkdir()
    (first.parent / "rec-2").symlink_to(elsewhere)
    moved = await _send(sock, {"verb": "record", "dir": str(first.parent / "rec-2")})
    assert moved == {"ok": False, "error": "recording folder unavailable"}
    fake.refs = {"e1.1": 11}
    assert (await _send(sock, {"verb": "click", "ref": "e1.1"}))["ok"]
    assert list(elsewhere.iterdir()) == [] and not (first / "0001-click.jpg").exists()


async def test_an_act_still_running_at_a_boundary_never_lands_in_the_next_steps_trail(
    daemon: DaemonFixture,
) -> None:
    """A browser call its driver gave up on can finish after the next step's boundary moved the
    recording: it was the last step's act, and the new step's trail starts clean."""
    _sock, fake, first, d = daemon
    fake.refs = {"e1.1": 11}
    released = asyncio.Event()
    handle = fake.handle

    async def slow(cmd: dict[str, Any]) -> dict[str, Any]:
        await released.wait()
        return await handle(cmd)

    fake.handle = slow
    late = asyncio.create_task(d.dispatch({"verb": "click", "ref": "e1.1"}))
    await asyncio.sleep(0)
    second = first.parent / "rec-2"
    assert (await d.dispatch({"verb": "record", "dir": str(second)})) == {"ok": True}
    released.set()
    assert (await late)["ok"]
    assert json.loads((second / "actions.json").read_text()) == []
    assert not (second / "0001-click.jpg").exists()


async def test_a_stale_reference_is_refused_with_a_reason(daemon: DaemonFixture) -> None:
    sock, *_ = daemon
    res = await _send(sock, {"verb": "click", "ref": "e0.7"})
    assert res == {"ok": False, "error": "stale reference e0.7; take a new snapshot"}


async def test_a_dead_browser_answers_restarted_then_recovers(daemon: DaemonFixture) -> None:
    sock, fake, _, _ = daemon
    assert (await _send(sock, {"verb": "die"})) == {"ok": False, "error": "browser restarted"}
    assert fake.alive  # the daemon started it again
    assert (await _send(sock, {"verb": "snapshot"}))["ok"]


async def test_unknown_verb_is_a_result_not_a_crash(daemon: DaemonFixture) -> None:
    sock, *_ = daemon
    res = await _send(sock, {"verb": "evaluate", "js": "1+1"})
    assert res["ok"] is False and "unknown verb" in res["error"]


async def test_a_screenshot_is_recorded_at_full_size_and_still_carries_png_b64(
    daemon: DaemonFixture,
) -> None:
    sock, _fake, rec, _ = daemon
    res = await _send(sock, {"verb": "screenshot"})
    assert res["ok"] and res["png_b64"] == base64.b64encode(b"\x89PNGfake").decode()
    assert (rec / "0001-screenshot.png").read_bytes().startswith(b"\x89PNG")
    rows = json.loads((rec / "actions.json").read_text())
    assert rows[0]["verb"] == "screenshot" and rows[0]["host"] == "x"


async def test_serve_starts_chromium_exactly_once_even_under_a_connecting_client(
    tmp_path: Path,
) -> None:
    """Regression for the double-start race: the old serve() opened the socket before the browser
    had finished starting, so the first client's dispatch() saw `alive == False` and started it a
    second time. Now the socket does not exist until start() has already returned once."""

    class SlowStartBrowser(FakeBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.start_calls = 0

        async def start(self) -> None:
            self.start_calls += 1
            await asyncio.sleep(0.3)
            self.alive = True

    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    try:
        fake = SlowStartBrowser()
        d = Daemon(browser=fake, recording_dir=str(tmp_path / "rec"))
        sock = str(Path(sock_dir) / "s.sock")
        task = asyncio.create_task(d.serve(sock, idle_s=60.0))
        try:
            res = await _connect_and_send(sock, {"verb": "ping"})
            assert res == {"ok": True, "chromium": "running", "held": False}
            assert fake.start_calls == 1
        finally:
            task.cancel()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_refs_are_cleared_on_restart_so_old_elements_go_stale(daemon: DaemonFixture) -> None:
    sock, fake, _, _ = daemon
    fake.refs = {"e1.1": 11}
    assert (await _send(sock, {"verb": "click", "ref": "e1.1"}))["ok"]
    assert (await _send(sock, {"verb": "die"})) == {"ok": False, "error": "browser restarted"}
    assert fake.alive  # the daemon started it again, which clears refs
    res = await _send(sock, {"verb": "click", "ref": "e1.1"})
    assert res == {"ok": False, "error": "stale reference e1.1; take a new snapshot"}


async def test_a_missing_ref_is_an_ok_false_result_not_a_crash(daemon: DaemonFixture) -> None:
    sock, *_ = daemon
    res = await _send(sock, {"verb": "click"})
    assert res["ok"] is False


async def test_a_non_dict_json_line_is_a_bad_request(daemon: DaemonFixture) -> None:
    sock, *_ = daemon
    res = await _send_raw(sock, b"[1,2]\n")
    assert res == {"ok": False, "error": "bad request"}


async def test_open_refuses_non_http_schemes_and_a_missing_url() -> None:
    b = Browser(profile_dir="/nonexistent", proxy=None)
    assert await b.handle({"verb": "open", "url": "file:///etc/passwd"}) == {
        "ok": False,
        "error": "refused scheme",
    }
    assert await b.handle({"verb": "open"}) == {"ok": False, "error": "missing url"}


# ── recording directory trust (a pre-created symlink or a foreign-owned directory) ─────────────


async def test_a_symlinked_recording_dir_disables_recording_but_still_serves(tmp_path: Path) -> None:
    """The agent uid (1000) can write under /artifacts before this daemon (uid 1001) ever runs -
    including pre-creating `recording-N` as a symlink. The daemon must refuse to follow it,
    never crash on startup, and keep answering every command with recording simply disabled."""
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    rec_path = tmp_path / "rec"
    rec_path.symlink_to(real_dir)
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    try:
        fake = FakeBrowser()
        d = Daemon(browser=fake, recording_dir=str(rec_path))
        sock = str(Path(sock_dir) / "s.sock")
        task = asyncio.create_task(d.serve(sock, idle_s=60.0))
        try:
            res = await _connect_and_send(sock, {"verb": "ping"})
            assert res == {"ok": True, "chromium": "running", "held": False}
            assert not d.recorder.recording
            fake.refs = {"e1.1": 11}
            click = await _connect_and_send(sock, {"verb": "click", "ref": "e1.1"})
            assert click["ok"] is True
            assert list(real_dir.iterdir()) == []  # nothing ever written through the symlink
        finally:
            task.cancel()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_finalize_writes_a_self_contained_index_and_opens_the_folder_for_harvest(
    daemon: DaemonFixture,
) -> None:
    _sock, _fake, rec, d = daemon
    await d.recorder.row("click", 'button "Save"', b"\xff\xd8\xff", "jpg", "http://127.0.0.1:8088/index.html")
    result = await d.dispatch({"verb": "finalize"})
    assert result["ok"] and result["actions"] == 1
    index = (rec / "index.html").read_text()
    assert "data:image/jpeg;base64," in index and 'button "Save"' in index and "127.0.0.1/index.html" in index
    assert oct(rec.stat().st_mode & 0o777) == "0o755"
    assert oct((rec / "actions.json").stat().st_mode & 0o777) == "0o644"


async def test_finalize_with_nothing_recorded_still_writes_an_index(daemon: DaemonFixture) -> None:
    _sock, _fake, rec, d = daemon
    result = await d.dispatch({"verb": "finalize"})
    assert result == {"ok": True, "actions": 0, "acting": 0, "path": str(rec)}
    assert "no actions were recorded" in (rec / "index.html").read_text()


async def test_a_recording_dir_owned_by_another_uid_disables_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing directory owned by another uid is hard to simulate without root, so the
    ownership branch is exercised by monkeypatching `os.getuid` to disagree with the directory's
    real (test-runner) owner - the same mismatch a foreign-owned directory would produce."""
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    rec_dir = tmp_path / "rec"
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="sc-")
    try:
        fake = FakeBrowser()
        d = Daemon(browser=fake, recording_dir=str(rec_dir))
        sock = str(Path(sock_dir) / "s.sock")
        task = asyncio.create_task(d.serve(sock, idle_s=60.0))
        try:
            res = await _connect_and_send(sock, {"verb": "ping"})
            assert res == {"ok": True, "chromium": "running", "held": False}
            assert not d.recorder.recording
            fake.refs = {"e1.1": 11}
            click = await _connect_and_send(sock, {"verb": "click", "ref": "e1.1"})
            assert click["ok"] is True
            assert not (rec_dir / "actions.json").exists()
        finally:
            task.cancel()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_a_verb_after_the_page_opened_a_tab_acts_on_that_tab(tmp_path: Path) -> None:
    """Found live: after a `target=_blank` link, every verb kept acting on the page left behind.
    The daemon moves the page to the new tab, under the verb lock, before the next verb runs."""
    cdp = FakeCdp()
    b = Browser(profile_dir="/nonexistent", proxy=None)
    b.connect(cdp)
    b.verbs._session, b.verbs._target, b.verbs._tabs = "S1", "T1", {"T1": "S1"}  # pyright: ignore[reportPrivateUsage]
    b.verbs._main_frame_id, b.alive = "F", True  # pyright: ignore[reportPrivateUsage]
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F2"}}})
    cdp.answer("Accessibility.getFullAXTree", {"nodes": []})
    d = Daemon(browser=b, recording_dir=str(tmp_path / "rec"))
    info = {"targetId": "T2", "type": "page", "openerId": "T1"}
    cdp.emit("Target.attachedToTarget", {"sessionId": "S2", "targetInfo": info, "waitingForDebugger": True})
    await cdp.drain()
    result = await d.dispatch({"verb": "snapshot"})
    assert result["ok"] is True
    assert cdp.sent_on("Accessibility.getFullAXTree") == ["S2"]


async def test_the_watch_never_moves_a_browser_that_is_not_running(tmp_path: Path) -> None:
    cdp = FakeCdp()
    b = Browser(profile_dir="/nonexistent", proxy=None)
    b.connect(cdp)
    b.verbs._session, b.verbs._target, b.verbs._tabs, b.verbs._order = "S1", "T1", {"T1": "S1"}, ["T1"]  # pyright: ignore[reportPrivateUsage]
    b.verbs._main_frame_id = "F"  # pyright: ignore[reportPrivateUsage]
    info = {"targetId": "T2", "type": "page", "openerId": "T1"}
    cdp.emit("Target.attachedToTarget", {"sessionId": "S2", "targetInfo": info, "waitingForDebugger": True})
    await cdp.drain()
    cdp.answer("Page.getFrameTree", {"frameTree": {"frame": {"id": "F2"}}})  # a move would succeed
    b.alive = False  # Chromium died between verbs; its tabs went with it
    d = Daemon(browser=b, recording_dir=str(tmp_path / "rec"))
    await d.follow_tab()
    assert "S2" not in cdp.sent_on("Page.enable"), "no move was even begun"


@pytest.mark.parametrize("approved_by", ["startup", "record"])
async def test_a_recording_dir_swapped_for_a_symlink_after_approval_is_never_written_through(
    tmp_path: Path, approved_by: str
) -> None:
    """The agent OWNS /artifacts, so its sticky bit does not stop it renaming the approved
    `recording-N` away at any moment and planting a symlink at that name. Every write, chmod and
    listing after approval must land in the directory the daemon checked, never follow the name:
    otherwise the daemon (uid 1001) writes into, and opens up, any browser-owned directory. A
    folder a warm Machine's next step switches in with `record` is held the same way, and the
    folder it leaves is closed, not leaked."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    (victim / "Cookies").write_text("token")
    (victim / "Cookies").chmod(0o600)
    d = Daemon(browser=FakeBrowser(), recording_dir=str(artifacts / "recording-1"))
    await d.recorder.open()
    approved = artifacts / "recording-1"
    if approved_by == "record":
        open_fds = len(os.listdir("/dev/fd"))
        approved = artifacts / "recording-2"
        assert await d.dispatch({"verb": "record", "dir": str(approved)}) == {"ok": True}
        assert len(os.listdir("/dev/fd")) == open_fds
    approved.rename(artifacts / "moved")
    approved.symlink_to(victim)
    await d.recorder.row("click", 'button "x"', b"\xff\xd8jpeg", "jpg", "https://a.example/b")
    assert (await d.dispatch({"verb": "finalize"}))["ok"]
    assert [p.name for p in victim.iterdir()] == ["Cookies"]
    assert stat.S_IMODE(victim.stat().st_mode) == 0o700
    assert stat.S_IMODE((victim / "Cookies").stat().st_mode) == 0o600
    assert sorted(p.name for p in (artifacts / "moved").iterdir()) == [
        "0001-click.jpg",
        "actions.json",
        "index.html",
    ]
