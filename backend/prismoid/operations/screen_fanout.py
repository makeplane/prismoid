# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen relay's sockets half (`Fanout`, the base `Relay` is built on): what each viewer is
sent, and who the cached lease lets drive.

**Each viewer** has a one-slot buffer (the newest frame replaces an unsent one, and the upstream
never waits for a viewer), a `Pacer` (its rate cap and its byte budget), a sender task of its own,
and a write timeout that closes it 4408. Behind a control-stream door the cursor rides beside the
frames: its shape in order with the control messages and kept for late joiners, its newest place in
a one-slot buffer of its own, panes only. Control messages (hello, status, masked, reset) go ahead of frames, in
order; the holder's RFB goes after them, in order and never dropped.

**Who may drive** is read off the cached lease (`may_drive`: its actor and its connection, the
lease live, a private claim's settle past - counted from the lease's `since` AND from when this
relay first saw that private lease, on its own clock). Nobody drives while the relay is blind.

How a viewer leaves (`_drop`) and how the lease is re-read (`lease_changed`) belong to the relay
that registers viewers and keeps upstreams (`operations/screen_relay.py`)."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import logfire

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.operations.machines import screen as machine_screen
from prismoid.operations.screen_state import (
    CLOSE_TOO_SLOW,
    CONTROL_DEPTH,
    PACE_MIN_S,
    SOCKET_TIMEOUT_S,
    VNC_BACKLOG,
    LeaseCall,
    LeaseOf,
    OpenControl,
    OpenUpstream,
    Sleep,
    TooSlow,
    Upstream,
    Viewer,
    lease_key,
)
from prismoid.operations.screen_wire import cursor_header, frame_header
from prismoid.schemas.screen import (
    Lease,
    WireError,
    encode,
    is_live,
    may_drive,
    reopen_until,
    viewer_fps,
)


