# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen relay on fakes: the fan-out, the one-slot buffer and the write
timeout, the input gate, the mask, the upstream's reopen and backoff, and the linger.

No database and no Docker: the upstream (`FakeUpstream`, a ScreenStream on queues), the lease
(`Leases`), the clock and the sleep are injected. `Clock(step=1.0)` moves one second per read, so
every pacer gap and bucket refill is satisfied and frames flow; `Clock(step=0.0)` stands still for
the rate cap and the settle. `Naps` records every backoff and linger delay and can park, so the
backoff is asserted as numbers, never timed. "Nothing more happens" is `drain()`: every ready task
runs to its next wait, with no wall-clock window."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import math
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import pytest

from prismoid.ai.frames.screen import ScreenClosed
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.operations import screen_control, screen_fanout, screen_relay
from prismoid.operations.machines.screen import HeldLease, NoScreen, Opened
from prismoid.operations.screen_state import OpenUpstream, Viewer
from prismoid.schemas.events import ActorRef
from prismoid.schemas.ids import ActorId
from prismoid.schemas.screen import PASTE_MAX, Lease, LeaseLimits, decode, grace_until, reopen_until

MID, TASK = uuid.uuid4(), uuid.uuid4()
ADA, BOB, CY = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
JPEG = b"\xff\xd8\xff\xe0"
LIMITS = LeaseLimits(silence_s=60, private_cap_s=600, grace_s=30, renew_every_s=5, settle_ms=300)


class FakeUpstream:
    """A ScreenStream on queues: the test says what the sidecar says; `sent` is what it heard."""

    def __init__(self) -> None:
        self.down: asyncio.Queue[tuple[dict[str, Any], bytes] | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        item = await self.down.get()
        if item is None:
            raise ScreenClosed("the bridge exited")
        return item

    async def send(self, header: Mapping[str, Any], payload: bytes = b"") -> None:
        if self.closed:
            raise ScreenClosed("closed")
        self.sent.append(dict(header))

    async def close(self) -> None:
        self.closed = True
        self.down.put_nowait(None)

    def frame(self, seq: int) -> None:
        header = {"t": "frame", "seq": seq, "ts": seq, "w": 1280, "h": 900, "fmt": "jpeg"}
        self.down.put_nowait((header, JPEG + bytes([seq])))

    def say(self, header: dict[str, Any]) -> None:
        self.down.put_nowait((header, b""))

    def end(self) -> None:
        self.down.put_nowait(None)

    def inputs(self) -> list[dict[str, Any]]:
        return [h for h in self.sent if h["t"] in ("pointer", "key", "text", "back")]

    def hints(self) -> list[dict[str, Any]]:
        return [h for h in self.sent if h["t"] == "hint"]


class FakeSocket:
    """A screen socket that keeps what it was sent, decoded; `stalled` never finishes a write."""

    def __init__(self, *, stalled: bool = False) -> None:
        self.got: list[tuple[dict[str, Any], bytes]] = []
        self.closed_with: tuple[int, str | None] | None = None
        self.stalled = stalled

    async def send_bytes(self, data: bytes) -> None:
        if self.stalled:
            await asyncio.Event().wait()
        self.got.append(decode(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = (code, reason)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [h for h, _ in self.got if h["t"] == kind]

    def frames(self) -> list[int]:
        return [payload[-1] for h, payload in self.got if h["t"] == "frame"]


PNG = b"\x89PNG\r\n\x1a\ncursor"


def cursor_move(up: FakeUpstream, x: int, y: int) -> None:
    up.down.put_nowait(({"t": "cursor", "serial": 1, "x": x, "y": y}, b""))


def cursor_shape(up: FakeUpstream) -> None:
    up.down.put_nowait(({"t": "cursor", "serial": 1, "w": 24, "h": 24, "hot_x": 1, "hot_y": 2}, PNG))


def cursors(sock: FakeSocket) -> list[tuple[int, int] | str]:
    """The cursor messages a socket got: a move's (x, y), or "shape"."""
    return ["shape" if payload else (h["x"], h["y"]) for h, payload in sock.got if h["t"] == "cursor"]


Play = tuple[FakeUpstream, int] | BaseException | Callable[[], tuple[FakeUpstream, int]]


class Opener:
    """`open_upstream` on a script: each call takes the next play, and the last one repeats."""

    def __init__(self, *plays: Play) -> None:
        self.plays: deque[Play] = deque(plays)
        self.calls = 0
        self.control = False

    def then(self, *plays: Play) -> None:
        self.plays = deque(plays)

    async def __call__(self, machine_id: uuid.UUID) -> Opened:
        self.calls += 1
        play = self.plays[0] if len(self.plays) == 1 else self.plays.popleft()
        if isinstance(play, BaseException):
            raise play
        stream, epoch = play() if callable(play) else play
        return Opened(stream=stream, epoch=epoch, control=self.control)


class FakeControl:
    """The holder's control stream, played: RFB down on a queue, what went up kept."""

    def __init__(self) -> None:
        self.down: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False
        self.gate: asyncio.Event | None = (
            None  # while set to an event, a send waits for it (the provider stalled)
        )

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        item = await self.down.get()
        if item is None:
            raise ScreenClosed("the control stream (VNC) closed")
        return {"t": "vnc"}, item

    async def send(self, header: Mapping[str, Any], payload: bytes = b"") -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.closed:
            raise ScreenClosed("closed")
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True
        self.down.put_nowait(None)


class Controls:
    """`open_control` on a list: each open takes the next FakeControl (a fresh one when empty)."""

    def __init__(self) -> None:
        self.opened: list[FakeControl] = []
        self.ready: deque[FakeControl] = deque()
        self.refuse = False

    async def __call__(self, machine_id: uuid.UUID) -> FakeControl:
        if self.refuse:
            raise ScreenClosed("the provider refused a takeover (unavailable)")
        control = self.ready.popleft() if self.ready else FakeControl()
        self.opened.append(control)
        return control


class Leases:
    """`lease_of` on a variable. `reading` is set by every read; while `gate` is set to an event, a
    read waits for it (a slow database); while `failing`, a read raises (the database is gone)."""

    def __init__(self) -> None:
        self.current: HeldLease | None = None
        self.reading = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.failing = False

    async def __call__(self, machine_id: uuid.UUID) -> HeldLease | None:
        self.reading.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failing:
            raise ConnectionError("the database went away")
        return self.current

    def until(self, until: dt.datetime) -> None:
        """The row's `until` moved (the grace, or its lifting)."""
        assert self.current is not None
        self.current = dataclasses.replace(
            self.current, lease=dataclasses.replace(self.current.lease, until=until)
        )

    def hold(self, actor: uuid.UUID, conn: str, *, private: bool = False, age_s: float = 5.0) -> None:
        now = utcnow()
        since = now - dt.timedelta(seconds=age_s)
        cap = since + dt.timedelta(seconds=600)
        until = cap if private else now + dt.timedelta(seconds=60)
        lease = Lease(
            actor_id=actor,
            conn=conn,
            private=private,
            since=since,
            until=until,
            cap_at=cap if private else None,
        )
        name = {ADA: "Ada", BOB: "Bob", CY: "Cy"}[actor]
        self.current = HeldLease(lease=lease, holder=ActorRef(type="user", id=ActorId(actor), name=name))


class Clock:
    """The relay's monotonic clock: every read moves it `step` seconds."""

    def __init__(self, step: float = 1.0) -> None:
        self.t, self.step = 1000.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class Naps:
    """The relay's sleep: records each delay; returns at once while `open` is set; from the
    `park_after`-th nap on, parks until cancelled (a nudge cuts a backoff nap short)."""

    def __init__(self, *, park_after: int | None = None) -> None:
        self.asked: list[float] = []
        self.park_after = park_after
        self.open = asyncio.Event()
        self.open.set()

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        await asyncio.sleep(0)
        if self.park_after is not None and len(self.asked) >= self.park_after:
            await asyncio.Event().wait()
        await self.open.wait()


class Rig:
    """A relay on fakes, and what it did to the lease."""

    def __init__(
        self, plays: tuple[Play, ...], *, clock: Clock, naps: Naps, opener: OpenUpstream | None = None
    ) -> None:
        self.opener = Opener(*plays)
        self.leases = Leases()
        self.clock, self.naps = clock, naps
        self.renewed: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        self.grace: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        self.reopened: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        self.closing: asyncio.Event | None = None  # while an event, `socket_closed` waits for it
        self.controls = Controls()
        self.relay = screen_relay.Relay(
            open_upstream=opener or self.opener,
            lease_of=self.leases,
            renew=self._renew,
            socket_closed=self._socket_closed,
            socket_reopened=self._socket_reopened,
            open_control=self.controls,
            clock=clock,
            sleep=naps,
        )

    async def _renew(self, machine_id: uuid.UUID, actor_id: uuid.UUID, conn: str) -> None:
        self.renewed.append((machine_id, actor_id, conn))

    def _bound(self, actor_id: uuid.UUID, conn: str) -> Lease | None:
        held = self.leases.current
        if held is None or (held.lease.actor_id, held.lease.conn) != (actor_id, conn):
            return None
        return held.lease

    async def _socket_closed(self, machine_id: uuid.UUID, actor_id: uuid.UUID, conn: str) -> None:
        """The row's grace, as `machines.screen.socket_closed` writes it."""
        if self.closing is not None:
            await self.closing.wait()
        self.grace.append((machine_id, actor_id, conn))
        if (lease := self._bound(actor_id, conn)) is not None:
            self.leases.until(grace_until(lease, utcnow(), LIMITS))

    async def _socket_reopened(self, machine_id: uuid.UUID, actor_id: uuid.UUID, conn: str) -> None:
        """The grace lifted, as `machines.screen.socket_reopened` writes it."""
        self.reopened.append((machine_id, actor_id, conn))
        lease = self._bound(actor_id, conn)
        if lease is not None and (until := reopen_until(lease, utcnow(), LIMITS)) is not None:
            self.leases.until(until)

    async def viewer(
        self, actor: uuid.UUID, conn: str, *, pane: bool = True, stalled: bool = False
    ) -> tuple[Viewer, FakeSocket]:
        sock = FakeSocket(stalled=stalled)
        viewer = Viewer(socket=sock, actor_id=actor, conn=conn, task_id=TASK, machine_id=MID, pane=pane)
        await self.relay.attach(viewer)
        return viewer, sock


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "screen_write_timeout_s", 0.2)
    monkeypatch.setattr(settings.computer_use, "mask_settle_ms", 300)


@pytest.fixture
async def rig() -> AsyncIterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def make(
        *plays: Play, clock: Clock | None = None, naps: Naps | None = None, opener: OpenUpstream | None = None
    ) -> Rig:
        made.append(Rig(plays, clock=clock or Clock(), naps=naps or Naps(), opener=opener))
        return made[-1]

    yield make
    for each in made:
        await each.relay.close_machine(MID, 1001, "test over")


async def until(ready: Callable[[], object], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not ready():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def drain(rounds: int = 50) -> None:
    """Every ready task runs to its next wait: a deterministic "and nothing more happens"."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def story(sock: FakeSocket) -> list[str]:
    """What a viewer was told about its screen, in order: the relay's status words, `frame`, `reset`."""
    out: list[str] = []
    for header, _ in sock.got:
        if header["t"] in ("frame", "reset"):
            out.append(header["t"])
        elif header["t"] == "status" and "upstream" in header:
            out.append(header["upstream"])
    return out


def masks(sock: FakeSocket) -> list[str | None]:
    """Every `masked` a socket heard, as the holder's name (None: unmasked)."""
    return [h["holder"]["name"] if h["holder"] is not None else None for h in sock.of("masked")]


CLICK: dict[str, Any] = {"t": "pointer", "type": "down", "x": 10, "y": 20, "button": "left", "buttons": 1}


# ── the fan-out and the buffer ────────────────────────────────────────────────────────────────


async def test_frames_fan_out_to_every_viewer(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001", pane=False)
    up.frame(1)
    await until(lambda: ada.frames() == [1] and bob.frames() == [1])
    up.frame(2)
    await until(lambda: ada.frames() == [1, 2] and bob.frames() == [1, 2])
    assert ada.got[0][0]["t"] == "hello" and ada.got[0][0]["v"] == 1
    assert story(ada) == ["live", "frame", "frame"]
    assert up.sent[0] == {"t": "hello", "v": 1} and up.sent[1]["t"] == "hint"


async def test_the_sidecars_status_goes_down_with_its_own_fields_only(rig: Callable[..., Rig]) -> None:
    """The sidecar's `status{chromium, busy}` is forwarded, to a late joiner too; a key the relay
    does not know never travels to a browser."""
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    up.say({"t": "status", "chromium": "running", "busy": True, "debug": "/home/browser/x"})
    up.frame(1)
    await until(lambda: ada.frames() == [1])
    _, late = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: late.frames() == [1])
    sidecar = {"t": "status", "chromium": "running", "busy": True, "n": 0}
    assert [h for h in ada.of("status") if "chromium" in h] == [sidecar]
    assert [h for h in late.of("status") if "chromium" in h] == [sidecar]
    assert set(ada.of("frame")[0]) == {"t", "seq", "ts", "w", "h", "fmt", "n"}


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("https://accounts.example.com", "https://accounts.example.com"),
        ("http://127.0.0.1:8088", "http://127.0.0.1:8088"),
        ("http://[::1]:9000", "http://[::1]:9000"),
        ("", ""),
        # Never more than an origin, and never a host a person could misread
        ("https://accounts.example.com/login?token=abc", ""),
        ("https://user:pw@accounts.example.com", ""),
        ("javascript:alert(1)", ""),
        ("https://accounts.example.com‮", ""),
        ("https://аccounts.example.com", ""),  # a Cyrillic a: Chromium states hosts in punycode
        ("https://" + "a" * 300 + ".com", ""),
        (7, None),
    ],
)
async def test_the_sidecars_origin_goes_down_as_an_origin_or_not_at_all(
    rig: Callable[..., Rig], said: object, shown: str | None
) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    up.say({"t": "status", "chromium": "running", "busy": False, "origin": said})
    up.frame(1)
    await until(lambda: ada.frames() == [1])
    [status] = [h for h in ada.of("status") if "chromium" in h]
    assert status.get("origin") == shown


async def test_the_origin_the_page_could_not_reach_goes_down_by_the_same_rule(
    rig: Callable[..., Rig],
) -> None:
    """`unreachable` names a host beside the address line: it is held to the origin's rule."""
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    up.say(
        {
            "t": "status",
            "chromium": "running",
            "busy": False,
            "origin": "",
            "unreachable": "https://off.example",
        }
    )
    up.say(
        {
            "t": "status",
            "chromium": "running",
            "busy": False,
            "origin": "",
            "unreachable": "https://off.example/?q=1",
        }
    )
    up.frame(1)
    await until(lambda: ada.frames() == [1])
    said = [h.get("unreachable") for h in ada.of("status") if "chromium" in h]
    assert said == ["https://off.example", ""]


async def test_back_is_input_only_the_holder_sends(rig: Callable[..., Rig]) -> None:
    """The screen's way back from a page moves the page: the input gate's, and nothing more rides it."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    cy, _ = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(cy, {"t": "back"})
    await r.relay.input(ada1, {"t": "back", "to": "https://evil.example"})
    await until(lambda: up.inputs())
    await drain()
    assert up.inputs() == [{"t": "back"}] and cy.dropped == 1


async def test_a_frame_bigger_than_a_viewers_budget_is_shown_slower_not_never(
    rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every frame of a dense page can outgrow one second of the budget: it still goes."""
    monkeypatch.setattr(settings.computer_use, "screen_viewer_bps", 1)
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1])


