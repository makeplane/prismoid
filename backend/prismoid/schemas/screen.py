# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen (ADR-065): its wire codec, and the pure decisions of the
control lease and the relay's pacing.

**The wire** is one framing, on every hop - the sidecar's socket, the exec'd bridge's
stdio, sandbar's upgraded pipe and the browser's WebSocket:

    [u32 big-endian header_len][JSON header, at most HEADER_MAX bytes][payload]

One addition to the base framing: every header carries `n`, the payload's byte count (0 when
there is none). On a byte stream `n` is what delimits a message; on a WebSocket the message
boundary does too, and `decode` refuses a mismatch. The sidecar holds a stdlib twin of this
codec (`ai/frames/image_browser/sidecar/wire.py`), and its suite asserts the two encode equal
bytes: change one, change both.

**The lease** is six columns on `machines`. Its decisions take the lease as a value,
`now` as an argument (Postgres `now()` for the lease operations, the API's wall clock for the
relay's input gate) and the numbers as `LeaseLimits` - a schema never imports conf. The relay's
pacing takes a monotonic `now` in seconds. Nothing in this module does I/O or reads a clock:
the `schemas/machines.py` posture.
"""

from __future__ import annotations

import datetime as dt
import json
import struct
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, cast

# ── the wire ──────────────────────────────────────────────────────────────────────────────

WIRE_VERSION: Final = 1  # each side opens with {"t": "hello", "v": WIRE_VERSION}
HEADER_MAX: Final = 4096
MESSAGE_MAX: Final = 4 * 1024 * 1024
PASTE_MAX: Final = 1000  # characters in one `text` message; the web splits a longer paste
HANDOFF_REASON_MAX: Final = 200  # characters of the agent's `browser.handoff` reason
# The relay's `status{upstream}` for a Machine with no screen at all: a browser
# on a driver with no screen door (Docker and sandbar each have one). Unlike "starting", waiting
# never changes it. Its words are the claim's 409, the CLI's and the pane's.
NO_SCREEN: Final = "no_screen"
NO_SCREEN_TEXT: Final = "This Machine has no live screen yet."

# What each direction may carry. `masked`, `role` and `bye` are the relay's own, on the browser hop
# only: `role{driver}` tells a socket whether its connection is the one the lease lets drive.
# `cursor` (the provider's pointer: a move, or a shape whose PNG is the payload) and `vnc` (the lease
# holder's RFB through a control-stream door: `state: open|closed` bracket a stream, a payload is its
# bytes) are a control-stream door's.
DOWN_TYPES: Final = frozenset({"hello", "frame", "reset", "status", "masked", "role", "bye", "cursor", "vnc"})
UP_TYPES: Final = frozenset({"hello", "pointer", "key", "text", "back", "hint", "vnc"})
# Dropped host-side unless the sender drives. `back` takes the page to the one before (or a tab
# it opened back to its opener): a person's way out of a page the browser could not reach.
INPUT_TYPES: Final = frozenset({"pointer", "key", "text", "back"})
# The one up type that carries a payload: the holder's RFB bytes, for the control stream alone.
CONTROL_TYPES: Final = frozenset({"vnc"})

_LEN: Final = struct.Struct(">I")


class WireError(ValueError):
    """A message the codec refuses. The socket or stream that sent it is closed (1008 on the
    browser hop); the relay itself never falls over on one."""


def encode(header: Mapping[str, Any], payload: bytes = b"") -> bytes:
    """One message. `n` is set from the payload (a caller's own `n` is overwritten). The header
    is compact UTF-8 JSON - keys in the caller's order, `n` last unless the caller already had
    one, and no ASCII escaping, so a 1,000-character paste in any script stays under HEADER_MAX
    - built by exactly this `json.dumps` call, which the sidecar's twin repeats byte for byte.
    WireError past HEADER_MAX or MESSAGE_MAX: the producer skips an oversized frame, it never
    sends one a peer must refuse. WireError too for a lone surrogate, which a peer's JSON can
    escape into a decoded string but UTF-8 cannot carry back out."""
    try:
        head = json.dumps({**header, "n": len(payload)}, separators=(",", ":"), ensure_ascii=False).encode()
    except UnicodeEncodeError as exc:
        raise WireError("the header has a lone surrogate") from exc
    if len(head) > HEADER_MAX:
        raise WireError(f"the header is {len(head)} bytes, over {HEADER_MAX}")
    size = _LEN.size + len(head) + len(payload)
    if size > MESSAGE_MAX:
        raise WireError(f"the message is {size} bytes, over {MESSAGE_MAX}")
    return _LEN.pack(len(head)) + head + payload


def _header(raw: bytes) -> dict[str, Any]:
    """A header's JSON: an object with a string `t` and a non-negative integer `n` (0 when
    absent). Strict UTF-8 first: `json.loads` on bytes would guess UTF-16/32, skip a BOM and pass
    a lone surrogate, and the sidecar's twin must refuse exactly what this refuses.
    JSONDecodeError and UnicodeDecodeError are ValueErrors; a deeply nested header is a
    RecursionError - all of them are a peer's malformed message, never a crash."""
    try:
        header = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise WireError("the header is not JSON") from exc
    if not isinstance(header, dict):
        raise WireError("the header is not a JSON object")
    header = cast("dict[str, Any]", header)
    kind = header.get("t")
    n = header.get("n", 0)
    if not isinstance(kind, str):
        raise WireError("the header has no type")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise WireError("the header's n is not a byte count")
    return header


def decode(message: bytes) -> tuple[dict[str, Any], bytes]:
    """Exactly one whole message (a WebSocket's) → (header, payload). The type is not checked
    against a direction here: the relay and the sidecar each refuse what they do not take."""
    if len(message) > MESSAGE_MAX:
        raise WireError(f"the message is {len(message)} bytes, over {MESSAGE_MAX}")
    if len(message) < _LEN.size:
        raise WireError("the message is shorter than its length prefix")
    (head_len,) = _LEN.unpack_from(message)
    if head_len > HEADER_MAX:
        raise WireError(f"the header is {head_len} bytes, over {HEADER_MAX}")
    body = _LEN.size + head_len
    if body > len(message):
        raise WireError("the message ends inside its header")
    header = _header(message[_LEN.size : body])
    payload = message[body:]
    if header.get("n", 0) != len(payload):
        raise WireError("the header's n does not match the payload")
    return header, payload


class StreamDecoder:
    """Messages out of a byte stream (an exec's stdout, sandbar's upgraded pipe), in order,
    delimited by `n`. It buffers at most one message: a header is refused as soon as it says
    the message would pass MESSAGE_MAX, before its payload arrives. A WireError desyncs the
    stream for good, so the decoder stays broken and its owner closes the stream."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._head: dict[str, Any] | None = None  # the parsed header of a message still arriving
        self._head_len = 0
        self._broken = False

    def feed(self, data: bytes) -> list[tuple[dict[str, Any], bytes]]:
        if self._broken:
            raise WireError("the stream broke earlier")
        self._buf += data
        out: list[tuple[dict[str, Any], bytes]] = []
        pos = 0
        try:
            while True:
                if self._head is None:
                    if len(self._buf) - pos < _LEN.size:
                        break
                    (head_len,) = _LEN.unpack_from(self._buf, pos)
                    if head_len > HEADER_MAX:
                        raise WireError(f"the header is {head_len} bytes, over {HEADER_MAX}")
                    start = pos + _LEN.size
                    if len(self._buf) < start + head_len:
                        break
                    head = _header(bytes(self._buf[start : start + head_len]))
                    if _LEN.size + head_len + head.get("n", 0) > MESSAGE_MAX:
                        raise WireError(f"the message would pass {MESSAGE_MAX} bytes")
                    self._head, self._head_len = head, head_len
                body = pos + _LEN.size + self._head_len
                end = body + int(self._head.get("n", 0))
                if len(self._buf) < end:
                    break
                out.append((self._head, bytes(self._buf[body:end])))
                self._head = None
                pos = end
        except WireError:
            self._broken = True
            raise
        del self._buf[:pos]
        return out


# ── the control lease ─────────────────────────────────────────────────────────────────────


class ControlEnd(StrEnum):
    """Why a lease ended: `machine.control_ended.reason`."""

    RELEASED = "released"  # the holder handed back
    EXPIRED = "expired"  # a non-private lease's minute with no input ran out
    GRACE = "grace"  # a private holder's socket closed and they did not come back in time
    CAP = "cap"  # a private lease reached its hard cap
    ENDED = "ended"  # the Project owner (on a personal task, its creator) ended it
    DEMOTED = "demoted"  # the holder is no longer an editor of the task
    DISABLED = "disabled"  # the kill switch
    LOST = "lost"  # the Machine slept, was reset or lost its frame under the lease


# A connection's id: the browser tab mints it, the screen ticket carries it, and the lease names
# it (`machines.control_conn`). Every `conn` validator uses this pattern.
CONN_PATTERN: Final = r"^[A-Za-z0-9_-]{8,64}$"


@dataclass(frozen=True, slots=True)
class LeaseLimits:
    """The lease's numbers, passed in (`ComputerUseSettings.control_*` and `mask_settle_ms`)."""

    silence_s: int
    private_cap_s: int
    grace_s: int
    renew_every_s: int
    settle_ms: int


@dataclass(frozen=True, slots=True)
class Lease:
    """The six `machines.control_*` columns, set. `until` is when the lease ends unless it is
    renewed; a private lease also has a hard `cap_at`, and its `until` never passes it."""

    actor_id: uuid.UUID
    conn: str
    private: bool
    since: dt.datetime
    until: dt.datetime
    cap_at: dt.datetime | None


def is_live(lease: Lease | None, now: dt.datetime) -> bool:
    """Expiry is computed on read, so a stale row never drives: `until == now` is
    already over."""
    return lease is not None and lease.until > now


def _private_window(now: dt.datetime, limits: LeaseLimits) -> tuple[dt.datetime, dt.datetime]:
    """(until, cap_at) of a private lease that starts its cap now: both at the cap."""
    cap = now + dt.timedelta(seconds=limits.private_cap_s)
    return cap, cap


def claim_window(
    *, private: bool, now: dt.datetime, limits: LeaseLimits
) -> tuple[dt.datetime, dt.datetime | None]:
    """A fresh claim's (until, cap_at): a minute of silence, or a private lease's 10-minute cap,
    which is its `until` too."""
    if private:
        return _private_window(now, limits)
    return now + dt.timedelta(seconds=limits.silence_s), None


def reclaim_until(lease: Lease, now: dt.datetime, limits: LeaseLimits) -> dt.datetime:
    """The holder re-claims their own live lease without switching Private (a reload rebinding
    the connection): a private lease's `until` is restored to its cap - never past it, and the
    cap never moves - and a non-private lease gets its minute back. Switching Private on or off
    is a fresh window from `claim_window` instead (`operations/machines/screen.claim`: switching it
    on is a fresh private claim from now, `since` included; switching it off keeps `since`)."""
    if lease.private and lease.cap_at is not None:
        return lease.cap_at
    return claim_window(private=lease.private, now=now, limits=limits)[0]


def renew_until(lease: Lease, now: dt.datetime, limits: LeaseLimits) -> dt.datetime | None:
    """The relay renews a non-private lease on its holder's input: a fresh minute, at most every
    `renew_every_s` (the last renewal was `until - silence_s`, so the edge is exact: at
    `renew_every_s` it renews, a microsecond before it does not). None means nothing to write:
    a private lease (silence never ends it), a lease that is already over, or a throttled one."""
    if lease.private or not is_live(lease, now):
        return None
    last = lease.until - dt.timedelta(seconds=limits.silence_s)
    if now - last < dt.timedelta(seconds=limits.renew_every_s):
        return None
    return now + dt.timedelta(seconds=limits.silence_s)


def extend_window(now: dt.datetime, limits: LeaseLimits) -> tuple[dt.datetime, dt.datetime]:
    """The private holder's Renew: (until, cap_at), both a fresh cap from now. An extend is the
    one thing that moves the cap, even after the old one has passed."""
    return _private_window(now, limits)


def grace_until(lease: Lease, now: dt.datetime, limits: LeaseLimits) -> dt.datetime:
    """The holder's socket closed: the lease ends within the grace unless they re-claim. Never
    later than it already would, so a private lease's grace never passes its cap."""
    return min(lease.until, now + dt.timedelta(seconds=limits.grace_s))


def reopen_until(lease: Lease, now: dt.datetime, limits: LeaseLimits) -> dt.datetime | None:
    """The holder's own connection came back (a socket that dropped and reconnected on the same
    connection): what the grace took is given back - a private lease's `until`
    to its cap (which never moves), a non-private lease its minute. A private lease the grace never
    shortened (`until == cap_at`), a non-private one with more than the grace left (the grace
    leaves at most `grace_s`), or a lease already over is left alone: None, nothing to write."""
    if not is_live(lease, now):
        return None
    if lease.private:
        if lease.cap_at is None or lease.until >= lease.cap_at:
            return None
        return lease.cap_at
    if lease.until > now + dt.timedelta(seconds=limits.grace_s):
        return None
    until = max(lease.until, now + dt.timedelta(seconds=limits.silence_s))
    return until if until != lease.until else None


def held_seconds(lease: Lease, at: dt.datetime) -> int:
    """How long a lease ended at `at` was held (`machine.control_ended.held_s`): until `at`, or
    until its `until` when its clock had already run out - however late the ending was noticed
    (the lease-end job, a claim that beat it, a transition that cleared it). Never negative."""
    return max(0, int((min(at, lease.until) - lease.since).total_seconds()))


def end_reason(lease: Lease) -> ControlEnd:
    """Why an expired lease ended, read off its row: a private lease ran to its cap (`until ==
    cap_at`) or the socket-close grace cut it short; a non-private one ran out of silence."""
    if lease.private:
        return ControlEnd.CAP if lease.until == lease.cap_at else ControlEnd.GRACE
    return ControlEnd.EXPIRED


def may_drive(
    lease: Lease | None, *, actor_id: uuid.UUID, conn: str, now: dt.datetime, limits: LeaseLimits
) -> bool:
    """The relay's host-side input gate: input goes up only from the live
    lease's own actor AND connection (the holder's second tab is a viewer), and never inside a
    private lease's settle after its claim, so nothing typed lands before every relay masks.
    The settle counts from `since`, which a re-claim that switches Private on restarts
    (`operations/machines/screen.claim`), so that switch is settled too."""
    if lease is None or not is_live(lease, now) or lease.actor_id != actor_id or lease.conn != conn:
        return False
    return not (lease.private and now < lease.since + dt.timedelta(milliseconds=limits.settle_ms))


def held_text(seconds: int) -> str:
    """A held duration as the timeline and the resumed call say it: "45s", "1m 40s", "2h 3m"."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes}m {rest}s" if rest else f"{minutes}m"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


# ── the relay's pacing ────────────────────────────────────────────────────────────────────

# A frame up to 10% early still counts as on time, so transport jitter on an upstream paced at
# the viewer's own rate never halves what the viewer sees.
_GAP_SLACK: Final = 0.9


@dataclass(slots=True)
class Pacer:
    """One viewer's frame budget: at most `fps` frames a second, and a byte bucket `rate_bps`
    deep (one second), full at the start and refilled continuously. `admit` answers for the
    newest frame; one that does not fit is skipped, never queued (the one-slot buffer holds only
    the newest). One bigger than the whole bucket goes once the bucket is full, into debt: a dense
    page (a map, a photo) is shown slower, never not at all, and the average stays at `rate_bps`.
    `fps <= 0` is paused. The relay changes `fps` when a hint or the lease moves."""

    rate_bps: int
    fps: float
    _tokens: float | None = field(default=None, init=False, repr=False)
    _refilled_at: float = field(default=0.0, init=False, repr=False)
    _sent_at: float | None = field(default=None, init=False, repr=False)

    def admit(self, now: float, size: int) -> bool:
        if self.fps <= 0:
            return False
        if self._tokens is None:
            self._tokens = float(self.rate_bps)
        else:
            refill = (now - self._refilled_at) * self.rate_bps
            self._tokens = min(float(self.rate_bps), self._tokens + refill)
        self._refilled_at = now
        if self._sent_at is not None and now - self._sent_at < _GAP_SLACK / self.fps:
            return False
        if size > self._tokens and self._tokens < self.rate_bps:
            return False
        self._tokens -= size
        self._sent_at = now
        return True


def viewer_fps(*, pane: bool, holder: bool, fps_thumb: int, fps_pane: int, fps_holder: int) -> int:
    """The server's cap for one socket: the lease holder's own connection at `fps_holder`, an
    open pane at `fps_pane`, a thumbnail at `fps_thumb`. A client's hint may ask for less."""
    if holder:
        return fps_holder
    return fps_pane if pane else fps_thumb


def upstream_fps(rates: Iterable[int], *, default: int) -> int:
    """The rate the relay asks the sidecar for: the highest any connected, unpaused viewer is
    allowed, or `default` (the thumbnail rate) when there is none."""
    return max(rates, default=default)


# ── what the lease looks like to its readers ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ControlState:
    """A live lease as the agent's browser tools see it (`BrowserPorts.control`): who, private or
    not, since when."""

    holder_name: str
    private: bool
    since: dt.datetime


class ControlUnknown(Exception):
    """`BrowserPorts.control` could not read the lease. Unknown is not "nobody is driving": a
    person may be signing in privately, so the browser call is refused, never dispatched - and
    never parked either, since no lease would ever answer that gate."""


@dataclass(frozen=True, slots=True)
class LeaseView:
    """A live lease as the API and `screen.state` present it: the holder as a person."""

    holder_id: uuid.UUID
    holder_name: str
    private: bool
    since: dt.datetime
    until: dt.datetime
    cap_at: dt.datetime | None


__all__ = [
    "CONN_PATTERN",
    "CONTROL_TYPES",
    "DOWN_TYPES",
    "HANDOFF_REASON_MAX",
    "HEADER_MAX",
    "INPUT_TYPES",
    "MESSAGE_MAX",
    "NO_SCREEN",
    "NO_SCREEN_TEXT",
    "PASTE_MAX",
    "UP_TYPES",
    "WIRE_VERSION",
    "ControlEnd",
    "ControlState",
    "Lease",
    "LeaseLimits",
    "LeaseView",
    "Pacer",
    "StreamDecoder",
    "WireError",
    "claim_window",
    "decode",
    "encode",
    "end_reason",
    "extend_window",
    "grace_until",
    "held_seconds",
    "held_text",
    "is_live",
    "may_drive",
    "reclaim_until",
    "renew_until",
    "upstream_fps",
    "viewer_fps",
]
