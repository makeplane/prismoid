# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the screen relay keeps in memory: one `Viewer` per open socket, one `Upstream` per Machine
this process relays, and the lease holder's `ControlStream` (a driver's VNC) - plus the relay's
numbers and the collaborators it is handed. Nothing here holds a row or a transaction; the relay
(`operations/screen_relay.py`) is the only writer of these fields."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import math
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from prismoid.ai.frames.screen import ScreenStream
from prismoid.operations.machines.screen import HeldLease, Opened
from prismoid.schemas.screen import Lease, Pacer

BACKOFF_MIN_S: Final = 0.2
BACKOFF_MAX_S: Final = 5.0
PACE_MIN_S: Final = 0.02  # a paced sender looks at its slot again at least this far apart
CONTROL_DEPTH: Final = 64  # unsent control messages a viewer may hold before it is too slow
OUTBOX_DEPTH: Final = 256  # messages waiting for the sidecar before new ones are dropped
SOCKET_TIMEOUT_S: Final = 1.0  # a bye or a close the relay sends a socket it is letting go
STREAM_CLOSE_S: Final = 2.0  # an upstream's close, bounded
TOMBSTONE_S: Final = 3.0  # after `close_machine`, an attach on that Machine is refused this long
RESTART_MIN_S: Final = 1.0  # a refresh or a new quality restarts the screencast: this far apart at most
CLOSE_TOO_SLOW: Final = 4408
VNC_BACKLOG: Final = 8 * 1024 * 1024  # unsent RFB bytes a holder's socket may hold before it is too slow
CONTROL_BACKLOG: Final = (
    1024 * 1024
)  # the holder's RFB bytes waiting for the provider before the stream restarts

LeaseKey = tuple[uuid.UUID, dt.datetime]  # (holder, since): one lease, however often renewed


class ScreenSocket(Protocol):
    """What the relay needs of a socket (Starlette's WebSocket has exactly this)."""

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


OpenUpstream = Callable[[uuid.UUID], Awaitable[Opened]]
OpenControl = Callable[[uuid.UUID], Awaitable[ScreenStream]]
LeaseOf = Callable[[uuid.UUID], Awaitable[HeldLease | None]]
LeaseCall = Callable[[uuid.UUID, uuid.UUID, str], Awaitable[None]]  # renew, socket_closed
Sleep = Callable[[float], Awaitable[None]]


class TooSlow(Exception):
    """A write did not finish within `screen_write_timeout_s`."""


@dataclass(eq=False)
class Viewer:
    """One open screen socket: who, on which connection, watching which task's Machine - and the
    relay's bookkeeping for it. `pane` is the client's pane flag (`?pane=1`): it lifts the rate
    cap from the thumbnail's to the pane's."""

    socket: ScreenSocket
    actor_id: uuid.UUID
    conn: str
    task_id: uuid.UUID
    machine_id: uuid.UUID
    pane: bool = False
    masked_by: uuid.UUID | None = field(default=None, init=False)  # the private holder hiding it
    role: bool | None = field(default=None, init=False)  # last told: its connection drives
    held_back: bool = field(default=False, init=False)  # no frames while the lease is unknown (blind)
    paused: bool = field(default=False, init=False)
    asked_fps: int | None = field(default=None, init=False)
    asked_quality: int | None = field(default=None, init=False)
    dropped: int = field(default=0, init=False)  # input the gate refused (counted)
    closed: bool = field(default=False, init=False)
    slot: bytes | None = field(default=None, init=False, repr=False)
    cursor: bytes | None = field(default=None, init=False, repr=False)  # the newest unsent cursor move
    control: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    vnc: deque[bytes] = field(
        default_factory=deque, init=False, repr=False
    )  # RFB down, in order, never dropped
    vnc_bytes: int = field(default=0, init=False, repr=False)
    wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    pacer: Pacer = field(default_factory=lambda: Pacer(rate_bps=1, fps=1.0), init=False, repr=False)
    tokens: float = field(default=0.0, init=False, repr=False)
    refilled: float = field(default=0.0, init=False, repr=False)
    sender: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def masked(self) -> bool:
        return self.masked_by is not None


@dataclass(eq=False)
class ControlStream:
    """The lease holder's control stream (a driver's VNC): opened for ONE pane
    socket of the live lease's own connection while that socket may drive (`_control_viewer`);
    RFB both ways, in order, never dropped. `key` is the lease it serves: a fresh claim is a
    fresh stream."""

    viewer: Viewer
    key: LeaseKey
    task: asyncio.Task[None] | None = None
    stream: ScreenStream | None = None
    opened: bool = False  # its socket was told `vnc{open}` and not yet `vnc{closed}`
    outbox: deque[bytes] = field(default_factory=deque)
    queued: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(eq=False)
class Upstream:
    machine_id: uuid.UUID
    task_id: uuid.UUID | None = None  # the task whose Machine this is (the control routes' lookup)
    viewers: list[Viewer] = field(default_factory=list)
    stream: ScreenStream | None = None
    epoch: int | None = None
    status: str | None = None  # the relay's own word for the screen: live | asleep | starting | no_screen
    side_status: bytes | None = None  # the sidecar's last status, for late joiners
    last: bytes | None = None  # the last frame, encoded: memory only, never persisted
    last_private: bool = False  # taken under a private lease: the holder's alone
    controllable: bool = False  # the door serves the holder on a control stream: no input here
    cursor_shape: bytes | None = None  # the provider's cursor shape, encoded, for late joiners
    cursor_at: bytes | None = None  # ... and its newest place
    held: HeldLease | None = None  # the cached lease, as the row last said
    live_key: LeaseKey | None = None  # the lease last treated as live
    driver: tuple[uuid.UUID, str] | None = None  # the (actor, conn) that live lease lets drive
    mask: HeldLease | None = None  # the private lease hiding this screen
    settle_key: LeaseKey | None = None  # the private lease this relay saw last ...
    settle_from: float = 0.0  # ... and when it first saw it, on the relay's own clock
    hinted: tuple[int, int, bool] | None = None
    blind: bool = False  # the last lease read failed: no socket gets frames, no input is forwarded
    refresh_due: bool = False  # a fresh frame was asked for and has not gone up yet
    restarted_at: float = -math.inf  # the last restart sent up (a refresh, a new quality) ...
    restart_timer: asyncio.Task[None] | None = None  # ... and the one deferred until the window ends
    renewed_by: tuple[uuid.UUID, str] | None = None  # the holder (actor, conn) renewed last ...
    renewed_at: float = 0.0  # ... and when, on the relay's own clock
    outbox: deque[dict[str, Any]] = field(default_factory=deque)
    out_wake: asyncio.Event = field(default_factory=asyncio.Event)
    nudge: asyncio.Event = field(default_factory=asyncio.Event)
    lease_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pump: asyncio.Task[None] | None = None
    linger: asyncio.Task[None] | None = None
    control: ControlStream | None = None  # the holder's control stream, while one is wanted
    control_resync: asyncio.Task[None] | None = None  # a private lease's settle, waited out


def lease_key(lease: Lease) -> LeaseKey:
    return (lease.actor_id, lease.since)


async def close_stream(stream: ScreenStream) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(stream.close(), timeout=STREAM_CLOSE_S)