async def test_a_viewer_that_never_reads_never_stalls_the_others(rig: Callable[..., Rig]) -> None:
    """A one-slot buffer and a sender per viewer; a write past the timeout is 4408."""
    up = FakeUpstream()
    r = rig((up, 1))
    slow_viewer, slow = await r.viewer(CY, "cy_tab_0001", stalled=True)
    _, fast = await r.viewer(ADA, "ada_tab_0001")
    for seq in (1, 2, 3):
        up.frame(seq)
        await until(lambda seq=seq: seq in fast.frames())
    await until(lambda: slow.closed_with is not None)
    assert slow.closed_with == (4408, "too slow") and slow_viewer.closed
    assert fast.frames() == [1, 2, 3] and fast.closed_with is None


async def test_close_machine_says_bye_closes_every_socket_and_the_upstream(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1] and cy.frames() == [1])
    await r.relay.close_machine(MID, 4404, "reset")
    for sock in (ada, cy):
        assert sock.of("bye")[-1]["reason"] == "reset" and sock.closed_with == (4404, "reset")
        assert sock.got[-1][0]["t"] == "bye"  # nothing follows the bye
    await until(lambda: up.closed)


# ── the input gate ────────────────────────────────────────────────────────────────────────────


async def test_input_is_gated_on_the_lease_actor_and_connection(rig: Callable[..., Rig]) -> None:
    """Host-side: Cy is no holder; Ada's second tab is her, on another connection."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    ada2, _ = await r.viewer(ADA, "ada_tab_0002")
    cy, _ = await r.viewer(CY, "cy_tab_0001")
    assert r.relay.held(MID) == r.leases.current and r.relay.held(uuid.uuid4()) is None
    await until(lambda: up.hints())
    await r.relay.input(cy, CLICK)
    await r.relay.input(ada2, CLICK)
    await r.relay.input(ada1, {**CLICK, "x": 11})
    await until(lambda: up.inputs())
    await drain()
    assert [i["x"] for i in up.inputs()] == [11]
    assert (cy.dropped, ada2.dropped, ada1.dropped) == (1, 1, 0)


def _released(up: FakeUpstream) -> int:
    return sum(1 for h in up.sent if h["t"] == "release")


async def test_whatever_a_driver_holds_is_let_go_when_they_stop_driving(rig: Callable[..., Rig]) -> None:
    """Server-side: the client's own release never arrives when the server
    ends the lease (the relay drops a non-holder's input) or the tab is gone, so the relay tells
    the sidecar to let go whenever the connection that drove stops driving - an ending, a re-claim
    from another tab, its last socket closing - and never on a renewal."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await r.viewer(CY, "cy_tab_0001")  # someone still watching: the upstream stays open
    await until(lambda: up.hints())
    await r.relay.lease_changed(MID)  # a renewal: the same connection drives
    await drain()
    assert _released(up) == 0
    r.leases.hold(ADA, "ada_tab_0002")  # a re-claim from her other tab
    await r.relay.lease_changed(MID)
    await until(lambda: _released(up) == 1)
    r.leases.current = None  # an ending
    await r.relay.lease_changed(MID)
    await until(lambda: _released(up) == 2)
    r.leases.hold(ADA, "ada_tab_0001")
    await r.relay.lease_changed(MID)
    await r.relay.detach(ada1)  # her driving tab closed
    await until(lambda: _released(up) == 3)


def _roles(sock: FakeSocket) -> list[bool]:
    return [h["driver"] for h in sock.of("role")]


async def test_each_socket_is_told_whether_its_connection_drives(rig: Callable[..., Rig]) -> None:
    """The page never guesses "this tab drives" (a re-claim from another tab, or a lease that
    ended, left a pane saying "You are driving" and typing into nothing): the relay tells each
    socket its role, on attach and whenever it changes, never twice in a row."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    _, ada1 = await r.viewer(ADA, "ada_tab_0001")
    _, ada2 = await r.viewer(ADA, "ada_tab_0002")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: (_roles(ada1), _roles(ada2), _roles(cy)) == ([True], [False], [False]))
    await r.relay.lease_changed(MID)  # a renewal: nothing new to say
    r.leases.hold(ADA, "ada_tab_0002")  # "Take control here" in her other tab
    await r.relay.lease_changed(MID)
    await until(lambda: (_roles(ada1), _roles(ada2), _roles(cy)) == ([True, False], [False, True], [False]))
    r.leases.current = None
    await r.relay.lease_changed(MID)
    await until(lambda: (_roles(ada1), _roles(ada2)) == ([True, False], [False, True, False]))


async def test_an_unknown_kind_or_a_bad_field_is_ignored_never_forwarded(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, sock = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, {"t": "teleport", "x": 1, "y": 1})
    await r.relay.hint(ada1, {"t": "pointer", "type": "down", "x": 1, "y": 1})
    await r.relay.input(ada1, {"t": "pointer", "type": "levitate", "x": 1, "y": 1})
    await r.relay.input(ada1, {"t": "pointer", "type": "down", "x": "1", "y": 1})
    await r.relay.input(ada1, {"t": "text", "text": "x" * (PASTE_MAX + 1)})
    await r.relay.input(ada1, {**CLICK, "evil": "<script>"})
    await until(lambda: up.inputs())
    assert up.inputs() == [CLICK]
    assert ada1.dropped == 3 and sock.closed_with is None


async def test_a_kind_or_type_that_is_not_a_string_is_dropped_never_raised(rig: Callable[..., Rig]) -> None:
    """JSON can make a pointer's or a key's `type` a list or an object: a malformed input, dropped
    and counted like any other - never a TypeError out of the gate, which the socket would have
    answered with 1011. A `t` that is not a string is no input at all."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, sock = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, {**CLICK, "type": [1]})
    await r.relay.input(ada1, {"t": "key", "type": {"down": True}, "key": "a"})
    await r.relay.input(ada1, {**CLICK, "t": ["pointer"]})
    await r.relay.input(ada1, CLICK)
    await until(lambda: up.inputs())
    await drain()
    assert up.inputs() == [CLICK] and ada1.dropped == 2 and sock.closed_with is None


async def test_a_paste_the_wire_cannot_carry_is_dropped_and_the_upstream_lives(
    rig: Callable[..., Rig],
) -> None:
    """1,000 control characters escape to 6,000 header bytes, past HEADER_MAX. The
    relay drops the paste at the gate; it never reaches the upstream's writer to break it."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, {"t": "text", "text": "\x01" * PASTE_MAX})
    await r.relay.input(ada1, CLICK)
    await until(lambda: up.inputs())
    await drain()
    assert up.inputs() == [CLICK] and ada1.dropped == 1
    assert not up.closed and r.opener.calls == 1


async def test_a_paste_with_a_lone_surrogate_is_dropped_and_the_holder_stays(
    rig: Callable[..., Rig],
) -> None:
    """A holder's clipboard can carry half a surrogate pair, which its JSON escapes and the wire
    cannot re-encode: the paste is dropped at the gate, and the holder's next input still goes up."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, {"t": "text", "text": "\ud800x"})
    await r.relay.input(ada1, CLICK)
    await until(lambda: up.inputs())
    await drain()
    assert up.inputs() == [CLICK] and ada1.dropped == 1


async def test_non_finite_coordinates_are_dropped_never_re_encoded(rig: Callable[..., Rig]) -> None:
    """Python's JSON reads NaN and Infinity: a coordinate that is not finite drops the input, and
    an optional number that is not finite is left out."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, {**CLICK, "x": math.nan})
    await r.relay.input(ada1, {**CLICK, "y": math.inf})
    await r.relay.input(ada1, {**CLICK, "dx": -math.inf})
    await until(lambda: up.inputs())
    await drain()
    assert up.inputs() == [CLICK] and ada1.dropped == 2


async def test_a_flood_is_refused_before_it_is_parsed(
    rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket and the lease come first: a non-holder's flood and input past the rate cost no
    parse and no encode, and a non-holder spends their own bucket like anyone."""
    parsed: list[Mapping[str, Any]] = []
    real = screen_relay.clean_input

    def counting(header: Mapping[str, Any]) -> dict[str, Any] | None:
        parsed.append(header)
        return real(header)

    monkeypatch.setattr(screen_relay, "clean_input", counting)
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    cy, _ = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: up.hints())
    for _ in range(200):
        await r.relay.input(cy, CLICK)
    assert parsed == [] and cy.dropped == 200
    await r.relay.hint(cy, {"t": "hint", "refresh": True})  # his bucket is spent: no refresh goes up
    for _ in range(130):
        await r.relay.input(ada1, CLICK)
    await until(lambda: len(up.inputs()) == 120)
    await drain()
    assert len(parsed) == 120 and ada1.dropped == 10
    assert {"t": "hint", "refresh": True} not in up.hints()


