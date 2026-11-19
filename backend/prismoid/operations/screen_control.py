# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen relay's control stream half (`ControlLane`, between `Fanout` and `Relay`): a driver's
VNC for the lease holder.

Where the Machine's door serves one, the live lease's own pane socket gets the desktop's VNC through
it once it may drive (a private lease's settle past), RFB both ways in `vnc` messages, in order and
never dropped; every other socket has no input path at all. Any ending, a re-claim elsewhere, the
socket leaving or the relay going blind stops it; a drop under the same lease reopens it. The
hand-back to the provider is the lease operations' (`after_end`), never the relay's."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import logfire

from prismoid.ai.frames.screen import ScreenClosed, ScreenStream
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.operations.machines import screen as machine_screen
from prismoid.operations.machines.screen import NoScreen
from prismoid.operations.screen_fanout import Fanout
from prismoid.operations.screen_state import (
    BACKOFF_MAX_S,
    BACKOFF_MIN_S,
    CONTROL_BACKLOG,
    PACE_MIN_S,
    ControlStream,
    Upstream,
    Viewer,
    close_stream,
    lease_key,
)
from prismoid.operations.screen_wire import RFB_INPUT, VNC_CLOSED, VNC_OPEN
from prismoid.schemas.screen import is_live


class ControlLane(Fanout):
    """The holder's control stream, opened, kept and stopped to match the lease and the sockets."""

    async def control_input(self, viewer: Viewer, payload: bytes) -> None:
        """The holder's RFB bytes up: only from the socket the control stream
        serves, while it is open and the cached lease still lets it drive (a lease that ran out
        stops the stream here, without waiting for the next read) - anyone else's are dropped and
        counted. Never dropped or rate-limited piece by piece (a dropped piece would corrupt the
        stream): bytes piling past CONTROL_BACKLOG restart the stream instead, and the RFB starts
        over. A non-private holder's key or pointer event renews the lease."""
        up = self._ups.get(viewer.machine_id)
        control = up.control if up is not None else None
        if (
            up is None
            or control is None
            or control.viewer is not viewer
            or control.stream is None
            or not payload
        ):
            viewer.dropped += 1
            return
        lease = up.held.lease if up.held is not None else None
        if not self._may_drive(up, viewer, lease):
            viewer.dropped += 1
            self._control_sync(up)
            return
        if control.queued + len(payload) > CONTROL_BACKLOG:
            # The close runs beside this socket's loop, never in it; until the link reopens, the
            # holder's bytes belong to the stream that is ending and are dropped.
            self._spawn(close_stream(control.stream), "restart a control stream")
            control.stream = None
            return
        control.outbox.append(payload)
        control.queued += len(payload)
        control.wake.set()
        if lease is not None and not lease.private and payload[0] in RFB_INPUT:
            self._maybe_renew(up, viewer)

    def _control_viewer(self, up: Upstream) -> Viewer | None:
        """The socket the holder's control stream serves: a pane of the live lease's own
        connection that may drive now (`_may_drive`: live, theirs, settled on both clocks, the
        relay not blind). A private lease still settling is looked at again when its settle ends
        (`_resync_after_settle`). Never a thumbnail or a glance (`pane` false). The NEWEST such pane
        wins: after a silent network drop the dead socket lingers until its ping times out, and the
        pane that reconnected on the same connection is the one the holder is looking at."""
        if not up.controllable or up.held is None:
            return None
        lease = up.held.lease
        if not is_live(lease, utcnow()):
            return None
        for viewer in reversed(up.viewers):
            if (
                viewer.pane
                and not viewer.closed
                and (viewer.actor_id, viewer.conn) == (lease.actor_id, lease.conn)
            ):
                if self._may_drive(up, viewer, lease):
                    return viewer
                if lease.private and not up.blind:
                    self._resync_after_settle(up)
                return None
        return None

    def _control_sync(self, up: Upstream) -> None:
        """Open, keep or stop the holder's control stream to match the lease and the sockets: a new
        holder, a fresh claim (another `since`), a re-claim from another tab, the socket leaving,
        the lease ending or the relay going blind each stop the stream that no longer fits. An
        upstream this relay no longer keeps (replaced, lingered out, closed) opens none."""
        viewer = self._control_viewer(up) if self._ups.get(up.machine_id) is up else None
        lease = up.held.lease if up.held is not None else None
        key = lease_key(lease) if viewer is not None and lease is not None else None
        current = up.control
        if current is not None and (viewer is not current.viewer or key != current.key):
            self._control_stop(up)
        if viewer is not None and key is not None and up.control is None:
            control = up.control = ControlStream(viewer=viewer, key=key)
            control.task = asyncio.create_task(self._control_link(up, control))
            self._hold(control.task)  # a stopped control's task finishes its close unawaited

    def _control_stop(self, up: Upstream) -> ControlStream | None:
        """Let the control stream go now: nothing more is sent up on it, its socket hears
        `vnc{closed}` at once - before any stream that replaces it says `vnc{open}` - and its task
        closes it on the way out. Returns it, for `_halt` to await."""
        control, up.control = up.control, None
        if control is None:
            return None
        control.stream = None
        if control.opened:
            control.opened = False
            self._vnc_restart(control.viewer, VNC_CLOSED)
        if control.task is not None:
            control.task.cancel()
        return control

    def _resync_after_settle(self, up: Upstream) -> None:
        """A private lease's holder may drive once its settle has passed on this relay's clock and
        the row's: look at the control stream again then (one waiter per upstream)."""
        if up.control_resync is not None and not up.control_resync.done():
            return
        up.control_resync = asyncio.create_task(self._settle_then_sync(up))

    async def _settle_then_sync(self, up: Upstream) -> None:
        """Sleep out what is left of the settle on both clocks (`_may_drive`), never less than
        PACE_MIN_S, so a settle that has not passed when looked at again is waited for, never spun
        on."""
        wait = PACE_MIN_S
        lease = up.held.lease if up.held is not None else None
        if lease is not None:
            here = up.settle_from + settings.computer_use.mask_settle_ms / 1000 - self._clock()
            settle = dt.timedelta(milliseconds=machine_screen.lease_limits().settle_ms)
            there = (lease.since + settle - utcnow()).total_seconds()
            wait = max(wait, here, there)
        await self._sleep(wait)
        up.control_resync = None
        if self._ups.get(up.machine_id) is up:
            self._control_sync(up)

    async def _control_link(self, up: Upstream, control: ControlStream) -> None:
        """Open the holder's control stream, relay it, and reopen it after a drop under the same
        lease (the provider's takeover TTL, a hand-back racing a re-claim), backing off 200 ms -> 5 s
        as the watch does - for as long as `_control_sync` keeps this control."""
        delay = BACKOFF_MIN_S
        while up.control is control:
            if await self._control_once(up, control):
                delay = BACKOFF_MIN_S
            if up.control is not control:
                return
            await self._sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
            # The lease is looked at again before every reopen: a lease whose clock ran out is
            # handed back by the lease-end job, and the provider closes the VNC before the hub's
            # `screen.state` reaches this relay - a reopen then would be a takeover no lease backs
            # (and an upstream this relay no longer keeps reopens nothing).
            self._control_sync(up)
            if up.control is not control:
                return

    async def _control_once(self, up: Upstream, control: ControlStream) -> bool:
        """One control stream, opened and relayed until it ends. True when it carried RFB down.
        However it ends, its writer is stopped and the stream closed."""
        stream: ScreenStream | None = None
        writer: asyncio.Task[None] | None = None
        carried = False
        try:
            stream = await self._open_control_shielded(up.machine_id)
            if up.control is not control:
                return carried
            control.stream = stream
            control.outbox.clear()
            control.queued = 0
            self._vnc_restart(control.viewer, VNC_OPEN)
            control.opened = True
            writer = asyncio.create_task(self._write_control(control, stream))
            while True:
                header, payload = await stream.recv()
                if header.get("t") == "vnc" and payload:
                    self._vnc_down(control.viewer, payload)
                    carried = True
        except (NoScreen, ScreenClosed):
            pass
        except Exception:
            logfire.exception("screen relay: the control stream failed", machine_id=str(up.machine_id))
        finally:
            # Everything before the await is synchronous, so no cancel can skip it: the close
            # runs on its own (`_spawn`), and the provider's takeover never outlives the relay.
            control.stream = None
            if writer is not None:
                writer.cancel()
            if stream is not None:
                self._spawn(close_stream(stream), "close a control stream")
            if control.opened:  # it ended on its own: `_control_stop` already said so otherwise
                control.opened = False
                self._vnc_restart(control.viewer, VNC_CLOSED)
            if writer is not None:
                await asyncio.gather(writer, return_exceptions=True)
        return carried

    async def _open_control_shielded(self, machine_id: uuid.UUID) -> ScreenStream:
        """`open_control`, shielded as the watch's open is: a stream an abandoned open hands back
        is closed, never leaked."""
        opening = asyncio.ensure_future(self._open_control(machine_id))
        self._hold(opening)
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            opening.add_done_callback(self._close_orphan_control)
            raise

    def _close_orphan_control(self, opening: asyncio.Future[ScreenStream]) -> None:
        if opening.cancelled() or opening.exception() is not None:
            return
        self._spawn(close_stream(opening.result()), "close an orphaned control stream")

    async def _write_control(self, control: ControlStream, stream: ScreenStream) -> None:
        """The control stream's one writer: the holder's RFB, in order. Any failure ends the
        stream, and its reader with it (the link reopens it)."""
        try:
            while True:
                while control.outbox:
                    payload = control.outbox.popleft()
                    control.queued -= len(payload)
                    await stream.send({"t": "vnc"}, payload)
                control.wake.clear()
                await control.wake.wait()
        except Exception:
            await close_stream(stream)