class Fanout(ABC):
    """The sockets half of the process's screen relay. Every collaborator is injected so the unit
    suite runs it on fakes; the defaults are the real ones."""

    def __init__(
        self,
        *,
        open_upstream: OpenUpstream = machine_screen.open_upstream,
        lease_of: LeaseOf = machine_screen.lease_of,
        renew: LeaseCall = machine_screen.renew,
        socket_closed: LeaseCall = machine_screen.socket_closed,
        socket_reopened: LeaseCall = machine_screen.socket_reopened,
        open_control: OpenControl = machine_screen.open_control,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._open = open_upstream
        self._lease_of = lease_of
        self._renew = renew
        self._socket_closed = socket_closed
        self._socket_reopened = socket_reopened
        self._open_control = open_control
        self._clock = clock
        self._sleep = sleep
        self._ups: dict[uuid.UUID, Upstream] = {}
        self._background: set[asyncio.Future[Any]] = set()
        self._tombstones: dict[uuid.UUID, tuple[float, int, str]] = {}  # deadline, code, reason

    @abstractmethod
    async def lease_changed(self, machine_id: uuid.UUID) -> None:
        """Re-read the lease and act on it (the relay's verb)."""

    @abstractmethod
    def _drop(self, viewer: Viewer, code: int, reason: str) -> None:
        """Give up on a socket (the relay unregisters it)."""

    # ── who may drive ─────────────────────────────────────────────────────────────────────

    def _may_drive(self, up: Upstream, viewer: Viewer, lease: Lease | None) -> bool:
        """`may_drive` on the cached lease (the database's clock: `since` + the settle), and for a
        private lease the settle again on this relay's own clock, from when it first saw that
        lease: `since` is backdated by the claim's pre-commit time, so the row's settle alone could
        be spent before this relay ever masked. Nobody drives while the relay is blind:
        the cached lease may be one an owner ended or a private claim replaced."""
        if up.blind:
            return False
        allowed = may_drive(
            lease,
            actor_id=viewer.actor_id,
            conn=viewer.conn,
            now=utcnow(),
            limits=machine_screen.lease_limits(),
        )
        if not allowed or lease is None or not lease.private:
            return allowed
        settle_s = settings.computer_use.mask_settle_ms / 1000
        return up.settle_key == lease_key(lease) and self._clock() - up.settle_from >= settle_s

    def _is_holder(self, up: Upstream, viewer: Viewer) -> bool:
        lease = up.held.lease if up.held is not None else None
        return (
            lease is not None
            and is_live(lease, utcnow())
            and (lease.actor_id, lease.conn) == (viewer.actor_id, viewer.conn)
        )

    def _drives_watch(self, up: Upstream, viewer: Viewer) -> bool:
        """The lease holder's own connection, on a watch that carries their input (Docker,
        sandbar): it gets the holder's rate, budget and quality. A holder behind a control-stream
        door drives through VNC and watches as a pane."""
        return not up.controllable and self._is_holder(up, viewer)

    def _graced_holder(self, up: Upstream, viewer: Viewer) -> bool:
        """This socket is the live lease's bound connection, and the grace has shortened the lease
        (`reopen_until`): its coming back gives the grace back."""
        lease = up.held.lease if up.held is not None else None
        return (
            lease is not None
            and (lease.actor_id, lease.conn) == (viewer.actor_id, viewer.conn)
            and reopen_until(lease, utcnow(), machine_screen.lease_limits()) is not None
        )

    async def _grace(self, viewer: Viewer) -> None:
        """The holder's bound connection closed: the grace starts (`socket_closed`). A socket of
        that same connection that attached meanwhile - a reconnect faster than the grace's write,
        so its attach found nothing to lift - has come back: the grace is lifted again."""
        await self._socket_closed(viewer.machine_id, viewer.actor_id, viewer.conn)
        up = self._ups.get(viewer.machine_id)
        mine = (viewer.actor_id, viewer.conn)
        if up is not None and any((v.actor_id, v.conn) == mine and not v.closed for v in up.viewers):
            await self._socket_reopened(viewer.machine_id, viewer.actor_id, viewer.conn)

    def _pace(self, up: Upstream, viewer: Viewer) -> None:
        """This socket's frame budget for its role: its rate cap, and the byte budget - the lease
        holder's own connection's while it drives, every other socket's otherwise."""
        cu = settings.computer_use
        viewer.pacer.fps = self._fps(up, viewer)
        viewer.pacer.rate_bps = (
            cu.screen_holder_bps if self._drives_watch(up, viewer) else cu.screen_viewer_bps
        )

    def _fps(self, up: Upstream, viewer: Viewer) -> int:
        cu = settings.computer_use
        allowed = viewer_fps(
            pane=viewer.pane,
            holder=self._drives_watch(up, viewer),
            fps_thumb=cu.screen_fps_thumb,
            fps_pane=cu.screen_fps_pane,
            fps_holder=cu.screen_fps_holder,
        )
        return min(viewer.asked_fps, allowed) if viewer.asked_fps else allowed

    def _maybe_renew(self, up: Upstream, viewer: Viewer) -> None:
        """Renew on the holder's input, pre-throttled here: at most once per
        `control_renew_every_s` per holder by the relay's own clock, so the row's lock is taken
        that often at most, never once per input (the row's `renew_until` throttles again)."""
        now = self._clock()
        who = (viewer.actor_id, viewer.conn)
        if up.renewed_by == who and now - up.renewed_at < settings.computer_use.control_renew_every_s:
            return
        up.renewed_by, up.renewed_at = who, now
        self._spawn(self._renew_then_reread(viewer), "renew")

    async def _renew_then_reread(self, viewer: Viewer) -> None:
        await self._renew(viewer.machine_id, viewer.actor_id, viewer.conn)
        await self.lease_changed(viewer.machine_id)  # the cached `until` moves with the row's

    def _spend(self, viewer: Viewer) -> bool:
        """One input from this socket's bucket: `screen_input_per_s` deep, refilled at that rate."""
        rate = float(settings.computer_use.screen_input_per_s)
        now = self._clock()
        viewer.tokens = min(rate, viewer.tokens + (now - viewer.refilled) * rate)
        viewer.refilled = now
        if viewer.tokens < 1.0:
            return False
        viewer.tokens -= 1.0
        return True

    # ── what each viewer is sent ──────────────────────────────────────────────────────────

    def _frame(self, up: Upstream, header: Mapping[str, Any], payload: bytes) -> None:
        try:
            data = encode(frame_header(header), payload)  # once, for every viewer: never re-encoded
        except WireError:
            return  # one the browser's wire cannot carry is skipped
        up.last, up.last_private = data, up.mask is not None or up.blind
        for viewer in up.viewers:
            if not (viewer.masked or viewer.paused or viewer.closed or viewer.held_back):
                viewer.slot = data  # the newest replaces an unsent one: the one-slot buffer
                viewer.wake.set()

    def _cursor(self, up: Upstream, header: Mapping[str, Any], payload: bytes) -> None:
        """The provider's pointer: a shape (its PNG the payload) goes to every pane
        that may see a frame, in order, and is kept for late joiners; a move replaces an unsent
        one. Never to a thumbnail, nor to a masked, paused, held-back or closed socket."""
        try:
            data = encode(cursor_header(header), payload)
        except WireError:
            return
        if payload:
            up.cursor_shape = data
        else:
            up.cursor_at = data
        for viewer in tuple(up.viewers):
            if not self._sees_cursor(viewer):
                continue
            if payload:
                self._queue(viewer, data)
            else:
                viewer.cursor = data
                viewer.wake.set()

    def _sees_cursor(self, viewer: Viewer) -> bool:
        return viewer.pane and not (viewer.masked or viewer.paused or viewer.closed or viewer.held_back)

    def _give_cursor(self, up: Upstream, viewer: Viewer) -> None:
        """A pane that may see the screen again (it joined, its mask lifted, it unpaused) gets the
        cursor's shape and its place."""
        if not self._sees_cursor(viewer):
            return
        if up.cursor_shape is not None:
            self._queue(viewer, up.cursor_shape)
        if up.cursor_at is not None:
            viewer.cursor = up.cursor_at
            viewer.wake.set()

    def _give_cached(self, up: Upstream, viewer: Viewer) -> None:
        if up.last is None or viewer.masked or viewer.paused or viewer.closed or viewer.held_back:
            return
        if up.last_private and not self._is_holder(up, viewer):
            return
        viewer.slot = up.last
        viewer.wake.set()

    def _broadcast(self, up: Upstream, data: bytes) -> None:
        for viewer in tuple(up.viewers):
            self._queue(viewer, data)

    def _queue(self, viewer: Viewer, data: bytes) -> None:
        """A control message, ahead of frames and in order; a viewer that lets them pile up is too slow."""
        if viewer.closed:
            return
        if len(viewer.control) >= CONTROL_DEPTH:
            self._drop(viewer, CLOSE_TOO_SLOW, "too slow")
            return
        viewer.control.append(data)
        viewer.wake.set()

    def _status(self, up: Upstream, word: str) -> None:
        if up.status != word:
            up.status = word
            self._broadcast(up, encode({"t": "status", "upstream": word}))

    def _vnc_restart(self, viewer: Viewer, message: bytes) -> None:
        """A stream began or ended: whatever the socket has not been sent of the old one is moot."""
        viewer.vnc.clear()
        viewer.vnc_bytes = 0
        self._queue(viewer, message)

    def _vnc_down(self, viewer: Viewer, payload: bytes) -> None:
        """RFB down to the holder's socket, in order. Never dropped: a socket that lets VNC_BACKLOG
        pile up is too slow (4408), and its leaving stops the control stream."""
        if viewer.closed:
            return
        data = encode({"t": "vnc"}, payload)
        if viewer.vnc_bytes + len(data) > VNC_BACKLOG:
            self._drop(viewer, CLOSE_TOO_SLOW, "too slow")
            return
        viewer.vnc.append(data)
        viewer.vnc_bytes += len(data)
        viewer.wake.set()

    async def _send_loop(self, viewer: Viewer) -> None:
        """This socket's writer: control first, then the holder's RFB (in order, never dropped),
        then the cursor's place, then the newest frame when the pacer admits it. A
        frame the budget cannot take yet waits in the slot, where a newer one replaces it (and the
        mask clears it); one larger than the whole budget waits for a full one (`Pacer`)."""
        after_cursor = False  # a cursor was just written: a waiting frame goes before the next one
        try:
            while not viewer.closed:
                after_cursor = await self._send_one(viewer, after_cursor)
        except TooSlow:
            self._drop(viewer, CLOSE_TOO_SLOW, "too slow")
        except Exception:
            viewer.closed = True  # the socket is gone; its handler's receive sees the end

    async def _send_one(self, viewer: Viewer, after_cursor: bool) -> bool:
        """One turn of `_send_loop`: write the next thing due, or wait for one. Returns whether a
        cursor was the last thing written."""
        if viewer.control:
            await self._write(viewer, viewer.control.popleft())
            return after_cursor
        if viewer.vnc:
            data = viewer.vnc.popleft()
            viewer.vnc_bytes -= len(data)
            await self._write(viewer, data)
            return after_cursor
        if (
            viewer.cursor is not None
            and not (viewer.masked or viewer.paused or viewer.held_back)
            and not (after_cursor and viewer.slot is not None)  # a frame gets its turn
        ):
            data, viewer.cursor = viewer.cursor, None
            await self._write(viewer, data)
            return True
        data = viewer.slot
        if data is None or viewer.masked or viewer.paused or viewer.held_back:
            viewer.wake.clear()
            await viewer.wake.wait()
            return False
        if not viewer.pacer.admit(self._clock(), len(data)):
            await self._wait_paced(viewer)
            return False
        viewer.slot = None
        await self._write(viewer, data)
        return False

    async def _wait_paced(self, viewer: Viewer) -> None:
        """The pacer said not yet: look again shortly, or sooner when something is queued."""
        viewer.wake.clear()  # a control message or a newer frame cuts the wait short
        wait = max(PACE_MIN_S, min(0.5, 0.5 / max(viewer.pacer.fps, 1.0)))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(viewer.wake.wait(), timeout=wait)

    async def _write(self, viewer: Viewer, data: bytes) -> None:
        try:
            await asyncio.wait_for(
                viewer.socket.send_bytes(data), timeout=settings.computer_use.screen_write_timeout_s
            )
        except TimeoutError:
            raise TooSlow from None

    async def _part(self, viewer: Viewer, code: int, reason: str, bye: bytes) -> None:
        """`close_machine`'s goodbye to one socket: its sender stopped, then `bye`, then the close."""
        await self._sender_done(viewer)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(viewer.socket.send_bytes(bye), timeout=SOCKET_TIMEOUT_S)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(viewer.socket.close(code, reason), timeout=SOCKET_TIMEOUT_S)

    def _retire(self, viewer: Viewer) -> None:
        viewer.closed = True
        viewer.slot = None
        viewer.cursor = None
        viewer.control.clear()
        viewer.vnc.clear()
        viewer.vnc_bytes = 0
        if viewer.sender is not None and viewer.sender is not asyncio.current_task():
            viewer.sender.cancel()

    async def _sender_done(self, viewer: Viewer) -> None:
        if viewer.sender is not None and viewer.sender is not asyncio.current_task():
            await asyncio.gather(viewer.sender, return_exceptions=True)

    def _spawn(self, work: Awaitable[None], what: str) -> None:
        """A detached lease call, eviction or close, held until it lands; a failure is logged,
        never raised into a socket's teardown."""

        async def run() -> None:
            try:
                await work
            except Exception:
                logfire.exception("screen relay: {what} failed", what=what)

        self._hold(asyncio.create_task(run()))

    def _hold(self, task: asyncio.Future[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)