async def test_the_settle_drops_a_private_holders_first_input(rig: Callable[..., Rig]) -> None:
    """Nothing typed reaches the screen before every relay can know the mask is on."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True, age_s=0.0)
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, {"t": "text", "text": "hunter2"})
    r.leases.hold(ADA, "ada_tab_0001", private=True, age_s=1.0)
    await r.relay.lease_changed(MID)
    await r.relay.input(ada1, {"t": "text", "text": "hunter2"})
    await until(lambda: up.inputs())
    assert up.inputs() == [{"t": "text", "text": "hunter2"}] and ada1.dropped == 1


async def test_the_settle_also_counts_from_when_this_relay_first_saw_the_private_lease(
    rig: Callable[..., Rig],
) -> None:
    """`since` is the database's clock, backdated by the claim's own pre-commit time: the relay also
    drops the holder's input for `mask_settle_ms` after IT first sees that private lease, on its
    own monotonic clock. A fresh private claim (a new `since`) settles again."""
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    r.leases.hold(ADA, "ada_tab_0001", private=True, age_s=5.0)  # long settled by the row's clock
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    secret = {"t": "text", "text": "hunter2"}
    await r.relay.input(ada1, secret)  # this relay saw the lease a moment ago: dropped
    r.clock.t += 0.2
    await r.relay.input(ada1, secret)  # 200 ms on: still dropped
    r.clock.t += 0.2
    await r.relay.input(ada1, secret)  # 400 ms on: through
    await until(lambda: up.inputs())
    r.leases.hold(ADA, "ada_tab_0001", private=True, age_s=6.0)  # claimed afresh: another `since`
    await r.relay.lease_changed(MID)
    await r.relay.input(ada1, secret)
    await drain()
    assert up.inputs() == [secret] and ada1.dropped == 3


async def test_pointer_moves_coalesce_and_keep_their_place(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    for x in (1, 2, 3):
        await r.relay.input(ada1, {"t": "pointer", "type": "move", "x": x, "y": 0})
    await r.relay.input(ada1, {"t": "pointer", "type": "down", "x": 3, "y": 0})
    for x in (4, 5):
        await r.relay.input(ada1, {"t": "pointer", "type": "move", "x": x, "y": 0})
    await until(lambda: len(up.inputs()) == 3)
    await drain()
    assert [(i["type"], i["x"]) for i in up.inputs()] == [("move", 3), ("down", 3), ("move", 5)]


async def test_input_above_the_rate_is_dropped(rig: Callable[..., Rig]) -> None:
    """120 a second per socket (`screen_input_per_s`), the rest dropped."""
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    key = {"t": "key", "type": "down", "key": "a", "code": "KeyA"}
    for _ in range(200):
        await r.relay.input(ada1, key)
    await until(lambda: len(up.inputs()) == 120)
    await drain()
    assert len(up.inputs()) == 120 and ada1.dropped == 80
    r.clock.t += 1.0  # a second on, the bucket is full again
    await r.relay.input(ada1, key)
    await until(lambda: len(up.inputs()) == 121)


async def test_a_non_private_holders_input_renews_at_most_every_five_seconds(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    key = {"t": "key", "type": "down", "key": "a"}
    await r.relay.input(ada1, key)  # renews
    r.clock.t += 4.0
    await r.relay.input(ada1, key)  # 4 s on: throttled
    r.clock.t += 1.0
    await r.relay.input(ada1, key)  # exactly 5 s on: renews again
    await until(lambda: len(r.renewed) == 2)
    await drain()
    assert r.renewed == [(MID, ADA, "ada_tab_0001")] * 2


async def test_the_renewal_throttle_is_per_holder(rig: Callable[..., Rig]) -> None:
    """A new holder's first input renews at once, whoever renewed a moment ago."""
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    bob1, _ = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: up.hints())
    key = {"t": "key", "type": "down", "key": "a"}
    await r.relay.input(ada1, key)
    await until(lambda: len(r.renewed) == 1)
    r.leases.hold(BOB, "bob_tab_0001")  # Ada handed back; Bob took control
    await r.relay.lease_changed(MID)
    r.clock.t += 1.0
    await r.relay.input(bob1, key)
    await until(lambda: len(r.renewed) == 2)
    assert r.renewed == [(MID, ADA, "ada_tab_0001"), (MID, BOB, "bob_tab_0001")]


