# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen relay: one upstream per Machine per API process, fanned out to every viewer without
re-encoding, the host-side input gate, and the privacy mask. `Relay` is built in three layers, a
file each: `Fanout` (what each socket is sent, who may drive), `ControlLane` (a driver's VNC for
the holder) and this one (the sockets' verbs, the upstream, the lease and the mask).

**The upstream** opens on a Machine's first viewer in this process and closes `screen_linger_s`
after its last one leaves. When it ends (a sleep, a wake on a new epoch, a sidecar restart, a send
it refused) the relay asks the row again (`open_upstream`): a warm Machine reopens, backing off
200 ms -> 5 s; a cold one tells its viewers `status{upstream: "asleep"}` and waits for a nudge
(`wake`, called on the task's `machine.*` events - never a proof's re-read) or the backoff. A
reset's `close_machine` tombstones the Machine briefly, so no attach strands a socket on it. A
bridge that finds no browser yet says `bye{no_browser}`: viewers hear "starting", and the backoff
keeps doubling until a frame proves the stream works, so a warm Machine without Chromium is polled,
never spun on. A Machine with no screen at all (`has_screen`: a driver with no
screen door) is `no_screen` to its viewers and is not polled: only a nudge asks again. A stream that
ends - its read or a send says `ScreenClosed` - is closed and never written to again; the open
itself is shielded, so a relay that lets go mid-open still closes what the open hands back.

**The input gate** forwards an input only when the cached lease lets THIS socket drive
(`_may_drive`), under a per-socket token bucket; pointer moves coalesce. A non-private holder's
input renews the lease, pre-throttled here per holder so the row is locked at most once per
`control_renew_every_s`. Behind a control-stream door the holder drives through that stream alone.

**The mask**: while a private lease is live, every socket but the holder's own connection gets
`masked{holder}` and no frames (a frame already waiting in its slot is dropped), and the cached
last frame is the holder's alone; when the mask lifts, that cache is dropped and the sidecar is
asked for a fresh frame (`hint{refresh}`).

The cached lease is a copy, re-read on every `screen.state` and `machine.*` event and at every
proof; expiry is computed on read. A read that fails leaves the relay blind, and
the mask fails CLOSED: until a read succeeds no socket gets a frame and nobody's input is forwarded
(the cached holder's neither: the cache is what the read could not confirm), and no mask is lifted. Every attach says the socket's mask state (`masked{holder}` or
`masked{holder: null}`), so a tab masked before it reconnected learns the mask is gone. The holder's
bound connection closing starts the lease's grace; the same connection attaching again gives it
back (`socket_reopened`). A refresh or a new quality restarts the sidecar's screencast, so those go
up at most once per `RESTART_MIN_S` per upstream; every ask inside the window becomes one deferred
restart. The relay holds no row and no transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Mapping
from typing import Any

import logfire

from prismoid.ai.frames.screen import ScreenClosed, ScreenStream
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.operations.machines.screen import HeldLease, NoScreen, Opened
from prismoid.operations.screen_control import ControlLane
from prismoid.operations.screen_state import (
    BACKOFF_MAX_S,
    BACKOFF_MIN_S,
    OUTBOX_DEPTH,
    RESTART_MIN_S,
    SOCKET_TIMEOUT_S,
    TOMBSTONE_S,
    Upstream,
    Viewer,
    close_stream,
    lease_key,
)
from prismoid.operations.screen_wire import RELEASE, RESET, clean_input, is_move, side_status, whole
from prismoid.schemas.screen import (
    INPUT_TYPES,
    NO_SCREEN,
    WIRE_VERSION,
    Lease,
    Pacer,
    WireError,
    encode,
    is_live,
    upstream_fps,
)


class Relay(ControlLane):
    """The process's screen relay. Every collaborator is injected so the unit suite runs it on
    fakes; the defaults are the real ones (`Fanout.__init__`)."""

    # ── the socket's verbs ────────────────────────────────────────────────────────────────

    async def attach(self, viewer: Viewer) -> None:
        """A socket joins: hello, the screen's state, its mask state (the holder hiding it, or
        `masked{holder: null}` - a tab masked before it reconnected must hear the mask is gone),
        and the last frame when this socket may see it. The lease is read BEFORE the viewer is
        registered, and from that read on nothing awaits, so no frame can reach a late joiner
        between the mask's decision and its registration. An upstream replaced during the read (a
        linger that ran out) is read again; one closed by `close_machine` meanwhile, or a Machine
        closed a moment ago, refuses this socket (its tombstone: `bye{reason}`, then its code) -
        `viewer.closed` tells the socket's handler. The lease's bound connection coming back while
        its grace runs lifts the grace (`socket_reopened`)."""
        while True:
            if (tomb := self._tombstone(viewer.machine_id)) is not None:
                viewer.closed = True
                await self._part(viewer, *tomb, encode({"t": "bye", "reason": tomb[1]}))
                return
            up = self._upstream(viewer.machine_id)
            up.task_id = viewer.task_id
            try:
                async with up.lease_lock:
                    self._apply(up, await self._lease_of(viewer.machine_id))
            except BaseException:
                self._maybe_linger(up)
                raise
            if self._ups.get(viewer.machine_id) is up:
                break
        cu = settings.computer_use
        viewer.pacer = Pacer(rate_bps=cu.screen_viewer_bps, fps=float(cu.screen_fps_thumb))
        viewer.tokens, viewer.refilled = float(cu.screen_input_per_s), self._clock()
        self._queue(viewer, encode({"t": "hello", "v": WIRE_VERSION}))
        if up.status is not None:
            self._queue(viewer, encode({"t": "status", "upstream": up.status}))
        if up.side_status is not None:
            self._queue(viewer, up.side_status)
        up.viewers.append(viewer)
        self._mask_one(up, viewer)
        if not viewer.masked:
            self._queue(viewer, encode({"t": "masked", "holder": None}))
        self._role_one(up, viewer)
        self._pace(up, viewer)
        self._give_cached(up, viewer)
        self._give_cursor(up, viewer)
        viewer.sender = asyncio.create_task(self._send_loop(viewer))
        self._hint_up(up)
        self._control_sync(up)
        if up.pump is None or up.pump.done():
            up.pump = asyncio.create_task(self._pump(up))
        if self._graced_holder(up, viewer):
            self._spawn(
                self._socket_reopened(viewer.machine_id, viewer.actor_id, viewer.conn), "socket_reopened"
            )

    async def detach(self, viewer: Viewer) -> None:
        """A socket leaves. When it was a pane of the lease holder's bound connection - and no other
        socket of that connection is still open here (a tab that reconnected) - the lease is
        shortened to the grace (`socket_closed`): a reload's re-claim inside it keeps the
        lease, and so does that connection coming back (`_grace`). A socket that is no pane (the
        CLI's `--shot` under its own claim) is a glance, never the holder walking away."""
        up = self._ups.get(viewer.machine_id)
        bound = self._remove(up, viewer) if up is not None and viewer in up.viewers else False
        self._retire(viewer)
        await self._sender_done(viewer)
        if bound and up is not None:
            self._up_send(up, dict(RELEASE))  # its last socket is gone, with whatever it held down
        if bound and viewer.pane:
            self._spawn(self._grace(viewer), "socket_closed")

    async def hint(self, viewer: Viewer, header: Mapping[str, Any]) -> None:
        """A client's `hint{fps, quality, paused, refresh}`: what it wants, inside what it is
        allowed. `refresh` asks the sidecar for one fresh frame, and spends input budget; it and a
        new quality restart the screencast, so they go up at most once a second (`_hint_up`)."""
        up = self._ups.get(viewer.machine_id)
        if header.get("t") != "hint" or up is None or viewer not in up.viewers:
            return
        fps, quality, paused = header.get("fps"), header.get("quality"), header.get("paused")
        if whole(fps) and fps > 0:
            viewer.asked_fps = fps
        if whole(quality) and 1 <= quality <= 100:
            viewer.asked_quality = quality
        if isinstance(paused, bool) and paused != viewer.paused:
            viewer.paused = paused
            if not paused:
                self._give_cached(up, viewer)
                self._give_cursor(up, viewer)
        self._pace(up, viewer)
        if header.get("refresh") is True and self._spend(viewer):
            up.refresh_due = True
        self._hint_up(up)

    async def input(self, viewer: Viewer, header: Mapping[str, Any]) -> None:
        """The host-side input gate. Forwarded only when the cached lease lets this
        socket drive and its bucket has a token left; anything else (no lease, another person, the
        holder's other connection, a private claim's settle, an unknown kind or field, a text over
        PASTE_MAX or one the wire cannot carry) is dropped and counted. Every input spends a token,
        and the bucket and the lease are checked before the message is parsed or encoded. A
        non-private holder's input renews the lease, at most every `control_renew_every_s`."""
        up = self._ups.get(viewer.machine_id)
        kind = header.get("t")
        if up is None or viewer not in up.viewers or not isinstance(kind, str) or kind not in INPUT_TYPES:
            return
        if up.controllable:
            viewer.dropped += 1  # a control-stream door: the holder drives through that stream alone
            return
        lease = up.held.lease if up.held is not None else None
        if not self._spend(viewer) or not self._may_drive(up, viewer, lease):
            viewer.dropped += 1  # rate and lease first: a flood or a non-holder costs no parse
            return
        if (clean := clean_input(header)) is None:
            viewer.dropped += 1
            return
        self._up_send(up, clean)
        if lease is not None and not lease.private:
            self._maybe_renew(up, viewer)

    async def wake(self, machine_id: uuid.UUID) -> None:
        """A `machine.*` event on the task's feed (created, woke, rebuilt, slept, control_*): the
        Machine's life moved, so an upstream that is down retries now instead of at the end of its
        backoff, and the lease is re-read. This is the ONLY nudge: a proof or `screen.state` calls
        `lease_changed`, which never cuts the backoff short - every viewer's 10 s proof would
        otherwise reopen a cold Machine N/10 times a second, past the 5 s ceiling."""
        up = self._ups.get(machine_id)
        if up is not None and up.stream is None:
            up.nudge.set()
        await self.lease_changed(machine_id)

    async def lease_changed(self, machine_id: uuid.UUID) -> None:
        """Re-read the lease and act on it: mask or unmask, the holder's rate, a refresh when a
        lease ended. Called on `screen.state`, at every proof, and by `wake`. It never nudges a
        down upstream (only `wake` does). A failed read fails CLOSED (`_blind`) until the next
        event or proof reads again."""
        up = self._ups.get(machine_id)
        if up is None:
            return
        try:
            async with up.lease_lock:
                self._apply(up, await self._lease_of(machine_id))
        except Exception:
            logfire.warning("screen relay: the lease re-read failed", machine_id=str(machine_id))
            self._blind(up)

    async def task_lease_changed(self, task_id: uuid.UUID) -> None:
        """`lease_changed` for the Machine of `task_id`, when this process relays its screen (the
        control routes' re-read: no database read of its own, so nothing there can fail open)."""
        for up in tuple(self._ups.values()):
            if up.task_id == task_id:
                await self.lease_changed(up.machine_id)

    def held(self, machine_id: uuid.UUID) -> HeldLease | None:
        """The cached lease of a Machine this process relays; None when it relays none."""
        up = self._ups.get(machine_id)
        return up.held if up is not None else None

    async def close_machine(self, machine_id: uuid.UUID, code: int, reason: str) -> None:
        """End every socket on this Machine in this process (`bye{reason}`, then `code`) and drop
        its upstream at once: after a reset no frame of the old incarnation is relayed again. Each
        sender is stopped first, so the bye is the last thing a socket hears. The Machine is
        tombstoned for `TOMBSTONE_S` (the relay's clock): an attach racing this close, or arriving
        just after it, is refused with the same `bye{reason}` and `code` instead of re-making an
        upstream no reset event will ever close."""
        now = self._clock()
        self._tombstones = {m: t for m, t in self._tombstones.items() if t[0] > now}
        self._tombstones[machine_id] = (now + TOMBSTONE_S, code, reason)
        up = self._ups.pop(machine_id, None)
        if up is None:
            return
        viewers, up.viewers = up.viewers, []
        for viewer in viewers:
            self._retire(viewer)
        bye = encode({"t": "bye", "reason": reason})
        await asyncio.gather(self._halt(up), *(self._part(viewer, code, reason, bye) for viewer in viewers))

    # ── the upstream ──────────────────────────────────────────────────────────────────────

    def _tombstone(self, machine_id: uuid.UUID) -> tuple[int, str] | None:
        """(code, reason) while `close_machine` closed this Machine less than TOMBSTONE_S ago."""
        tomb = self._tombstones.get(machine_id)
        if tomb is None:
            return None
        if self._clock() >= tomb[0]:
            del self._tombstones[machine_id]
            return None
        return tomb[1], tomb[2]

    def _upstream(self, machine_id: uuid.UUID) -> Upstream:
        up = self._ups.get(machine_id)
        if up is None:
            up = self._ups[machine_id] = Upstream(machine_id)
        if up.linger is not None:
            up.linger.cancel()  # someone came back inside the linger
            up.linger = None
        return up

    async def _pump(self, up: Upstream) -> None:
        """Open, relay, reopen - for as long as this upstream lives (the linger or `close_machine`
        cancels it). Every reopen waits out a nap first; the backoff resets only when a stream
        carried a frame, so one that opens and at once says `bye{no_browser}` keeps doubling it. A
        Machine with no screen at all (`NoScreen("no_browser")`: no row read changes that) waits
        for a nudge alone."""
        delay = BACKOFF_MIN_S
        while True:
            framed, screenless = False, False
            try:
                framed = await self._link(up)
            except NoScreen as no:
                cold = no.reason in ("asleep", "gone")
                if cold:  # a cold Machine shows no image: its last frame goes with it
                    up.last, up.last_private = None, False
                    up.cursor_shape = up.cursor_at = None
                screenless = no.reason == "no_browser"
                self._status(up, "asleep" if cold else NO_SCREEN if screenless else "starting")
            except ScreenClosed:  # the row says warm, but its frame did not answer: retry
                self._status(up, "starting")
            except Exception:
                logfire.exception("screen relay: the upstream failed", machine_id=str(up.machine_id))
            if framed:
                delay = BACKOFF_MIN_S
            await self._nap(up, None if screenless else delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    async def _link(self, up: Upstream) -> bool:
        """One upstream, opened and relayed until it ends. True when it carried a frame. However it
        ends, its writer is stopped and the stream closed: nothing is sent on it again."""
        opened = await self._open_shielded(up.machine_id)
        stream, epoch = opened.stream, opened.epoch
        framed = False
        writer: asyncio.Task[None] | None = None
        try:
            if up.epoch is not None and epoch != up.epoch:
                # A new incarnation (a wake, a rebuild): nothing of the old one is shown again.
                up.last, up.last_private, up.side_status = None, False, None
                up.cursor_shape = up.cursor_at = None
                self._broadcast(up, RESET)
            up.epoch, up.stream, up.hinted = epoch, stream, None
            up.controllable = opened.control
            self._control_sync(up)
            for viewer in up.viewers:  # a holder's rate and budget depend on what this door carries
                self._pace(up, viewer)
            up.refresh_due = False  # the sidecar restarts its screencast for a new connection anyway
            writer = asyncio.create_task(self._write_up(up, stream))
            self._up_send(up, {"t": "hello", "v": WIRE_VERSION})
            self._hint_up(up)
            while True:
                header, payload = await stream.recv()
                kind = header.get("t")
                if kind == "frame":
                    if not framed:
                        framed = True
                        self._status(up, "live")
                    self._frame(up, header, payload)
                elif kind == "status":
                    up.side_status = side_status(header)
                    self._broadcast(up, up.side_status)
                elif kind == "reset":  # Chromium restarted: the cached frame shows a page that is gone
                    up.last, up.last_private = None, False
                    up.cursor_shape = up.cursor_at = None
                    self._broadcast(up, RESET)
                elif kind == "cursor":
                    self._cursor(up, header, payload)
                elif kind == "bye":
                    if header.get("reason") == "no_browser":
                        self._status(up, "starting")
                    return framed
                # `hello`, and anything this relay does not know: ignored
        except ScreenClosed:
            return framed
        finally:
            up.stream = None
            up.outbox.clear()  # input for a stream that ended is moot; the next open hints afresh
            if writer is not None:
                writer.cancel()
            try:
                await close_stream(stream)
            finally:
                if writer is not None:
                    await asyncio.gather(writer, return_exceptions=True)

    async def _open_shielded(self, machine_id: uuid.UUID) -> Opened:
        """`open_upstream`, shielded: when the relay lets go mid-open (a linger, `close_machine`),
        the open runs on and whatever stream it hands back is closed, never leaked."""
        opening = asyncio.ensure_future(self._open(machine_id))
        self._hold(opening)
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            opening.add_done_callback(self._close_orphan)
            raise

    def _close_orphan(self, opening: asyncio.Future[Opened]) -> None:
        if opening.cancelled() or opening.exception() is not None:
            return  # nothing was opened (and the exception is retrieved)
        stream = opening.result().stream
        self._spawn(close_stream(stream), "close an orphaned upstream")

    async def _write_up(self, up: Upstream, stream: ScreenStream) -> None:
        """The upstream's one writer: the outbox, in order. A message the wire refuses is skipped
        (nothing of it was written); any other failure is the stream's end - it is closed, so its
        reader ends too, and this writer never sends on it again."""
        try:
            while True:
                await self._drain(up, stream)
                up.out_wake.clear()
                await up.out_wake.wait()
        except Exception:
            await close_stream(stream)

    async def _drain(self, up: Upstream, stream: ScreenStream) -> None:
        """Send what the outbox holds, in order, skipping a message the wire refuses."""
        while up.outbox:
            message = up.outbox.popleft()
            with contextlib.suppress(WireError):
                await stream.send(message)

    async def _nap(self, up: Upstream, delay: float | None) -> None:
        """Wait out the backoff, or less: a nudge (`wake`: a `machine.*` event) cuts it short.
        `None` waits for the nudge alone."""
        if not up.nudge.is_set():
            waits: set[asyncio.Future[Any]] = {asyncio.ensure_future(up.nudge.wait())}
            if delay is not None:
                waits.add(asyncio.ensure_future(self._sleep(delay)))
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
        up.nudge.clear()

    def _maybe_linger(self, up: Upstream) -> None:
        if not up.viewers and (up.linger is None or up.linger.done()) and self._ups.get(up.machine_id) is up:
            up.linger = asyncio.create_task(self._linger(up))

    async def _linger(self, up: Upstream) -> None:
        await self._sleep(settings.computer_use.screen_linger_s)
        if up.viewers or self._ups.get(up.machine_id) is not up:
            return
        del self._ups[up.machine_id]
        await self._halt(up)

    async def _halt(self, up: Upstream) -> None:
        control = self._control_stop(up)  # cancels its task, once: never again below
        for task in (up.linger, up.pump, up.restart_timer, up.control_resync):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if control is not None and control.task is not None and control.task is not asyncio.current_task():
            await asyncio.wait(
                {control.task}
            )  # waited for, not gathered: a cancelled halt never cancels it twice

    def _up_send(self, up: Upstream, header: dict[str, Any]) -> None:
        """Queue one message for the sidecar, in order. A pointer move replaces a move still waiting
        behind it, so only the latest move between sends goes up."""
        if up.stream is None:
            return  # nothing to send to: input is moot, and the next open sends its own hint
        if is_move(header) and up.outbox and is_move(up.outbox[-1]):
            up.outbox[-1] = header
        elif len(up.outbox) < OUTBOX_DEPTH:
            up.outbox.append(header)
        else:
            return  # the sidecar is not reading: drop rather than grow
        up.out_wake.set()

    def _hint_up(self, up: Upstream, *, due: bool = False) -> None:
        """Tell the sidecar the rate and quality its viewers want (`_wanted`); paused when no viewer
        here can see a frame.

        A new quality and a refresh (`refresh_due`) each restart the sidecar's screencast, so they
        go up at most once per `RESTART_MIN_S`: one inside the window
        waits for its end (`_restart_later`), the quality meanwhile staying what the sidecar has,
        and every ask until then is that one restart. A new quality's restart sends a fresh frame
        too, so a refresh due with it is not sent again. `due`: the deferred restart's own call."""
        watching = [v for v in up.viewers if not (v.paused or v.masked or v.closed or v.held_back)]
        fps, quality = self._wanted(up, watching)
        if up.stream is None:
            return
        restyle = up.hinted is not None and quality != up.hinted[1]
        restart = restyle or up.refresh_due
        if restart and not due and self._clock() - up.restarted_at < RESTART_MIN_S:
            self._restart_later(up)
            if up.hinted is not None:
                quality = up.hinted[1]  # the new quality waits for the window, with the refresh
            restyle = restart = False
        hint = (fps, quality, not watching)
        if hint != up.hinted:
            up.hinted = hint
            self._up_send(up, {"t": "hint", "fps": fps, "quality": quality, "paused": not watching})
        if restart:
            up.restarted_at = self._clock()
            if up.refresh_due and not restyle:
                self._up_send(up, {"t": "hint", "refresh": True})
            up.refresh_due = False

    def _wanted(self, up: Upstream, watching: list[Viewer]) -> tuple[int, int]:
        """The upstream's rate is the highest any watching viewer is allowed, its quality the
        highest any asks for up to `screen_quality` (`screen_holder_quality` while the lease
        holder's connection watches). Nobody watching keeps the quality the sidecar has."""
        cu = settings.computer_use
        fps = upstream_fps((self._fps(up, v) for v in watching), default=cu.screen_fps_thumb)
        cap = cu.screen_quality
        if any(self._drives_watch(up, v) for v in watching):
            cap = min(cap, cu.screen_holder_quality)  # driving: smaller frames, more of them
        asked = [v.asked_quality for v in watching if v.asked_quality is not None]
        quality = min(max(asked), cap) if asked else cap
        if not watching and up.hinted is not None:
            quality = up.hinted[1]  # nobody can see a frame: no restart is worth it
        return fps, quality

    def _restart_later(self, up: Upstream) -> None:
        """One deferred restart per upstream, at the end of the window: whatever is asked for
        meanwhile rides it."""
        if up.restart_timer is not None and not up.restart_timer.done():
            return
        wait = max(0.0, up.restarted_at + RESTART_MIN_S - self._clock())
        up.restart_timer = asyncio.create_task(self._restart_after(up, wait))

    async def _restart_after(self, up: Upstream, wait: float) -> None:
        await self._sleep(wait)
        up.restart_timer = None
        if self._ups.get(up.machine_id) is up:
            self._hint_up(up, due=True)

    # ── the lease and the mask ────────────────────────────────────────────────────────────

    def _apply(self, up: Upstream, held: HeldLease | None) -> None:
        """Act on a fresh read of the lease. `live_key` names the lease the relay last treated as
        live, so an ending (released, expired on read, ended, or replaced) is noticed exactly once.
        A read after a failed one ends the blindness (`_blind`): every viewer may get frames again,
        by the mask's rules, and a fresh frame is asked for."""
        recovered, up.blind = up.blind, False
        for viewer in up.viewers:
            viewer.held_back = False
        up.held = held
        live = held.lease if held is not None and is_live(held.lease, utcnow()) else None
        key = lease_key(live) if live is not None else None
        ended = up.live_key is not None and up.live_key != key
        up.live_key = key
        self._track_driver(up, live)
        lifted = self._track_mask(
            up, held if held is not None and live is not None and live.private else None
        )
        for viewer in tuple(up.viewers):
            self._mask_one(up, viewer)
            self._role_one(up, viewer)
            self._pace(up, viewer)
            if recovered:
                self._give_cursor(up, viewer)  # a shape that changed while blind was never queued
        if ended or lifted or recovered:
            up.refresh_due = True
        self._hint_up(up)
        self._control_sync(up)

    def _track_driver(self, up: Upstream, live: Lease | None) -> None:
        """The (actor, conn) the live lease lets drive; one that stopped driving is let go of."""
        driver = (live.actor_id, live.conn) if live is not None else None
        if up.driver is not None and up.driver != driver:
            # It stopped driving where its page may never hear it (an End, an expiry, a re-claim
            # from another tab): a key or a button it held must not stay down.
            self._up_send(up, dict(RELEASE))
        up.driver = driver

    def _track_mask(self, up: Upstream, mask: HeldLease | None) -> bool:
        """The private lease now hiding the screen, if any; True when the mask lifted or passed to
        another private lease (an ending, or Private switched off, which keeps the lease): the cache
        taken under it is dropped, and the caller asks the sidecar for a fresh frame - a still page
        sends none on its own. A private lease this relay has not seen before starts its own settle
        clock (`_may_drive`)."""
        if mask is not None and lease_key(mask.lease) != up.settle_key:
            up.settle_key, up.settle_from = lease_key(mask.lease), self._clock()
        if mask is not None and up.mask is None:
            up.last_private = up.last is not None  # from the claim on, the cache is the holder's
        lifted = up.mask is not None and (mask is None or lease_key(mask.lease) != lease_key(up.mask.lease))
        if lifted:
            up.last, up.last_private = None, False  # what was cached under the mask is dropped
        up.mask = mask
        return lifted

    def _blind(self, up: Upstream) -> None:
        """A lease read failed, and this relay no longer knows who may see the screen or drive it (a
        switch to Private, or an owner's End, may have just committed elsewhere): it fails CLOSED
        until a read succeeds. Every socket is held back and its waiting frame dropped - the cached
        holder's too, because the cached lease is exactly what the failed read could not confirm -
        and `_may_drive` forwards nobody's input, so every pane freezes for at most one proof
        interval. The cached frame becomes the holder's alone, by the next read's lease. No mask is
        lifted, and none is sent: each socket keeps the mask state it has."""
        up.blind = True
        if up.last is not None:
            up.last_private = True
        for viewer in up.viewers:
            viewer.held_back = True
            viewer.slot = None
            viewer.cursor = None
        self._hint_up(up)
        self._control_sync(up)

    def _mask_one(self, up: Upstream, viewer: Viewer) -> None:
        mask = up.mask
        hidden_by: uuid.UUID | None = None
        if mask is not None and (viewer.actor_id, viewer.conn) != (mask.lease.actor_id, mask.lease.conn):
            hidden_by = mask.lease.actor_id
        if hidden_by == viewer.masked_by:
            return
        viewer.masked_by = hidden_by
        if mask is not None and hidden_by is not None:
            viewer.slot = None  # a frame waiting for this socket is never sent behind the mask
            viewer.cursor = None
            holder = {"id": str(mask.holder.id), "name": mask.holder.name}
            self._queue(viewer, encode({"t": "masked", "holder": holder}))
        else:
            viewer.slot = None  # never anything left from behind the mask: only the cache's rules
            self._queue(viewer, encode({"t": "masked", "holder": None}))
            self._give_cached(up, viewer)
            self._give_cursor(up, viewer)

    def _role_one(self, up: Upstream, viewer: Viewer) -> None:
        """Tell a socket whether its connection drives - the live lease's bound (actor, conn) - when
        that changes: the page's "you are driving" is the server's word, never a guess from what
        the tab remembers (a re-claim from another tab, a duplicated tab, a lease that ended)."""
        driver = up.driver == (viewer.actor_id, viewer.conn)
        if viewer.role != driver:
            viewer.role = driver
            self._queue(viewer, encode({"t": "role", "driver": driver}))

    # ── a viewer leaving ──────────────────────────────────────────────────────────────────

    def _drop(self, viewer: Viewer, code: int, reason: str) -> None:
        """Give up on a socket (4408: too slow - no bye, it could not take one). It is unregistered
        at once, so nothing more is queued for it; the close runs in the background."""
        up = self._ups.get(viewer.machine_id)
        if up is None or viewer not in up.viewers:
            return  # already gone: detached, dropped, or closed with its Machine
        bound = self._remove(up, viewer)
        self._spawn(self._close_dropped(viewer, code, reason, bound), "evict")

    async def _close_dropped(self, viewer: Viewer, code: int, reason: str, bound: bool) -> None:
        await self._sender_done(viewer)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(viewer.socket.close(code, reason), timeout=SOCKET_TIMEOUT_S)
        if bound:
            await self._grace(viewer)

    def _remove(self, up: Upstream, viewer: Viewer) -> bool:
        """Unregister a viewer; True when it was the lease's bound connection and no other socket
        of that connection is still open here."""
        up.viewers.remove(viewer)
        self._retire(viewer)
        self._hint_up(up)
        self._maybe_linger(up)
        self._control_sync(up)
        lease = up.held.lease if up.held is not None else None
        mine = (viewer.actor_id, viewer.conn)
        return (
            lease is not None
            and (lease.actor_id, lease.conn) == mine
            and all((other.actor_id, other.conn) != mine for other in up.viewers)
        )
