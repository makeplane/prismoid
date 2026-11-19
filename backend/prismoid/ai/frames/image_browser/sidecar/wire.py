# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/prismoid/ai/frames/image_browser/sidecar/wire.py
"""The screen's wire inside the image: the screen's framing, both directions.

    [u32 big-endian header_len][JSON header, at most HEADER_MAX bytes][payload]

The stdlib twin of `prismoid.schemas.screen`'s codec, which the image cannot import (the sidecar
is stdlib-only). `tests/unit/test_browser_sidecar_screen.py` holds the two to the same bytes for
the same input, and to the same refusals: change both or neither. Every header carries `n`, its
payload's byte count, and on a byte stream (the bridge's stdio, the daemon's socket) `n` is what
delimits a message."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final, cast

WIRE_VERSION: Final = 1
HEADER_MAX: Final = 4096
MESSAGE_MAX: Final = 4 * 1024 * 1024
PASTE_MAX: Final = 1000  # characters in one `text` message; the client splits a longer paste
INPUT_TYPES: Final = frozenset({"pointer", "key", "text", "back"})  # `back`: the page before, or its opener

_LEN: Final = 4


class WireError(ValueError):
    """A message this side refuses: a header over HEADER_MAX, a message over MESSAGE_MAX, a header
    that is not strict UTF-8 JSON naming an object with a string `t`, or a bad `n`."""


def encode(header: Mapping[str, Any], payload: bytes = b"") -> bytes:
    """One message. `n` is set here (in place if the caller set one, else last); the JSON is
    compact and keeps non-ASCII as UTF-8, so a 1,000-character paste fits HEADER_MAX."""
    raw = json.dumps({**header, "n": len(payload)}, separators=(",", ":"), ensure_ascii=False).encode()
    if len(raw) > HEADER_MAX:
        raise WireError(f"header is {len(raw)} bytes")
    if _LEN + len(raw) + len(payload) > MESSAGE_MAX:
        raise WireError("message too large")
    return len(raw).to_bytes(_LEN, "big") + raw + payload


def _header(raw: bytes) -> dict[str, Any]:
    """Strict UTF-8 first (`json.loads` on bytes would guess UTF-16/32, skip a BOM and pass a lone
    surrogate). A deeply nested header is a RecursionError on some Pythons: malformed, not a crash."""
    try:
        header = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # JSONDecodeError and UnicodeDecodeError alike
        raise WireError("header is not JSON") from e
    if not isinstance(header, dict) or not isinstance(header.get("t"), str):
        raise WireError("header is not an object with a type")
    fields = cast("dict[str, Any]", header)  # JSON object keys are always strings
    n = fields.get("n", 0)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise WireError("bad payload length")
    return fields


class StreamDecoder:
    """Whole messages out of a byte stream, in order, however the bytes were split. A refused
    message stays at the head of the buffer, so the decoder stays broken (as the host's does) and
    its owner closes the stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[dict[str, Any], bytes]]:
        self._buf += data
        out: list[tuple[dict[str, Any], bytes]] = []
        while len(self._buf) >= _LEN:
            size = int.from_bytes(self._buf[:_LEN], "big")
            if size > HEADER_MAX:
                raise WireError(f"header is {size} bytes")
            if len(self._buf) < _LEN + size:
                break
            header = _header(bytes(self._buf[_LEN : _LEN + size]))
            end = _LEN + size + int(header.get("n", 0))
            if end > MESSAGE_MAX:
                raise WireError("message too large")
            if len(self._buf) < end:
                break
            out.append((header, bytes(self._buf[_LEN + size : end])))
            del self._buf[:end]
        return out