async def test_only_the_holders_bound_connection_closing_starts_the_grace(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    ada2, _ = await r.viewer(ADA, "ada_tab_0002")
    await r.relay.detach(ada2)
    await drain()
    assert r.grace == []
    await r.relay.detach(ada1)
    await until(lambda: r.grace)
    assert r.grace == [(MID, ADA, "ada_tab_0001")]


async def test_a_holders_glance_sees_their_own_private_screen_and_is_never_them_leaving(
    rig: Callable[..., Rig],
) -> None:
    """The CLI's `--shot` under its own private claim: the holder's connection, so it is not masked,
    but no pane - a one-off look whose close is not the holder walking away, so it starts no grace
    and a private lease keeps its ten minutes."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "cli_conn_0001", private=True)
    _, cy = await r.viewer(CY, "cy_tab_0001")
    glance, sock = await r.viewer(ADA, "cli_conn_0001", pane=False)
    up.frame(1)
    await until(lambda: sock.frames() == [1])
    await drain()
    assert cy.frames() == [], "everyone else is masked"
    await r.relay.detach(glance)
    await drain()
    assert r.grace == []


async def test_a_bound_connection_still_open_here_starts_no_grace(rig: Callable[..., Rig]) -> None:
    """A tab that reconnected (its new socket attached before the old one left) has not closed."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    old, _ = await r.viewer(ADA, "ada_tab_0001")
    new, _ = await r.viewer(ADA, "ada_tab_0001")
    await r.relay.detach(old)
    await drain()
    assert r.grace == []
    await r.relay.detach(new)
    await until(lambda: r.grace)
    assert r.grace == [(MID, ADA, "ada_tab_0001")]


# ── the upstream's hint ──────────────────────────────────────────────────────────────────────


async def test_the_upstream_hint_follows_the_viewers(rig: Callable[..., Rig]) -> None:
    """1 fps for thumbnails, 5 with a pane open, 30 only for the holder's own connection; paused
    when no viewer here can see a frame."""
    up = FakeUpstream()
    r = rig((up, 1))
    thumb, _ = await r.viewer(CY, "cy_tab_0001", pane=False)
    await until(lambda: up.hints())
    assert up.hints()[-1] == {"t": "hint", "fps": 1, "quality": 60, "paused": False}
    pane, _ = await r.viewer(BOB, "bob_tab_0001", pane=True)
    await until(lambda: up.hints()[-1]["fps"] == 5)
    r.leases.hold(BOB, "bob_tab_0001")
    await r.relay.lease_changed(MID)
    await until(lambda: up.hints()[-1]["fps"] == 30)
    for viewer in (thumb, pane):
        await r.relay.hint(viewer, {"t": "hint", "paused": True})
    await until(lambda: up.hints()[-1]["paused"] is True)


async def test_the_holders_connection_gets_the_driving_budget_while_it_drives(
    rig: Callable[..., Rig],
) -> None:
    """Driving needs frames a watcher does not: the lease holder's own connection is paced by
    `screen_holder_bps`, every other socket by `screen_viewer_bps`, and a lease that ends hands the
    holder's connection back to the watcher's budget."""
    up = FakeUpstream()
    r = rig((up, 1))
    bob, _ = await r.viewer(BOB, "bob_tab_0001")
    cy, _ = await r.viewer(CY, "cy_tab_0001")
    cu = settings.computer_use
    assert (bob.pacer.rate_bps, cy.pacer.rate_bps) == (cu.screen_viewer_bps, cu.screen_viewer_bps)
    r.leases.hold(BOB, "bob_tab_0001")
    await r.relay.lease_changed(MID)
    assert (bob.pacer.rate_bps, cy.pacer.rate_bps) == (cu.screen_holder_bps, cu.screen_viewer_bps)
    assert (bob.pacer.fps, cy.pacer.fps) == (cu.screen_fps_holder, cu.screen_fps_pane)
    r.leases.current = None
    await r.relay.lease_changed(MID)
    assert bob.pacer.rate_bps == cu.screen_viewer_bps


async def test_the_screencast_drops_to_the_driving_quality_while_the_holder_watches(
    rig: Callable[..., Rig],
) -> None:
    """One screencast serves everyone, so while the holder's connection watches its quality is
    capped at `screen_holder_quality` (smaller frames, more of them); it goes back once the lease
    ends. Each change restarts the screencast, inside the once-a-second rule."""
    up = FakeUpstream()
    r = rig((up, 1))
    await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: up.hints())
    assert up.hints()[-1]["quality"] == 60
    r.leases.hold(BOB, "bob_tab_0001")
    await r.relay.lease_changed(MID)
    await until(lambda: up.hints()[-1].get("quality") == 45)
    assert up.hints()[-1] == {"t": "hint", "fps": 30, "quality": 45, "paused": False}
    r.leases.current = None
    await r.relay.lease_changed(MID)
    await until(lambda: [h for h in up.hints() if "quality" in h][-1]["quality"] == 60)


async def test_a_paused_upstream_keeps_its_quality(rig: Callable[..., Rig]) -> None:
    """Nobody can see a frame, so nothing is worth a screencast restart: the driver hiding their tab
    pauses the upstream at the quality it had, and showing it again restarts nothing."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(BOB, "bob_tab_0001")
    bob, _ = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: up.hints() and up.hints()[-1].get("quality") == 45)
    await r.relay.hint(bob, {"t": "hint", "paused": True})
    await until(lambda: up.hints()[-1].get("paused") is True)
    await r.relay.hint(bob, {"t": "hint", "paused": False})
    await until(lambda: up.hints()[-1].get("paused") is False)
    assert {h["quality"] for h in up.hints() if "quality" in h} == {45}


# ── the mask ─────────────────────────────────────────────────────────────────────────────────


async def test_a_private_claim_masks_everyone_but_the_holders_connection(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1] and cy.frames() == [1])

    r.leases.hold(ADA, "ada_tab_0001", private=True)
    await r.relay.lease_changed(MID)
    await until(lambda: masks(cy) == [None, "Ada"])
    assert cy.of("masked")[-1]["holder"] == {"id": str(ADA), "name": "Ada"}
    assert masks(ada) == [None], "the holder's own connection is told only that it was unmasked"
    up.frame(2)
    await until(lambda: ada.frames() == [1, 2])

    _, late = await r.viewer(BOB, "bob_tab_0001")  # a late joiner, during the private lease
    _, tab2 = await r.viewer(ADA, "ada_tab_0002")  # Ada's own second tab is a viewer too
    await until(lambda: late.of("masked") and tab2.of("masked"))
    up.frame(3)
    await until(lambda: ada.frames() == [1, 2, 3])
    await drain()
    assert cy.frames() == [1] and late.frames() == [] and tab2.frames() == []


async def test_a_frame_during_a_late_joiners_lease_read_never_reaches_it(rig: Callable[..., Rig]) -> None:
    """The late joiner is registered only after its lease read, and masked before its sender
    starts: a private frame relayed while that read is in flight never reaches it."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1])

    r.leases.reading.clear()
    r.leases.gate = asyncio.Event()
    joining = asyncio.create_task(r.viewer(BOB, "bob_tab_0001"))
    await until(r.leases.reading.is_set)  # Bob's read is in flight
    up.frame(2)
    await until(lambda: ada.frames() == [1, 2])
    r.leases.gate.set()
    _, late = await joining
    await until(lambda: late.of("masked"))
    up.frame(3)
    await until(lambda: ada.frames() == [1, 2, 3])
    await drain()
    assert late.frames() == [] and story(late) == ["live"]


