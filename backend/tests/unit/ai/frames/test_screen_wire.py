# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's wire: the screen's framing with the
payload's length `n` in every header, and the codec's bounds. Whatever a hostile or broken peer
sends is a WireError - never a crash, never an unbounded buffer."""

from __future__ import annotations

import json
import struct

import pytest

from prismoid.ai.frames.image_browser.sidecar import wire as image_wire
from prismoid.schemas.screen import (
    CONTROL_TYPES,
    DOWN_TYPES,
    HEADER_MAX,
    INPUT_TYPES,
    MESSAGE_MAX,
    PASTE_MAX,
    UP_TYPES,
    WIRE_VERSION,
    StreamDecoder,
    WireError,
    decode,
    encode,
)


def raw(head: bytes, payload: bytes = b"") -> bytes:
    """A message built by hand, so a test can say exactly what a peer put on the wire."""
    return struct.pack(">I", len(head)) + head + payload


# ── encode and decode ─────────────────────────────────────────────────────────────────────


def test_a_message_is_a_length_prefix_then_compact_json_then_the_payload() -> None:
    message = encode({"t": "frame", "seq": 7, "w": 1280, "h": 900, "fmt": "jpeg"}, b"\xff\xd8jpeg")
    head = b'{"t":"frame","seq":7,"w":1280,"h":900,"fmt":"jpeg","n":6}'
    assert message == struct.pack(">I", len(head)) + head + b"\xff\xd8jpeg"
    header, payload = decode(message)
    assert header == {"t": "frame", "seq": 7, "w": 1280, "h": 900, "fmt": "jpeg", "n": 6}
    assert payload == b"\xff\xd8jpeg"


def test_encode_sets_n_from_the_payload_and_overrides_a_callers_n() -> None:
    assert decode(encode({"t": "reset"})) == ({"t": "reset", "n": 0}, b"")
    header, payload = decode(encode({"t": "frame", "n": 99}, b"abc"))
    assert (header["n"], payload) == (3, b"abc")


def test_the_hello_and_the_type_sets() -> None:
    assert WIRE_VERSION == 1
    assert decode(encode({"t": "hello", "v": WIRE_VERSION}))[0] == {"t": "hello", "v": 1, "n": 0}
    assert INPUT_TYPES < UP_TYPES
    assert {"masked", "bye", "frame", "status", "reset"} < DOWN_TYPES
    assert not (INPUT_TYPES & DOWN_TYPES)
    assert image_wire.INPUT_TYPES == INPUT_TYPES, "the relay forwards what the sidecar takes"
    assert {"cursor", "vnc"} < DOWN_TYPES and {"vnc"} == CONTROL_TYPES and CONTROL_TYPES < UP_TYPES
    assert not (CONTROL_TYPES & INPUT_TYPES), "RFB is never parsed as the sidecar's input"


def test_the_header_is_utf8_so_a_full_paste_in_any_script_fits() -> None:
    """No ASCII escaping: `\\uXXXX` would make a 1,000-character paste 6,000 bytes, over the cap."""
    for char in ("é", "中", "😀"):  # two, three and four UTF-8 bytes
        message = encode({"t": "text", "text": char * PASTE_MAX})
        head = json.dumps(
            {"t": "text", "text": char * PASTE_MAX, "n": 0}, separators=(",", ":"), ensure_ascii=False
        )
        assert message[4:] == head.encode()
        assert decode(message)[0]["text"] == char * PASTE_MAX


def test_decode_takes_any_type_and_an_absent_n_as_no_payload() -> None:
    """Which types a direction takes is its reader's question (the relay, the sidecar)."""
    assert decode(raw(b'{"t":"nope"}')) == ({"t": "nope"}, b"")


def test_a_header_at_the_limit_is_fine_and_one_byte_over_is_not() -> None:
    def padded(size: int) -> bytes:
        stub = b'{"t":"status","n":0,"pad":""}'
        head = b'{"t":"status","n":0,"pad":"' + b"x" * (size - len(stub)) + b'"}'
        assert len(head) == size
        return head

    assert decode(raw(padded(HEADER_MAX)))[0]["t"] == "status"
    with pytest.raises(WireError):
        decode(raw(padded(HEADER_MAX + 1)))
    with pytest.raises(WireError):
        encode({"t": "status", "pad": "x" * HEADER_MAX})


def test_a_lone_surrogate_is_a_wire_error_never_a_unicode_error() -> None:
    """A peer's JSON may escape a lone surrogate (`"\\ud800"`), which decodes but has no UTF-8:
    encoding it again is the codec's refusal, so a caller that guards on WireError keeps its
    socket."""
    with pytest.raises(WireError):
        encode({"t": "text", "text": "\ud800x"})


