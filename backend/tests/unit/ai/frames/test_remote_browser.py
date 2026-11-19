# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`RemoteBrowser` (a remote driver's): the sidecar's command protocol
answered in the worker, over a real `WsCdp` to a scripted Chromium (`WsPeer` around `FakeCdp`).
What `Daemon.dispatch` does for the sidecar it does here (one lock, the page followed, `_record`
popped, every error a result - D7); what differs is that a dropped connection reconnects at the
next verb, and a person taking the screen is "a person is driving"."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from prismoid.ai.frames.cdp_ws import GATE_CLOSE, WsCdp
from prismoid.ai.frames.frame_sink import FrameSink
from prismoid.ai.frames.image_browser.sidecar.recorder import Recorder
from prismoid.ai.frames.image_browser.sidecar.verbs import BrowserVerbs
from prismoid.ai.frames.remote_browser import DRIVING, DROPPED, UNREACHABLE, RemoteBrowser
from tests.support.fakes.cdp_peer import WsPeer
from tests.support.fakes.cdp_scripts import FakeCdp, ref_for, script_page
from tests.support.fakes.frame_files import MemoryFiles

FOLDER = "/task/recordings/recording-s1-1"


@dataclass
class Rig:
    fake: FakeCdp
    files: MemoryFiles
    browser: RemoteBrowser
    peers: list[WsPeer] = field(default_factory=list)
    refuse: list[bool] = field(default_factory=lambda: [False])  # [0]: the next dials are refused

    @property
    def dials(self) -> int:
        return len(self.peers)

    def rows(self) -> list[dict[str, Any]]:
        raw = self.files.files.get(f"{FOLDER}/actions.json")
        return json.loads(raw) if raw else []


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    fake = FakeCdp()
    script_page(fake)
    files = MemoryFiles(yields=3)  # a frame's file door yields, as a network one does
    peers: list[WsPeer] = []
    refuse = [False]

    async def answer(method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        return await fake.call(method, params, session_id=session)

    async def connect() -> WsCdp:
        if refuse[0]:
            raise ConnectionError("the relay did not answer")
        peer = WsPeer(answer)
        peers.append(peer)
        return await peer.connect()

    browser = RemoteBrowser(connect, Recorder(FrameSink(files), FOLDER))
    rig = Rig(fake, files, browser, peers, refuse)
    try:
        yield rig
    finally:
        await browser.close()
        for peer in peers:
            await peer.aclose()


async def test_a_verb_opens_the_session_once_and_files_its_act(rig: Rig) -> None:
    snap = await rig.browser.handle({"verb": "snapshot"})
    clicked = await rig.browser.handle({"verb": "click", "ref": ref_for(snap, 'button "Save changes"')})
    assert snap["ok"] and clicked["ok"] and "_record" not in clicked, "the capture never leaves"
    assert rig.dials == 1 and rig.browser.connected
    assert [row["verb"] for row in rig.rows()] == ["click"]
    assert f"{FOLDER}/0001-click.jpg" in rig.files.files


async def test_the_daemons_control_commands_never_dial_the_browser(rig: Rig) -> None:
    assert await rig.browser.handle({"verb": "ping"}) == {"ok": True, "chromium": "stopped", "held": False}
    assert await rig.browser.handle({"verb": "hold"}) == {"ok": True, "held": True}
    assert await rig.browser.handle({"verb": "release"}) == {"ok": True, "held": False}
    assert await rig.browser.handle({"verb": "record"}) == {"ok": True}
    assert await rig.browser.handle({"verb": "shutdown"}) == {"ok": True}
    done = await rig.browser.handle({"verb": "finalize"})
    assert done["ok"] and done["actions"] == 0 and done["path"] == FOLDER
    assert await rig.browser.handle({"verb": "screen"}) == {"ok": False, "error": "unknown verb 'screen'"}
    assert rig.dials == 0


async def test_a_record_names_the_next_steps_folder(rig: Rig) -> None:
    assert await rig.browser.handle({"verb": "record", "dir": "/task/recordings/recording-s2-1"}) == {
        "ok": True
    }
    snap = await rig.browser.handle({"verb": "snapshot"})
    await rig.browser.handle({"verb": "click", "ref": ref_for(snap, 'button "Save changes"')})
    assert "/task/recordings/recording-s2-1/0001-click.jpg" in rig.files.files
    assert await rig.browser.handle({"verb": "record", "dir": "/task/../etc"}) == {
        "ok": False,
        "error": "recording folder unavailable",
    }


async def test_a_dropped_connection_is_a_result_and_the_next_verb_reconnects_counting_on(rig: Rig) -> None:
    first = await rig.browser.handle({"verb": "snapshot"})
    assert first["epoch"] == 1
    await rig.peers[0].drop(1011, "the relay restarted")
    await asyncio.sleep(0.05)
    assert await rig.browser.handle({"verb": "snapshot"}) == {"ok": False, "error": DROPPED}
    again = await rig.browser.handle({"verb": "snapshot"})
    assert again["ok"] and again["epoch"] == 2, "a reconnect continues the epochs, as a restart does"
    assert rig.dials == 2


async def test_a_person_taking_the_screen_is_driving_not_a_failure(rig: Rig) -> None:
    """A remote CDP gate first refuses commands `screen_busy`, then closes the
    socket 1013: both say a person is driving, and the act that never happened files no row."""
    snap = await rig.browser.handle({"verb": "snapshot"})
    ref = ref_for(snap, 'button "Save changes"')

    def busy(session: str | None, params: Any) -> None:
        from sidecar.cdp import CdpError  # FakeCdp's own class: only its code and message cross the wire

        raise CdpError(-32000, "screen_busy: a human holds the screen")

    rig.fake.hooks["Input.dispatchMouseEvent"] = busy
    assert await rig.browser.handle({"verb": "click", "ref": ref}) == {"ok": False, "error": DRIVING}
    assert rig.rows() == []
    del rig.fake.hooks["Input.dispatchMouseEvent"]
    await rig.peers[0].drop(GATE_CLOSE, "a person took over the screen")
    await asyncio.sleep(0.05)
    assert await rig.browser.handle({"verb": "click", "ref": ref}) == {"ok": False, "error": DRIVING}
    assert rig.rows() == [] and not rig.browser.connected

    # Still held: the redial's own commands are refused, and that is still "driving", never lost.
    rig.fake.hooks["Target.getTargets"] = busy
    assert await rig.browser.handle({"verb": "snapshot"}) == {"ok": False, "error": DRIVING}
    rig.refuse[0] = True
    assert await rig.browser.handle({"verb": "snapshot"}) == {"ok": False, "error": DRIVING}
    # The person lets go: the next verb reaches the page and the flag clears.
    rig.refuse[0] = False
    del rig.fake.hooks["Target.getTargets"]
    assert (await rig.browser.handle({"verb": "snapshot"}))["ok"]
    rig.refuse[0] = True
    await rig.browser.drop()
    assert await rig.browser.handle({"verb": "snapshot"}) == {"ok": False, "error": UNREACHABLE}


async def test_an_exception_a_verb_meets_is_a_result_and_the_session_stays(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7: `BrowserVerbs.handle` lets a non-CDP exception through for its owner to turn into a
    result, as `Daemon.dispatch` does."""

    async def missing_field(self: BrowserVerbs, cmd: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("entries")

    monkeypatch.setattr(BrowserVerbs, "_v_wait", missing_field)
    assert await rig.browser.handle({"verb": "wait", "ms": 10}) == {"ok": False, "error": "KeyError"}
    assert (await rig.browser.handle({"verb": "snapshot"}))["ok"]
    assert rig.dials == 1


async def test_a_session_that_cannot_open_is_unreachable_and_the_next_verb_tries_again() -> None:
    tries = 0

    async def refuse() -> WsCdp:
        nonlocal tries
        tries += 1
        raise ConnectionError("the relay did not answer")

    browser = RemoteBrowser(refuse, Recorder(FrameSink(MemoryFiles()), FOLDER))
    assert await browser.handle({"verb": "snapshot"}) == {"ok": False, "error": UNREACHABLE}
    assert await browser.handle({"verb": "snapshot"}) == {"ok": False, "error": UNREACHABLE}
    assert tries == 2


async def test_verbs_and_recording_writes_never_interleave(rig: Rig) -> None:
    """The no-yield guard-then-write invariant `Recorder.row` relies on holds for a network sink
    only because one lock serialises every verb and every recording write."""
    snap = await rig.browser.handle({"verb": "snapshot"})
    ref = ref_for(snap, 'button "Save changes"')
    results = await asyncio.gather(*(rig.browser.handle({"verb": "click", "ref": ref}) for _ in range(3)))
    assert all(r["ok"] for r in results)
    assert [row["seq"] for row in rig.rows()] == [1, 2, 3]
    assert {f"{FOLDER}/000{n}-click.jpg" for n in (1, 2, 3)} <= set(rig.files.files)


async def test_a_target_that_says_screen_busy_is_not_the_gate(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's refusal leads the error; a wait whose model-authored target merely contains the
    words is a plain timeout."""

    async def timed_out(self: BrowserVerbs, cmd: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "timed out waiting for 'screen_busy'"}

    monkeypatch.setattr(BrowserVerbs, "_v_wait", timed_out)
    assert await rig.browser.handle({"verb": "wait", "text": "screen_busy"}) == {
        "ok": False,
        "error": "timed out waiting for 'screen_busy'",
    }


class FlakyRead(MemoryFiles):
    """A file door whose first read of the trail raises, as a remote driver's hiccup does."""

    def __init__(self) -> None:
        super().__init__(yields=1)
        self.hiccups = 1

    async def read_bytes(self, path: str) -> bytes:
        if path.endswith("actions.json") and self.hiccups:
            self.hiccups -= 1
            raise ConnectionError("the file door hiccuped")
        return await super().read_bytes(path)


async def test_a_file_door_hiccup_opening_the_recording_never_fails_the_verb() -> None:
    fake = FakeCdp()
    script_page(fake)
    files = FlakyRead()
    old = json.dumps([{"seq": 7, "verb": "type", "file": "0007-type.jpg"}]).encode()
    files.files[f"{FOLDER}/actions.json"] = old
    peer = WsPeer(lambda m, p, s: fake.call(m, p, session_id=s))

    async def connect() -> WsCdp:
        return await peer.connect()

    browser = RemoteBrowser(connect, Recorder(FrameSink(files), FOLDER))
    try:
        snap = await browser.handle({"verb": "snapshot"})
        assert snap["ok"]
        assert files.files[f"{FOLDER}/actions.json"] == old and not files.writes, (
            "nothing recorded, trail kept"
        )
        clicked = await browser.handle({"verb": "click", "ref": ref_for(snap, 'button "Save changes"')})
        assert clicked["ok"]
        rows = json.loads(files.files[f"{FOLDER}/actions.json"])
        assert [r["seq"] for r in rows] == [7, 8], "the retried open picked the trail up"
    finally:
        await browser.close()
        await peer.aclose()


class FlakyOpen(MemoryFiles):
    """A file door whose read of one folder's trail raises once (the `record` open's read)."""

    def __init__(self, folder: str) -> None:
        super().__init__(yields=1)
        self.folder = folder
        self.hiccups = 1

    async def read_bytes(self, path: str) -> bytes:
        if path == f"{self.folder}/actions.json" and self.hiccups:
            self.hiccups -= 1
            raise ConnectionError("the file door hiccuped")
        return await super().read_bytes(path)


async def test_a_record_that_hiccups_never_overwrites_the_trail_it_named() -> None:
    folder_b = "/task/recordings/recording-s2-1"
    fake = FakeCdp()
    script_page(fake)
    files = FlakyOpen(folder_b)
    old = json.dumps([{"seq": 1, "verb": "type", "file": "0001-type.jpg"}]).encode()
    files.files[f"{folder_b}/actions.json"] = old
    files.files[f"{folder_b}/0001-type.jpg"] = b"first"
    peer = WsPeer(lambda m, p, s: fake.call(m, p, session_id=s))

    async def connect() -> WsCdp:
        return await peer.connect()

    browser = RemoteBrowser(connect, Recorder(FrameSink(files), FOLDER))
    try:
        assert await browser.handle({"verb": "record"}) == {"ok": True}  # the step was recording already
        assert (await browser.handle({"verb": "record", "dir": folder_b}))["ok"] is False
        snap = await browser.handle({"verb": "snapshot"})
        assert snap["ok"] and files.files[f"{folder_b}/actions.json"] == old
        clicked = await browser.handle({"verb": "click", "ref": ref_for(snap, 'button "Save changes"')})
        assert clicked["ok"]
        rows = json.loads(files.files[f"{folder_b}/actions.json"])
        assert [r["seq"] for r in rows] == [1, 2], "the retried open kept the earlier row"
        assert files.files[f"{folder_b}/0001-type.jpg"] == b"first"
        assert f"{folder_b}/0002-click.jpg" in files.files
    finally:
        await browser.close()
        await peer.aclose()


async def test_a_person_taking_the_screen_while_the_session_opens_is_driving() -> None:
    fake = FakeCdp()
    script_page(fake)
    holder: list[WsPeer] = []

    async def answer(method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        if method == "Target.getTargets":
            await holder[0].drop(GATE_CLOSE, "a person took over the screen")
        return await fake.call(method, params, session_id=session)

    peer = WsPeer(answer)
    holder.append(peer)

    async def connect() -> WsCdp:
        return await peer.connect()

    browser = RemoteBrowser(connect, Recorder(FrameSink(MemoryFiles()), FOLDER))
    try:
        assert await browser.handle({"verb": "snapshot"}) == {"ok": False, "error": DRIVING}
    finally:
        await browser.close()
        await peer.aclose()


async def test_a_verb_that_times_out_drops_the_session_and_the_next_verb_redials(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    defaults = BrowserVerbs.call_on.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "timeout", 0.1)  # the default is bound when `call_on` is defined
    assert (await rig.browser.handle({"verb": "snapshot"}))["ok"]
    rig.peers[0].holds["Accessibility.getFullAXTree"] = asyncio.Event()  # never answered
    assert await rig.browser.handle({"verb": "snapshot"}) == {"ok": False, "error": "TimeoutError"}
    assert not rig.browser.connected
    assert (await rig.browser.handle({"verb": "snapshot"}))["ok"]
    assert rig.dials == 2


class Ledger:
    """The computer's epoch ledger (the driver keeps it in `/task/.browser-epochs`): the highest
    epoch any session reserved."""

    def __init__(
        self,
        *,
        fail: bool = False,
        fail_on: frozenset[int] = frozenset(),
        hang: bool = False,
        hang_on: frozenset[int] = frozenset(),
        ceiling: int = 0,
    ) -> None:
        self.ceiling = ceiling
        self.calls: list[int] = []
        self.fail = fail
        self.fail_on = fail_on  # 1-based call numbers that fail
        self.hang = hang
        self.hang_on = hang_on  # 1-based call numbers that never answer

    async def __call__(self, block: int) -> int:
        self.calls.append(block)
        if self.hang or len(self.calls) in self.hang_on:
            await asyncio.Event().wait()
        if self.fail or len(self.calls) in self.fail_on:
            raise ConnectionError("the ledger could not be written")
        base, self.ceiling = self.ceiling, self.ceiling + block
        return base


async def _session(rig: Rig, ledger: Ledger, *, block: int = 100) -> RemoteBrowser:
    async def connect() -> WsCdp:
        async def answer(method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
            return await rig.fake.call(method, params, session_id=session)

        peer = WsPeer(answer)
        rig.peers.append(peer)
        return await peer.connect()

    return RemoteBrowser(connect, Recorder(FrameSink(rig.files), FOLDER), reserve=ledger, block=block)


async def test_a_fresh_computers_first_snapshot_is_epoch_one(rig: Rig) -> None:
    ledger = Ledger()
    step = await _session(rig, ledger)
    try:
        assert (await step.handle({"verb": "snapshot"}))["epoch"] == 1
        assert ledger.calls == [100] and ledger.ceiling == 100
    finally:
        await step.close()


async def test_a_later_session_starts_past_every_epoch_an_earlier_one_reserved(rig: Rig) -> None:
    """A reference the model kept from an earlier step is stale in the next one,
    never another element of the next page."""
    ledger = Ledger()
    first = await _session(rig, ledger)
    old = await first.handle({"verb": "snapshot"})
    await first.close()
    second = await _session(rig, ledger)
    try:
        new = await second.handle({"verb": "snapshot"})
        assert new["epoch"] == 101
        kept = ref_for(old, 'button "Save changes"')
        assert await second.handle({"verb": "click", "ref": kept}) == {
            "ok": False,
            "error": f"stale reference {kept}; take a new snapshot",
        }
    finally:
        await second.close()


async def test_a_session_that_uses_up_its_block_reserves_the_next_before_passing_it(rig: Rig) -> None:
    ledger = Ledger()
    step = await _session(rig, ledger, block=2)
    try:
        epochs = [(await step.handle({"verb": "snapshot"}))["epoch"] for _ in range(3)]
        assert epochs == [1, 2, 3] and ledger.calls == [2, 2] and ledger.ceiling == 4
    finally:
        await step.close()


async def test_a_ledger_that_cannot_be_written_never_stops_the_browser(rig: Rig) -> None:
    step = await _session(rig, Ledger(fail=True))
    try:
        assert (await step.handle({"verb": "snapshot"}))["ok"]
    finally:
        await step.close()


async def _used(step: RemoteBrowser, ledger: Ledger, cmd: dict[str, Any], *, covered: bool) -> int:
    got = await step.handle(cmd)
    assert got["ok"]
    if covered:
        assert got["epoch"] <= ledger.ceiling, (got["epoch"], ledger.ceiling)
    return int(got["epoch"])


async def test_the_ledger_catches_up_after_a_failed_write_and_no_epoch_is_repeated(rig: Rig) -> None:
    """Q4 invariant: after a failure past the session's first reservation, the first good reserve
    covers every epoch used; a new session starts strictly past them all. A failed FIRST
    reservation counts from the clock instead (`..._counts_from_the_clock_past_any_ledger`)."""
    failing = 2  # the session's second reservation
    ledger = Ledger(fail_on=frozenset({failing}))
    first = await _session(rig, ledger, block=2)
    used: list[int] = []
    try:
        for n in range(7):
            # until the throttled retry (a block of epochs past the failure) a verb may sit past the ledger
            used.append(await _used(first, ledger, {"verb": "snapshot"}, covered=n > failing))
    finally:
        await first.close()
    assert used == list(range(1, 8))
    second = await _session(rig, ledger, block=2)
    try:
        assert (await second.handle({"verb": "snapshot"}))["epoch"] > max(used)
    finally:
        await second.close()


async def test_a_reconnect_within_a_step_neither_reserves_again_nor_repeats_an_epoch(rig: Rig) -> None:
    ledger = Ledger()
    step = await _session(rig, ledger)
    try:
        assert (await step.handle({"verb": "snapshot"}))["epoch"] == 1
        await rig.peers[0].drop(1011, "the relay restarted")
        assert (await step.handle({"verb": "snapshot"}))["ok"] is False  # the dropped call
        assert (await step.handle({"verb": "snapshot"}))["epoch"] == 2
        assert ledger.calls == [100]
    finally:
        await step.close()


async def test_a_verb_that_snapshots_again_stays_under_the_written_ceiling(rig: Rig) -> None:
    ledger = Ledger()
    step = await _session(rig, ledger, block=2)
    try:
        for _ in range(4):
            got = await step.handle({"verb": "snapshot", "snapshot": True})
            assert got["ok"] and got["epoch"] <= ledger.ceiling, (got["epoch"], ledger.ceiling)
    finally:
        await step.close()


async def test_a_ledger_that_never_answers_is_a_failed_write_not_a_stalled_verb(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("prismoid.ai.frames.remote_browser.RESERVE_TIMEOUT_S", 0.05)
    step = await _session(rig, Ledger(hang=True))
    try:
        assert (await asyncio.wait_for(step.handle({"verb": "snapshot"}), 5))["ok"]
    finally:
        await step.close()


@pytest.mark.parametrize("hang", [True, False])
async def test_a_failed_ledger_is_not_asked_again_until_a_block_of_epochs_is_used(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hang: bool
) -> None:
    """The session's second reservation fails (a first one that fails counts from the clock and
    never asks again): the next try waits a block of epochs, then catches up."""
    monkeypatch.setattr("prismoid.ai.frames.remote_browser.RESERVE_TIMEOUT_S", 0.05)
    ledger = Ledger(
        hang_on=frozenset({2}) if hang else frozenset(), fail_on=frozenset() if hang else frozenset({2})
    )
    step = await _session(rig, ledger, block=2)
    try:
        counts = []
        for _ in range(4):
            assert (await step.handle({"verb": "snapshot"}))["ok"]
            counts.append(len(ledger.calls))
        assert counts == [1, 2, 2, 4]
    finally:
        await step.close()


PLAUSIBLE = 10**12  # past any ledger a computer could hold: a trillion snapshots


@pytest.mark.parametrize("hang", [True, False])
async def test_a_failed_first_reservation_counts_from_the_clock_past_any_ledger(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hang: bool
) -> None:
    """Review I1: a session whose first reservation fails knows no ledger value, so counting from 0
    would repeat an earlier step's epochs. It counts from a millisecond clock instead - past any
    plausible ledger - and asks the ledger no more (no ledger reaches it)."""
    monkeypatch.setattr("prismoid.ai.frames.remote_browser.RESERVE_TIMEOUT_S", 0.05)
    earlier = Ledger(ceiling=500)  # an earlier step used epochs up to 500
    earlier.fail, earlier.hang = not hang, hang
    step = await _session(rig, earlier, block=2)
    try:
        epochs = [(await step.handle({"verb": "snapshot"}))["epoch"] for _ in range(5)]
    finally:
        await step.close()
    assert epochs[0] > PLAUSIBLE and epochs == list(range(epochs[0], epochs[0] + 5))
    assert len(earlier.calls) == 1, "a clock-based session never asks the ledger again"


async def test_serial_sessions_whose_ledger_fails_never_collide(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each failed session reads the clock at its own start: the next starts past every epoch the
    last used, because a snapshot takes longer than the clock's millisecond."""
    now = [1_760_000_000_000]
    monkeypatch.setattr("prismoid.ai.frames.remote_browser.clock_base", lambda: now[0])
    used: list[int] = []
    for _ in range(2):
        step = await _session(rig, Ledger(fail=True))
        try:
            for _ in range(3):
                used.append(int((await step.handle({"verb": "snapshot"}))["epoch"]))
                now[0] += 2  # a snapshot's milliseconds
        finally:
            await step.close()
    assert len(set(used)) == len(used) and used == sorted(used), used


async def test_a_healthy_session_crossing_blocks_never_warns_the_ledger_is_ahead(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[str] = []
    monkeypatch.setattr("prismoid.ai.frames.remote_browser.logfire.warn", lambda msg, **_: warned.append(msg))
    step = await _session(rig, Ledger(), block=2)
    try:
        for _ in range(7):
            assert (await step.handle({"verb": "snapshot"}))["ok"]
    finally:
        await step.close()
    assert warned == []