async def test_a_frame_waiting_for_a_viewer_is_never_sent_once_the_mask_lands(
    rig: Callable[..., Rig],
) -> None:
    """The one-slot buffer under the mask: a frame the pacer held back for Cy is dropped from his
    slot when the private claim lands, so it can never go out behind the mask."""
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1] and cy.frames() == [1])
    up.frame(2)  # inside the pacer's gap (the clock stands still): it waits in each slot
    await drain()
    assert cy.frames() == [1] and ada.frames() == [1]

    r.leases.hold(ADA, "ada_tab_0001", private=True)
    await r.relay.lease_changed(MID)
    r.clock.t += 10.0  # the gap is long past: whatever is still in a slot may go now
    up.frame(3)
    await until(lambda: masks(cy) == [None, "Ada"] and ada.frames()[-1] >= 2)
    r.clock.t += 10.0  # the holder may have taken frame 2 first; frame 3 follows past its gap
    await until(lambda: 3 in ada.frames())
    await drain()
    assert cy.frames() == [1]


async def test_the_end_of_a_private_lease_unmasks_and_asks_for_a_fresh_frame(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    up.frame(2)
    await until(lambda: ada.frames() == [2])

    before = len(up.hints())
    r.leases.current = None  # released
    await r.relay.lease_changed(MID)
    await until(lambda: cy.of("masked") and cy.of("masked")[-1]["holder"] is None)
    # a fresh frame: a refresh, or the restart back to the watchers' quality, which repaints too
    assert any(h.get("refresh") or h.get("quality") == 60 for h in up.hints()[before:])
    _, fresh = await r.viewer(BOB, "bob_tab_0001")
    await drain()
    assert fresh.frames() == []  # the frame cached under the mask was dropped, never served
    up.frame(3)
    await until(lambda: ada.frames() == [2, 3] and cy.frames() == [3] and fresh.frames() == [3])


async def test_switching_private_off_unmasks_and_asks_for_a_fresh_frame(rig: Callable[..., Rig]) -> None:
    """Switching Private off keeps the lease and its `since` (nothing ended), but the mask lifts:
    the frame cached under it is dropped, and the sidecar is asked for a fresh one."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    up.frame(2)
    await until(lambda: ada.frames() == [2] and cy.of("masked"))

    held = r.leases.current
    assert held is not None
    until_ = utcnow() + dt.timedelta(seconds=60)
    r.leases.current = dataclasses.replace(
        held, lease=dataclasses.replace(held.lease, private=False, until=until_, cap_at=None)
    )
    await r.relay.lease_changed(MID)
    await until(lambda: cy.of("masked")[-1]["holder"] is None)
    assert {"t": "hint", "refresh": True} in up.hints()
    _, fresh = await r.viewer(BOB, "bob_tab_0001")
    await drain()
    assert cy.frames() == [] and fresh.frames() == []
    up.frame(3)
    await until(lambda: ada.frames() == [2, 3] and cy.frames() == [3] and fresh.frames() == [3])


# ── the upstream's life ──────────────────────────────────────────────────────────────────────


async def test_the_screen_survives_a_sleep_and_reopens_on_the_new_epoch(rig: Callable[..., Rig]) -> None:
    """Viewers hear `asleep`, then a reset and frames again on the same socket,
    and nothing of the old epoch's frame is relayed after the wake."""
    first, second = FakeUpstream(), FakeUpstream()
    r = rig((first, 1), NoScreen("asleep"), naps=Naps(park_after=3))
    _, cy = await r.viewer(CY, "cy_tab_0001")
    first.frame(1)
    await until(lambda: cy.frames() == [1])

    first.end()  # the Machine slept: the bridge's exec died with its frame
    await until(lambda: "asleep" in story(cy))
    first.frame(9)  # the old incarnation, after its end: never read again
    r.opener.then((second, 2))
    await r.relay.wake(MID)  # `machine.woke` on the hub nudges the upstream
    second.frame(2)
    await until(lambda: cy.frames() == [1, 2])
    assert story(cy) == ["live", "frame", "asleep", "reset", "live", "frame"]
    _, late = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: late.frames() == [2])


async def test_a_warm_machine_without_a_browser_is_polled_never_spun_on(rig: Callable[..., Rig]) -> None:
    """Every open meets `bye{no_browser}`; the backoff doubles to 5 s and stays."""

    def no_browser() -> tuple[FakeUpstream, int]:
        up = FakeUpstream()
        up.say({"t": "hello", "v": 1})
        up.say({"t": "bye", "reason": "no_browser"})
        return up, 1

    r = rig(no_browser, naps=Naps(park_after=8))
    _, cy = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: len(r.naps.asked) == 8)
    assert r.naps.asked == [0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0]
    assert r.opener.calls == 8
    assert story(cy) == ["starting"]


async def test_a_machine_with_no_screen_says_so_and_waits_for_a_wake(rig: Callable[..., Rig]) -> None:
    """A Machine with no screen at all (a driver with no screen door) is `no_screen`
    to its viewers - never "starting", which the web turns into "starts with the agent's next
    step" - and its row is not polled: nothing changes by waiting, so only a `machine.*` event
    (`wake`) asks again."""
    r = rig(NoScreen("no_browser"))
    _, cy = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: story(cy) == ["no_screen"])
    await drain()
    assert r.opener.calls == 1 and r.naps.asked == []
    await r.relay.wake(MID)
    await until(lambda: r.opener.calls == 2)
    await drain()
    assert r.opener.calls == 2 and r.naps.asked == [] and story(cy) == ["no_screen"]


async def test_proofs_never_shorten_the_backoff_and_a_wake_does(rig: Callable[..., Rig]) -> None:
    """Under the socket's proofs: every viewer re-reads the lease every 10 s
    (`lease_changed`), and none of those reads reopens a cold Machine early; a `machine.*` event
    (`wake`) does, once."""
    r = rig(NoScreen("asleep"), naps=Naps(park_after=2))
    viewers = [await r.viewer(CY, f"cy_tab_{n:04d}") for n in range(5)]
    await until(lambda: len(r.naps.asked) == 2)  # 0.2 s, then parked in the 0.4 s nap
    for _ in range(20):
        for _viewer in viewers:
            await r.relay.lease_changed(MID)
    await drain()
    assert r.opener.calls == 2 and r.naps.asked == [0.2, 0.4]
    await r.relay.wake(MID)
    await until(lambda: len(r.naps.asked) == 3)
    assert r.opener.calls == 3 and r.naps.asked == [0.2, 0.4, 0.8]
    assert story(viewers[0][1]) == ["asleep"]


async def test_an_attach_racing_close_machine_is_refused_not_stranded(rig: Callable[..., Rig]) -> None:
    """A reset closes every socket on the Machine (`close_machine`). A socket whose attach was
    reading the lease meanwhile - or that arrives just after - is refused with the same bye and
    code; it never re-makes an upstream that no reset event would close. Past the tombstone, a
    socket is welcome again."""
    up = FakeUpstream()
    r = rig((up, 1), clock=Clock(step=0.0))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1])

    r.leases.reading.clear()
    r.leases.gate = asyncio.Event()
    joining = asyncio.create_task(r.viewer(BOB, "bob_tab_0001"))
    await until(r.leases.reading.is_set)  # Bob's read is in flight
    await r.relay.close_machine(MID, 4404, "reset")
    assert up.closed and ada.closed_with == (4404, "reset")
    r.leases.gate.set()
    bob, bob_sock = await joining
    assert bob.closed and bob_sock.closed_with == (4404, "reset")
    assert [h["t"] for h, _ in bob_sock.got] == ["bye"] and bob_sock.of("bye")[0]["reason"] == "reset"
    assert r.relay.held(MID) is None and r.opener.calls == 1  # no upstream was re-made

    r.leases.gate = None
    cy, cy_sock = await r.viewer(CY, "cy_tab_0001")  # just after the reset: refused too
    assert cy.closed and cy_sock.closed_with == (4404, "reset")
    r.clock.t += 10.0  # past the tombstone
    fresh = FakeUpstream()
    r.opener.then((fresh, 2))
    again, again_sock = await r.viewer(CY, "cy_tab_0002")
    fresh.frame(5)
    await until(lambda: again_sock.frames() == [5])
    assert not again.closed and r.opener.calls == 2