def test_a_message_over_four_mebibytes_is_refused_both_ways() -> None:
    head = json.dumps({"t": "frame", "n": MESSAGE_MAX}, separators=(",", ":")).encode()
    with pytest.raises(WireError):
        decode(raw(head, b"\0" * MESSAGE_MAX))
    with pytest.raises(WireError):
        encode({"t": "frame"}, b"\0" * MESSAGE_MAX)
    # The largest payload that fits: its header is {"t":"frame","n":<7 digits>}.
    largest = MESSAGE_MAX - 4 - len(b'{"t":"frame","n":}') - len(str(MESSAGE_MAX))
    assert len(encode({"t": "frame"}, b"\0" * largest)) == MESSAGE_MAX
    assert len(decode(encode({"t": "frame"}, b"\0" * largest))[1]) == largest
    with pytest.raises(WireError):
        encode({"t": "frame"}, b"\0" * (largest + 1))


@pytest.mark.parametrize(
    "message",
    [
        b"",  # nothing
        b"\x00\x00",  # shorter than the length prefix
        struct.pack(">I", HEADER_MAX + 1),  # a header length over the limit, before any header
        raw(b'{"t":"reset","n":0}')[:-1],  # ends inside its header
        raw(b'{"t":"frame","n":5}', b"abc"),  # n says more than arrived
        raw(b'{"t":"frame","n":0}', b"abc"),  # n says less than arrived
        raw(b'{"t":"frame"}', b"abc"),  # no n, but a payload
        raw(b'{"t":"frame","n":-1}'),
        raw(b'{"t":"frame","n":true}'),
        raw(b'{"t":"frame","n":"3"}', b"abc"),
        raw(b"not json"),
        raw(b"\xff\xfe\x00"),  # not text at all
        raw(b"[1,2]"),  # JSON, not an object
        raw(b'{"n":0}'),  # no type
        raw(b'{"t":5,"n":0}'),  # a type that is not a string
    ],
)
def test_decode_refuses_a_malformed_message(message: bytes) -> None:
    with pytest.raises(WireError):
        decode(message)


@pytest.mark.parametrize(
    "head",
    [
        '{"t":"reset"}'.encode("utf-16"),  # UTF-16 behind its byte-order mark
        '{"t":"reset"}'.encode("utf-16-le"),  # ... and without one
        '{"t":"reset"}'.encode("utf-32"),
        b"\xef\xbb\xbf" + b'{"t":"reset"}',  # UTF-8 behind a byte-order mark
        b'{"t":"\xed\xa0\x80"}',  # a lone surrogate: never strict UTF-8
    ],
    ids=["utf16-bom", "utf16-le", "utf32", "utf8-bom", "surrogate"],
)
def test_a_header_that_is_not_strict_utf8_is_refused(head: bytes) -> None:
    """`json.loads` on bytes guesses UTF-16/32, skips a BOM and lets a surrogate through; the
    wire's headers are UTF-8, strictly, in `decode` and the stream decoder alike."""
    with pytest.raises(WireError):
        decode(raw(head))
    with pytest.raises(WireError):
        StreamDecoder().feed(raw(head))


# ── the stream decoder (byte streams: the exec's stdout, sandbar's pipe) ──────────────────


def test_many_messages_in_one_feed_come_out_in_order() -> None:
    messages = [
        encode({"t": "hello", "v": 1}),
        encode({"t": "frame", "seq": 1}, b"one"),
        encode({"t": "status", "chromium": "running", "busy": False}),
        encode({"t": "frame", "seq": 2}, b"two!"),
    ]
    out = StreamDecoder().feed(b"".join(messages))
    assert [h["t"] for h, _ in out] == ["hello", "frame", "status", "frame"]
    assert [p for _, p in out] == [b"", b"one", b"", b"two!"]


def test_a_message_split_anywhere_arrives_whole_once_complete() -> None:
    message = encode({"t": "frame", "seq": 9}, b"payload-bytes")
    for cut in range(1, len(message)):
        decoder = StreamDecoder()
        assert decoder.feed(message[:cut]) == []
        assert decoder.feed(message[cut:]) == [({"t": "frame", "seq": 9, "n": 13}, b"payload-bytes")]


def test_a_truncated_stream_waits_byte_by_byte_and_keeps_the_rest_for_the_next_message() -> None:
    first, second = encode({"t": "frame", "seq": 1}, b"ab"), encode({"t": "reset"})
    decoder = StreamDecoder()
    got: list[tuple[dict[str, object], bytes]] = []
    for byte in first + second[:3]:
        got += decoder.feed(bytes([byte]))
    assert got == [({"t": "frame", "seq": 1, "n": 2}, b"ab")]
    assert decoder.feed(second[3:]) == [({"t": "reset", "n": 0}, b"")]


def test_a_header_that_promises_too_much_is_refused_before_its_payload_is_buffered() -> None:
    decoder = StreamDecoder()
    head = json.dumps({"t": "frame", "n": MESSAGE_MAX}, separators=(",", ":")).encode()
    with pytest.raises(WireError):
        decoder.feed(raw(head))  # no payload sent: the promise alone is refused


@pytest.mark.parametrize(
    "bad",
    [struct.pack(">I", HEADER_MAX + 1) + b"{", raw(b"not json"), raw(b'{"t":"frame","n":-2}')],
)
def test_a_broken_stream_stays_broken(bad: bytes) -> None:
    decoder = StreamDecoder()
    with pytest.raises(WireError):
        decoder.feed(bad)
    with pytest.raises(WireError):
        decoder.feed(encode({"t": "reset"}))  # desynced for good: the owner closes it
