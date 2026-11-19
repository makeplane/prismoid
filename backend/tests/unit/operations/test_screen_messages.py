# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the browser may send up the screen socket: binary only,
one whole message within the bounds, a known up kind, no payload. Anything else is a WireError,
which the socket answers with the generic policy close (1008) - never 4401, and never a crash in
the relay."""

from __future__ import annotations

import json
import struct
from typing import Any

import pytest

from prismoid.operations.screen import up_header, up_message
from prismoid.schemas.screen import HEADER_MAX, WireError, encode


def _receive(data: bytes | None = None, text: str | None = None) -> dict[str, Any]:
    return {"type": "websocket.receive", "bytes": data, "text": text}


def _framed(header: bytes, payload: bytes = b"") -> bytes:
    """A message built by hand, so it can lie about itself."""
    return struct.pack(">I", len(header)) + header + payload


def test_a_hint_an_input_and_a_hello_pass() -> None:
    assert up_header(_receive(encode({"t": "hint", "fps": 5})))["fps"] == 5
    move = up_header(_receive(encode({"t": "pointer", "type": "move", "x": 3, "y": 4})))
    assert (move["t"], move["type"], move["x"], move["y"]) == ("pointer", "move", 3, 4)
    assert up_header(_receive(encode({"t": "hello", "v": 1})))["v"] == 1
    assert up_header(_receive(encode({"t": "text", "text": "hunter2"})))["text"] == "hunter2"


FIVE_MIB = 5 * 1024 * 1024


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(_receive(text='{"t": "hint"}'), id="a text frame"),
        pytest.param(_receive(), id="neither bytes nor text"),
        pytest.param(
            _receive(_framed(json.dumps({"t": "hint", "n": FIVE_MIB}).encode(), b"x" * FIVE_MIB)),
            id="a 5 MiB message",
        ),
        pytest.param(
            _receive(_framed(json.dumps({"t": "hint", "pad": "x" * HEADER_MAX, "n": 0}).encode())),
            id="a header over 4096 bytes",
        ),
        pytest.param(_receive(_framed(b'{"t":"hint","n":5}', b"abc")), id="an n that disagrees"),
        pytest.param(_receive(_framed(b"not json")), id="a header that is not JSON"),
        pytest.param(_receive(b"\x00\x00\x00\x10{}"), id="shorter than its length prefix"),
        pytest.param(_receive(encode({"t": "teleport"})), id="an unknown kind"),
        pytest.param(_receive(encode({"t": "frame"})), id="a down kind sent up"),
        pytest.param(
            _receive(encode({"t": "pointer", "type": "move", "x": 1, "y": 1}, b"extra")), id="a payload"
        ),
    ],
)
def test_a_malformed_message_is_refused(message: dict[str, Any]) -> None:
    with pytest.raises(WireError):
        up_header(message)


def test_vnc_is_the_one_up_message_that_carries_a_payload() -> None:
    assert up_message({"bytes": encode({"t": "vnc"}, b"RFB 003.008\n")}) == (
        {"t": "vnc", "n": 12},
        b"RFB 003.008\n",
    )
    with pytest.raises(WireError):
        up_message({"bytes": encode({"t": "vnc"})})  # an empty piece
    with pytest.raises(WireError):
        up_message({"bytes": encode({"t": "pointer", "type": "move", "x": 1, "y": 1}, b"x")})
    with pytest.raises(WireError):
        up_message({"bytes": encode({"t": "vnc", "state": "open"})})