async def test_an_upstream_that_refuses_a_send_is_dropped_and_reopened(rig: Callable[..., Rig]) -> None:
    """`ScreenClosed` from a send is terminal for that stream: the relay never sends on it again,
    closes it, and reopens a fresh one after the backoff's nap."""

    class Refusing(FakeUpstream):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def send(self, header: Mapping[str, Any], payload: bytes = b"") -> None:
            self.attempts += 1
            if header["t"] == "pointer":
                raise ScreenClosed("the bridge would not take a message")
            await super().send(header, payload)

    broken, fresh = Refusing(), FakeUpstream()
    r = rig((broken, 1), (fresh, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: broken.hints())
    await r.relay.input(ada1, CLICK)
    await until(lambda: fresh.hints())
    assert broken.closed and broken.attempts == 3  # hello, hint, the refused click: nothing after
    assert r.naps.asked == [0.2] and r.opener.calls == 2
    await r.relay.input(ada1, {**CLICK, "x": 12})
    await until(lambda: fresh.inputs())
    assert [i["x"] for i in fresh.inputs()] == [12] and broken.attempts == 3


async def test_a_relay_that_lets_go_mid_open_still_closes_the_stream(rig: Callable[..., Rig]) -> None:
    """The open is shielded: when the relay is cancelled while `open_upstream` is still opening, the
    stream that open hands back afterwards is closed, never leaked, and nothing is sent on it."""
    up = FakeUpstream()
    started, gate = asyncio.Event(), asyncio.Event()

    async def slow_open(machine_id: uuid.UUID) -> Opened:
        started.set()
        await gate.wait()
        return Opened(up, 1)

    r = rig(opener=slow_open)
    await r.viewer(CY, "cy_tab_0001")
    await until(started.is_set)
    await r.relay.close_machine(MID, 4404, "reset")  # the pump is cancelled mid-open
    assert not up.closed
    gate.set()  # the open finishes after its caller has gone
    await until(lambda: up.closed)
    assert up.sent == []


async def test_the_upstream_lingers_after_its_last_viewer(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    naps = Naps()
    naps.open.clear()  # every nap waits for the test
    r = rig((up, 1), naps=naps)
    first, sock = await r.viewer(CY, "cy_tab_0001")
    up.frame(1)
    await until(lambda: sock.frames() == [1])

    await r.relay.detach(first)
    await until(lambda: naps.asked == [5])
    await drain()
    assert not up.closed  # lingering: a reload inside 5 s keeps the stream
    again, _ = await r.viewer(CY, "cy_tab_0002")
    await r.relay.detach(again)
    await until(lambda: naps.asked == [5, 5])
    naps.open.set()
    await until(lambda: up.closed)
    assert r.opener.calls == 1


# ── more relay edges ───────────────────────────────────────────────────────────────────────────


async def test_the_holders_4408_and_reconnect_on_the_same_connection_lift_the_grace(
    rig: Callable[..., Rig],
) -> None:
    """A private holder too slow for their budget is dropped 4408, which starts the
    grace; their tab reconnects on the SAME connection at once, and the attach gives the grace back
    (`socket_reopened`) - so the lease runs to its cap instead of ending 30 s later."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, slow = await r.viewer(ADA, "ada_tab_0001", stalled=True)
    await until(lambda: slow.closed_with is not None and r.grace)
    assert slow.closed_with == (4408, "too slow") and r.grace == [(MID, ADA, "ada_tab_0001")]
    graced = r.leases.current
    assert graced is not None and graced.lease.cap_at is not None and graced.lease.until < graced.lease.cap_at
    _, back = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: r.reopened)
    assert r.reopened == [(MID, ADA, "ada_tab_0001")]
    held = r.leases.current
    assert held is not None and held.lease.until == held.lease.cap_at
    assert masks(back) == [None]


async def test_a_reconnect_that_beats_the_graces_write_still_lifts_it(rig: Callable[..., Rig]) -> None:
    """The reconnect attaches while the closed socket's grace is still being written: its attach
    sees no grace to lift, so the relay lifts it once that write lands."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    old, _ = await r.viewer(ADA, "ada_tab_0001")
    r.closing = asyncio.Event()
    await r.relay.detach(old)
    await r.viewer(ADA, "ada_tab_0001")
    await drain()
    assert r.reopened == []
    r.closing.set()
    await until(lambda: r.reopened)
    held = r.leases.current
    assert r.grace == r.reopened == [(MID, ADA, "ada_tab_0001")]
    assert held is not None and held.lease.until == held.lease.cap_at


async def test_only_the_bound_connection_coming_back_lifts_a_grace(rig: Callable[..., Rig]) -> None:
    """Another tab of the holder's, or someone else, lifts nothing: a different connection is a
    re-claim's to rebind. An untouched lease is never written to."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    await r.viewer(ADA, "ada_tab_0001")
    held = r.leases.current
    assert held is not None
    r.leases.until(utcnow() + dt.timedelta(seconds=20))  # the grace, from a socket elsewhere
    await r.viewer(ADA, "ada_tab_0002")
    await r.viewer(CY, "cy_tab_0001")
    await drain()
    assert r.reopened == []


async def test_a_socket_that_attaches_unmasked_is_told_so(rig: Callable[..., Rig]) -> None:
    """A tab that was masked before it reconnected must hear that the mask
    lifted, or "Ada is signing in privately" sticks until a frame comes. Every attach says the mask
    state: `masked{holder: null}`, or the holder, right after `hello`."""
    up = FakeUpstream()
    r = rig((up, 1))
    _, cy = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: cy.of("masked"))
    assert masks(cy) == [None]
    kinds = [h["t"] for h, _ in cy.got]
    assert kinds.index("hello") < kinds.index("masked")
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, late = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: late.of("masked"))
    assert masks(late) == ["Ada"]


async def test_a_failed_lease_read_withholds_frames_from_everyone(rig: Callable[..., Rig]) -> None:
    """The mask fails CLOSED. A read that fails (a switch to Private, or an owner's End, may just
    have committed) leaves this relay not knowing who may see the screen - the cached holder
    included, since the cache is exactly what the read could not confirm - so until a read
    succeeds no socket gets a frame, and no mask is lifted or sent. A read that succeeds again asks
    for a fresh frame for everyone."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1] and cy.frames() == [1])
    told = len(cy.of("masked"))

    r.leases.failing = True
    await r.relay.lease_changed(MID)
    await until(lambda: up.hints()[-1].get("paused") is True)
    up.frame(2)
    await drain()
    assert ada.frames() == [1] and cy.frames() == [1] and len(cy.of("masked")) == told
    refreshes = sum(1 for h in up.hints() if h.get("refresh"))

    r.leases.failing = False
    await r.relay.lease_changed(MID)
    await until(lambda: sum(1 for h in up.hints() if h.get("refresh")) == refreshes + 1)
    up.frame(3)
    await until(lambda: ada.frames() == [1, 3] and cy.frames() == [1, 3])


async def test_a_stale_holder_neither_sees_nor_drives_while_the_relay_is_blind(
    rig: Callable[..., Rig],
) -> None:
    """The owner ended Ada's lease and Bob took PRIVATE control on another process; this relay
    hears only `screen.state`, and its re-read fails. Its cache still says Ada drives - so a frame
    of Bob's sign-in must not reach her, and her click must not reach the page."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001")
    ada_v, ada = await r.viewer(ADA, "ada_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    up.frame(1)
    await until(lambda: ada.frames() == [1] and bob.frames() == [1])

    r.leases.hold(BOB, "bob_tab_0001", private=True)
    r.leases.failing = True
    await r.relay.lease_changed(MID)
    up.frame(2)
    before = len(up.inputs())
    await r.relay.input(ada_v, CLICK)
    await drain()
    assert ada.frames() == [1] and bob.frames() == [1]
    assert len(up.inputs()) == before and ada_v.dropped == 1


async def test_a_failed_lease_read_never_lifts_a_mask(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: cy.of("masked"))
    r.leases.failing = True
    await r.relay.lease_changed(MID)
    up.frame(1)
    await drain()
    assert ada.frames() == [] and cy.frames() == [] and masks(cy) == ["Ada"]


async def test_screencast_restarts_go_up_at_most_once_a_second(rig: Callable[..., Rig]) -> None:
    """A refresh or a new quality restarts the sidecar's screencast. The first goes at once; every ask
    inside the next second becomes ONE deferred restart - a new quality's, whose restart sends a
    fresh frame anyway, so the refresh rides it."""
    up = FakeUpstream()
    naps = Naps()
    r = rig((up, 1), clock=Clock(step=0.0), naps=naps)
    viewer, _ = await r.viewer(CY, "cy_tab_0001")
    await until(lambda: up.hints())
    naps.open.clear()  # the deferred restart waits for the test

    def restarts() -> list[dict[str, Any]]:
        return [h for h in up.hints() if h.get("refresh") or h.get("quality", 60) != 60]

    for _ in range(5):
        await r.relay.hint(viewer, {"t": "hint", "refresh": True})
    await r.relay.hint(viewer, {"t": "hint", "quality": 40})
    await drain()
    assert restarts() == [{"t": "hint", "refresh": True}]
    assert naps.asked and 0 < naps.asked[-1] <= 1.0
    r.clock.t += 1.0
    naps.open.set()
    await until(lambda: len(restarts()) == 2)
    await drain()
    assert restarts()[1] == {"t": "hint", "fps": 5, "quality": 40, "paused": False}
    assert len(restarts()) == 2


# ── a control-stream door: the cursor and a watch without input ──────────────────────────────


async def test_the_cursor_reaches_panes_and_a_late_joiner_gets_its_shape_and_place(
    rig: Callable[..., Rig],
) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.opener.control = True
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, thumb = await r.viewer(BOB, "bob_tab_0001", pane=False)
    cursor_shape(up)
    cursor_move(up, 10, 20)
    up.frame(1)
    await until(lambda: ada.frames() == [1])
    assert cursors(ada) == ["shape", (10, 20)]
    assert cursors(thumb) == []  # a thumbnail draws no cursor
    _, late = await r.viewer(CY, "cy_tab_00001")
    await until(lambda: cursors(late) == ["shape", (10, 20)])


async def test_a_masked_socket_gets_no_cursor_until_the_mask_lifts(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.opener.control = True
    r.leases.hold(ADA, "ada_tab_0001", private=True)
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    cursor_shape(up)
    cursor_move(up, 7, 7)
    await until(lambda: cursors(ada) == ["shape", (7, 7)])
    await drain()
    assert cursors(bob) == []
    r.leases.current = None
    await r.relay.lease_changed(MID)
    await until(lambda: cursors(bob) == ["shape", (7, 7)])


async def test_a_reset_drops_the_cached_cursor(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    cursor_shape(up)
    up.say({"t": "reset"})
    up.frame(1)
    await until(lambda: ada.frames() == [1])
    _, late = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: late.frames() == [1])
    assert cursors(late) == []


async def test_a_control_stream_doors_watch_takes_no_input_from_anyone(rig: Callable[..., Rig]) -> None:
    """Behind a control-stream door nobody but the control stream has an input path - the lease
    holder's pointer and keys on the watch are dropped and counted, never sent up."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.opener.control = True
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await until(lambda: up.hints())
    await r.relay.input(ada1, CLICK)
    await drain()
    assert up.inputs() == [] and ada1.dropped == 1


async def test_a_control_stream_holder_is_paced_as_a_pane(rig: Callable[..., Rig]) -> None:
    """The holder's 30 fps and lower quality are for a watch that carries input (Docker, sandbar)."""
    up = FakeUpstream()
    r = rig((up, 1))
    r.opener.control = True
    r.leases.hold(ADA, "ada_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    await r.relay.hint(ada1, {"t": "hint", "fps": 30})
    await until(lambda: up.hints())
    assert ada1.pacer.fps == settings.computer_use.screen_fps_pane
    assert [h for h in up.hints() if "quality" in h][-1]["quality"] == settings.computer_use.screen_quality


async def test_a_pane_recovering_from_blind_gets_the_cursor_back(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.opener.control = True
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    cursor_shape(up)
    cursor_move(up, 1, 1)
    await until(lambda: cursors(ada) == ["shape", (1, 1)])
    r.leases.failing = True
    await r.relay.lease_changed(MID)
    cursor_shape(up)  # the shape changes while the pane is held back
    cursor_move(up, 5, 6)
    await drain()
    n = len(ada.got)
    r.leases.failing = False
    await r.relay.lease_changed(MID)
    await until(lambda: cursors(ada)[-2:] == ["shape", (5, 6)])
    assert len(ada.got) > n


class SlowSocket(FakeSocket):
    async def send_bytes(self, data: bytes) -> None:
        await asyncio.sleep(0.005)
        await super().send_bytes(data)


async def test_a_stream_of_cursor_moves_never_starves_the_frames(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.opener.control = True
    ada, _ = await r.viewer(ADA, "ada_tab_0001")
    slow = SlowSocket()
    ada.socket = slow
    for i in range(200):
        cursor_move(up, i, i)
        if i % 10 == 0:
            up.frame(i % 250 + 1)
        await asyncio.sleep(0.002)
    await until(lambda: slow.frames())
    assert slow.frames()


# ── a control-stream door: the holder's control stream ───────────────────────────────────────


def vnc_bytes(sock: FakeSocket) -> bytes:
    return b"".join(payload for h, payload in sock.got if h["t"] == "vnc" and payload)


def vnc_states(sock: FakeSocket) -> list[str]:
    return [h["state"] for h, _ in sock.got if h["t"] == "vnc" and "state" in h]


async def control_rig(rig: Callable[..., Rig], **kw: Any) -> tuple[Rig, FakeUpstream]:
    up = FakeUpstream()
    r = rig((up, 1), **kw)
    r.opener.control = True
    return r, up


async def test_the_holders_pane_gets_the_desktops_vnc_and_nobody_else_does(rig: Callable[..., Rig]) -> None:
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, bob = await r.viewer(BOB, "bob_tab_0001")
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    _, cy = await r.viewer(CY, "cy_tab_00001")
    _, bob_thumb = await r.viewer(BOB, "bob_tab_0001", pane=False)
    await until(lambda: r.controls.opened)
    r.controls.opened[0].down.put_nowait(b"RFB 003.008\n")
    await until(lambda: vnc_bytes(bob) == b"RFB 003.008\n")
    assert vnc_states(bob) == ["open"] and len(r.controls.opened) == 1
    for other in (ada, cy, bob_thumb):
        assert vnc_states(other) == [] and vnc_bytes(other) == b""
    await r.relay.control_input(bob1, b"RFB 003.008\n")
    await until(lambda: r.controls.opened[0].sent == [b"RFB 003.008\n"])


async def test_a_docker_machine_never_opens_a_control_stream(rig: Callable[..., Rig]) -> None:
    up = FakeUpstream()
    r = rig((up, 1))
    r.leases.hold(BOB, "bob_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    up.frame(1)
    await until(lambda: bob.frames() == [1])
    await drain()
    assert r.controls.opened == [] and vnc_states(bob) == []


async def test_someone_elses_vnc_bytes_never_reach_the_desktop(rig: Callable[..., Rig]) -> None:
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    await r.viewer(BOB, "bob_tab_0001")
    ada1, _ = await r.viewer(ADA, "ada_tab_0001")
    bob_other, _ = await r.viewer(BOB, "bob_tab_0002")  # the holder's second tab
    await until(lambda: r.controls.opened)
    await r.relay.control_input(ada1, b"\x05\x01\x00\x10\x00\x10")
    await r.relay.control_input(bob_other, b"\x05\x01\x00\x10\x00\x10")
    await drain()
    assert r.controls.opened[0].sent == [] and ada1.dropped == 1 and bob_other.dropped == 1


async def test_a_private_holders_control_opens_only_after_the_settle(rig: Callable[..., Rig]) -> None:
    """RFB input bypasses the per-message settle, so the stream itself waits until
    this relay has had the private lease for `mask_settle_ms` - every other socket is masked first."""
    naps = Naps()
    r, _ = await control_rig(rig, clock=Clock(step=0.0), naps=naps)
    r.leases.hold(BOB, "bob_tab_0001", private=True, age_s=5.0)
    await r.viewer(BOB, "bob_tab_0001")
    _, ada = await r.viewer(ADA, "ada_tab_0001")
    await drain()
    assert r.controls.opened == [] and masks(ada) == ["Bob"]
    r.clock.t += 0.4  # the settle has passed on this relay's clock
    await until(lambda: r.controls.opened)
    assert any(0 < s <= 0.31 for s in naps.asked)  # it waited out the settle, no longer


async def test_every_lease_ending_closes_the_control_stream(rig: Callable[..., Rig]) -> None:
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    r.leases.current = None  # a hand-back, an expiry, an owner's End: the row says nobody
    await r.relay.lease_changed(MID)
    await until(lambda: r.controls.opened[0].closed and vnc_states(bob) == ["open", "closed"])
    await r.relay.control_input(bob1, b"\x04\x01\x00\x00\x00\x00\x00\x61")
    await drain()
    assert r.controls.opened[0].sent == [] and len(r.controls.opened) == 1


async def test_a_reclaim_from_another_tab_moves_the_control_stream(rig: Callable[..., Rig]) -> None:
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    _, first = await r.viewer(BOB, "bob_tab_0001")
    _, second = await r.viewer(BOB, "bob_tab_0002")
    await until(lambda: r.controls.opened)
    r.leases.hold(BOB, "bob_tab_0002")
    await r.relay.lease_changed(MID)
    await until(lambda: len(r.controls.opened) == 2 and vnc_states(second) == ["open"])
    assert r.controls.opened[0].closed and vnc_states(first) == ["open", "closed"]


async def test_the_holders_socket_leaving_closes_the_control_stream(rig: Callable[..., Rig]) -> None:
    """The provider's grace (= `control_grace_s`) keeps the takeover for a reload; the relay only lets go."""
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, _ = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    await r.relay.detach(bob1)
    await until(lambda: r.controls.opened[0].closed and r.grace)
    assert r.grace == [(MID, BOB, "bob_tab_0001")]


async def test_a_control_stream_that_drops_under_a_live_lease_reopens(rig: Callable[..., Rig]) -> None:
    """The provider's takeover TTL, or a hand-back racing a re-claim, closes the VNC
    while the lease lives: a fresh takeover, a fresh RFB stream."""
    naps = Naps()
    r, _ = await control_rig(rig, naps=naps)
    r.leases.hold(BOB, "bob_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    r.controls.opened[0].down.put_nowait(None)  # the provider closed it
    await until(lambda: len(r.controls.opened) == 2)
    await until(lambda: vnc_states(bob) == ["open", "closed", "open"])  # the sender delivers it
    assert 0.2 in naps.asked


async def test_a_control_stream_that_drops_after_the_lease_ran_out_stays_closed(
    rig: Callable[..., Rig],
) -> None:
    """The lease-end job hands back a lease whose clock ran out, and the provider
    closes the VNC before the hub's `screen.state` reaches this relay: no takeover the lease no
    longer backs is opened again (the agent would meet `screen_busy`)."""
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    ends = utcnow() + dt.timedelta(milliseconds=300)
    r.leases.until(ends)
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    await until(lambda: utcnow() > ends)
    r.controls.opened[0].down.put_nowait(None)  # the provider closed it: the hand-back
    await until(lambda: vnc_states(bob) == ["open", "closed"])
    await drain()
    assert len(r.controls.opened) == 1


async def test_a_reconnect_on_the_same_connection_hands_the_stream_over(rig: Callable[..., Rig]) -> None:
    """After a silent network drop the dead socket lingers until its ping times
    out; the pane that reconnected on the same connection is the one the holder sees, so the
    stream moves to it at once (the newest pane wins) and the old socket hears `vnc{closed}`."""
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    _, dead = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    _, back = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: len(r.controls.opened) == 2 and vnc_states(back) == ["open"])
    await until(lambda: vnc_states(dead) == ["open", "closed"])
    assert r.controls.opened[0].closed
    r.controls.opened[1].down.put_nowait(b"RFB 003.008\n")
    await until(lambda: vnc_bytes(back) == b"RFB 003.008\n")
    assert vnc_bytes(dead) == b""


async def test_a_refused_takeover_backs_off_and_retries(rig: Callable[..., Rig]) -> None:
    naps = Naps()
    r, _ = await control_rig(rig, naps=naps)
    r.controls.refuse = True
    r.leases.hold(BOB, "bob_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: len([s for s in naps.asked if s in (0.2, 0.4, 0.8)]) >= 3)
    r.controls.refuse = False
    await until(lambda: r.controls.opened and vnc_states(bob) == ["open"])


async def test_a_holder_too_slow_for_vnc_is_closed_and_control_stops(
    rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFB is never dropped; a socket that cannot take it is cut off (4408) by the
    backlog itself - the write timeout is pushed out of reach, so only the backlog can close it."""
    monkeypatch.setattr(screen_fanout, "VNC_BACKLOG", 64)
    monkeypatch.setattr(settings.computer_use, "screen_write_timeout_s", 30)
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001", stalled=True)
    await until(lambda: r.controls.opened)
    for _ in range(4):
        r.controls.opened[0].down.put_nowait(b"x" * 40)
    await until(lambda: bob.closed_with is not None)
    assert bob.closed_with == (4408, "too slow")
    await until(lambda: r.controls.opened[0].closed)


async def test_a_holder_whose_bytes_pile_up_restarts_the_rfb_rather_than_drop_one(
    rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screen_control, "CONTROL_BACKLOG", 16)
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, _ = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    first = r.controls.opened[0]
    first.gate = asyncio.Event()  # the provider stalls: the first piece is in flight, the rest queue
    await until(lambda: r.controls.opened[0] is first)
    for _ in range(3):
        await r.relay.control_input(bob1, b"\x03" + b"\x00" * 9)
    await until(lambda: len(r.controls.opened) == 2 and first.closed)


async def test_vnc_key_and_pointer_events_renew_a_non_private_lease_and_update_requests_do_not(
    rig: Callable[..., Rig],
) -> None:
    """The silence clock: a holder who walks away still runs out."""
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: vnc_states(bob) == ["open"])
    await r.relay.control_input(bob1, b"\x03\x01\x00\x00\x00\x00\x05\x00\x03\x20")  # FramebufferUpdateRequest
    await drain()
    assert r.renewed == []
    await r.relay.control_input(bob1, b"\x05\x00\x00\x10\x00\x10")  # PointerEvent
    await until(lambda: r.renewed == [(MID, BOB, "bob_tab_0001")])


class SlowClosing(FakeControl):
    """A control stream whose close takes a while (a websocket's closing handshake)."""

    def __init__(self, close_s: float = 0.05) -> None:
        super().__init__()
        self.close_s = close_s

    async def close(self) -> None:
        await asyncio.sleep(self.close_s)
        await super().close()


async def test_a_fresh_claim_on_the_same_tab_closes_the_old_stream_before_the_new_one_opens(
    rig: Callable[..., Rig],
) -> None:
    """The socket hears `vnc{closed}` for the old stream before `vnc{open}` for the new one, and the
    new stream's bytes are never cleared by the old one's ending - however long that close takes."""
    r, _ = await control_rig(rig)
    r.controls.ready.append(SlowClosing())
    r.leases.hold(BOB, "bob_tab_0001")
    _, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    r.leases.hold(BOB, "bob_tab_0001", age_s=1.0)  # a fresh claim: another `since`
    await r.relay.lease_changed(MID)
    await until(lambda: len(r.controls.opened) == 2)
    r.controls.opened[1].down.put_nowait(b"RFB 003.008\n")
    await until(lambda: vnc_bytes(bob) == b"RFB 003.008\n" and r.controls.opened[0].closed)
    await drain()
    assert vnc_states(bob) == ["open", "closed", "open"] and vnc_bytes(bob) == b"RFB 003.008\n"


async def test_a_lease_that_ran_out_stops_the_holders_bytes_at_once(rig: Callable[..., Rig]) -> None:
    """Expiry is computed on read: the holder's next piece after `until` is dropped and the stream
    stops, without waiting for the next lease read."""
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, bob = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: vnc_states(bob) == ["open"])
    r.leases.until(utcnow() + dt.timedelta(milliseconds=200))
    await r.relay.lease_changed(MID)  # the cached copy runs out in 200 ms, and nothing reads it again
    await asyncio.sleep(0.25)
    assert vnc_states(bob) == ["open"]
    await r.relay.control_input(bob1, b"\x04\x01\x00\x00\x00\x00\x00\x61")
    await until(lambda: vnc_states(bob) == ["open", "closed"] and r.controls.opened[0].closed)
    assert r.controls.opened[0].sent == [] and bob1.dropped == 1 and len(r.controls.opened) == 1


async def test_close_machine_closes_the_control_stream_while_the_watch_is_in_backoff(
    rig: Callable[..., Rig],
) -> None:
    """A halt cancels the control's task once: its stream is still closed, and the provider's takeover
    never outlives the relay (fix round 1, I1)."""
    r, up = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    r.opener.then(NoScreen("asleep"))
    up.end()  # the watch drops, and every reopen finds the Machine cold: the pump naps
    await until(lambda: r.opener.calls >= 3)
    await r.relay.close_machine(MID, 4404, "reset")
    await until(lambda: r.controls.opened[0].closed)


async def test_the_linger_closing_an_unwatched_upstream_closes_the_control_stream(
    rig: Callable[..., Rig],
) -> None:
    r, _ = await control_rig(rig)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, _ = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    await r.relay.detach(bob1)
    await until(lambda: r.controls.opened[0].closed and r.relay.held(MID) is None)


async def test_a_backlog_restart_never_blocks_the_holders_socket_on_the_close(
    rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart's close runs beside the socket's loop: its input path never waits on the provider."""
    monkeypatch.setattr(screen_control, "CONTROL_BACKLOG", 16)
    r, _ = await control_rig(rig)
    first = SlowClosing(close_s=0.3)
    r.controls.ready.append(first)
    r.leases.hold(BOB, "bob_tab_0001")
    bob1, _ = await r.viewer(BOB, "bob_tab_0001")
    await until(lambda: r.controls.opened)
    first.gate = asyncio.Event()
    for _ in range(3):
        await asyncio.wait_for(r.relay.control_input(bob1, b"\x03" + b"\x00" * 9), timeout=0.1)
    await until(lambda: len(r.controls.opened) == 2 and first.closed)
