# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The relay's side of the screen's wire, as pure values: what of the sidecar's messages reaches a
browser (`frame_header`, `cursor_header`, `side_status`), what of a browser's input reaches the
sidecar (`clean_input`), and the fixed messages the relay sends on its own. Known kinds and
fields only, each of its type: a hostile client's or a confused sidecar's extra keys never travel."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Final, TypeGuard

from prismoid.schemas.screen import PASTE_MAX, WireError, encode

_POINTER_KINDS: Final = frozenset({"move", "down", "up", "wheel"})
_KEY_KINDS: Final = frozenset({"down", "up"})
_KEY_TEXT_MAX: Final = 32  # a key's `key`, `code` or `text` names one key; a paste is `text`
# The page's origin as Chromium states it: http(s), a host in canonical ASCII (an IDN in punycode,
# an IPv6 literal in brackets), a port. A path, credentials, a bidi control or a non-ASCII host -
# anything more, or anything a person could misread - never reaches a pane.
_ORIGIN_RE: Final = re.compile(r"https?://(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?|\[[0-9a-f:.]+\])(?::\d{1,5})?")
_ORIGIN_MAX: Final = 270  # a 253-character host, its scheme and a port

RESET: Final = encode({"t": "reset"})
RFB_INPUT: Final = frozenset({4, 5})  # KeyEvent, PointerEvent: a person at the keys
VNC_OPEN: Final = encode({"t": "vnc", "state": "open"})
VNC_CLOSED: Final = encode({"t": "vnc", "state": "closed"})
# Up to the sidecar when a connection stops driving: whatever its input holds down is let go.
RELEASE: Final = {"t": "release"}


def frame_header(header: Mapping[str, Any]) -> dict[str, Any]:
    """A frame's header as the browser hears it: the sidecar's known fields only."""
    return {"t": "frame", **{k: header[k] for k in ("seq", "ts", "w", "h", "fmt") if k in header}}


def cursor_header(header: Mapping[str, Any]) -> dict[str, Any]:
    """A cursor message as the browser hears it: its known whole-number fields only."""
    fields = ("serial", "x", "y", "w", "h", "hot_x", "hot_y")
    return {"t": "cursor", **{k: header[k] for k in fields if whole(header.get(k))}}


def side_status(header: Mapping[str, Any]) -> bytes:
    """The sidecar's `status{chromium, busy, origin, unreachable}` as the browser hears it: those
    fields only, and an origin - the page's, or the one it could not load - that is not one exactly
    is stated as none ("")."""
    out: dict[str, Any] = {"t": "status"}
    chromium, busy = header.get("chromium"), header.get("busy")
    if isinstance(chromium, str) and len(chromium) <= _KEY_TEXT_MAX:
        out["chromium"] = chromium
    if isinstance(busy, bool):
        out["busy"] = busy
    for key in ("origin", "unreachable"):
        if isinstance(origin := header.get(key), str):
            out[key] = origin if len(origin) <= _ORIGIN_MAX and _ORIGIN_RE.fullmatch(origin) else ""
    return encode(out)


def clean_input(header: Mapping[str, Any]) -> dict[str, Any] | None:
    """The input the sidecar is sent: known kinds and fields only, each of its type - a guard in
    front of the guest's own, so a hostile client's extra keys never travel - and only
    what the wire can carry (a paste of control characters escapes past HEADER_MAX)."""
    clean = _shape(header)
    if clean is None:
        return None
    try:
        encode(clean)
    except WireError:
        return None
    return clean


def _shape(header: Mapping[str, Any]) -> dict[str, Any] | None:
    kind, action = header.get("t"), header.get("type")
    if not isinstance(action, str):
        action = None  # a list or an object is no kind - and a set's `in` would raise on it
    if kind == "back":
        return {"t": "back"}  # it names no page: the sidecar goes to the one before
    if kind == "text":
        text = header.get("text")
        return {"t": "text", "text": text} if isinstance(text, str) and 0 < len(text) <= PASTE_MAX else None
    if kind == "pointer":
        x, y = header.get("x"), header.get("y")
        if action not in _POINTER_KINDS or not (_number(x) and _number(y)):
            return None
        fields = _fields(
            header, words=("button",), whole_=("buttons", "clicks", "modifiers"), numbers=("dx", "dy")
        )
        return {"t": "pointer", "type": action, "x": x, "y": y, **fields}
    if kind == "key" and action in _KEY_KINDS:
        fields = _fields(header, words=("key", "code", "text"), whole_=("modifiers", "keyCode"), numbers=())
        return {"t": "key", "type": action, **fields}
    return None


def _fields(
    header: Mapping[str, Any], *, words: tuple[str, ...], whole_: tuple[str, ...], numbers: tuple[str, ...]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in words:
        value = header.get(name)
        if isinstance(value, str) and len(value) <= _KEY_TEXT_MAX:
            out[name] = value
    for name in whole_:
        if whole(header.get(name)):
            out[name] = header[name]
    for name in numbers:
        if _number(header.get(name)):
            out[name] = header[name]
    return out


def whole(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> TypeGuard[int | float]:
    """A finite number: NaN and Infinity (which Python's JSON reads) are refused, never re-encoded."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def is_move(header: Mapping[str, Any]) -> bool:
    return header.get("t") == "pointer" and header.get("type") == "move"
