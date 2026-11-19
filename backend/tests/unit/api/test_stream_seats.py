# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Each viewer holds a fixed number of live streams in one API process.

A live stream holds a socket, a queue, a task and a re-proof query every interval on the shared
API, and any account may open as many as it likes on its own task. So the hub seats each viewer
at most `STREAMS_PER_VIEWER` streams; past it the doors answer 429 (SSE) or close 4429 (the
WebSocket), and a stream that ends gives its seat back. No database: a full viewer is refused
before the hub ever listens.
"""

from __future__ import annotations

import uuid

import pytest

from prismoid.api.hub import STREAMS_PER_VIEWER, EventHub, StreamSeats, StreamsFull


def test_a_viewer_is_seated_up_to_the_limit_and_refused_past_it() -> None:
    seats = StreamSeats(limit=2)
    ada, bob = uuid.uuid4(), uuid.uuid4()
    seats.take(ada)
    seats.take(ada)
    assert seats.full(ada)
    with pytest.raises(StreamsFull):
        seats.take(ada)
    seats.take(bob)  # another viewer has their own seats
    assert not seats.full(bob)


def test_a_stream_that_ends_gives_its_seat_back() -> None:
    seats = StreamSeats(limit=1)
    ada = uuid.uuid4()
    seats.take(ada)
    seats.give(ada)
    assert not seats.full(ada)
    seats.take(ada)


def test_the_limit_leaves_room_for_a_person_with_many_tabs() -> None:
    assert 16 <= STREAMS_PER_VIEWER <= 64


async def test_the_hub_refuses_a_full_viewer_before_it_listens() -> None:
    hub = EventHub()
    ada = uuid.uuid4()
    for _ in range(STREAMS_PER_VIEWER):
        hub.seats.take(ada)
    with pytest.raises(StreamsFull):
        await hub.register(uuid.uuid4(), viewer=ada)  # no Postgres here: refused before start()
