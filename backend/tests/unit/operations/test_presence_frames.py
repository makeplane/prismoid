# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import asyncio
import json
import uuid

import pytest

from prismoid.api.hub import EventHub, QueueItem
from prismoid.db.base import utcnow
from prismoid.operations.presence import allow, clean_conn
from prismoid.schemas.events import (
    LIVE_TYPES,
    PRESENCE_TYPES,
    ActorRef,
    LiveEventAdapter,
    PresenceTyping,
    event_type_names,
    is_durable,
)
from prismoid.schemas.ids import ActorId, TaskId


def _typing() -> PresenceTyping:
    return PresenceTyping(
        task_id=TaskId(uuid.uuid4()),
        at=utcnow(),
        conn="abcd1234",
        actor=ActorRef(type="user", id=ActorId(uuid.uuid4()), name="Bob"),
        editor=True,
        on=True,
        mode="steer",
    )


def test_presence_rides_the_live_adapter_and_is_never_durable() -> None:
    ev = _typing()
    back = LiveEventAdapter.validate_json(ev.model_dump_json())
    assert back == ev and not is_durable(back)
    assert len(ev.model_dump_json()) < 4096
    assert PresenceTyping in PRESENCE_TYPES and PresenceTyping not in LIVE_TYPES


def test_presence_names_are_on_the_wire_list() -> None:
    names = event_type_names()
    for name in ("presence.join", "presence.leave", "presence.here", "presence.typing"):
        assert name in names


@pytest.mark.parametrize(
    "raw", [None, "", "short", "<script>alert(1)</script>", "x" * 5000, "a b c d e f g h"]
)
def test_a_hostile_or_missing_conn_is_replaced(raw: str | None) -> None:
    out = clean_conn(raw)
    assert out != raw and 8 <= len(out) <= 64 and out.replace("-", "").replace("_", "").isalnum()


def test_a_good_conn_is_kept() -> None:
    assert clean_conn("tab_7f3a9c21") == "tab_7f3a9c21"


def test_the_rate_limit_is_two_per_second_per_person_per_task() -> None:
    """Keyed on who and where, never on the caller-chosen `conn` - a key the caller picks
    is no cap at all."""
    ada, bob, task, other = (uuid.uuid4() for _ in range(4))
    assert [allow(ada, task) for _ in range(3)] == [True, True, False]
    assert allow(bob, task), "another person has their own budget"
    assert allow(ada, other), "and so does another task"


async def test_the_hub_drops_an_unparseable_live_frame_and_routes_presence() -> None:
    """An unparseable live frame (an unknown `type`, or a known one that fails validation) used
    to raise inside asyncpg's NOTIFY callback; now it is logged once per type and dropped, and
    presence routes by task_id like any task frame."""
    hub = EventHub()
    ev = _typing()
    queue: asyncio.Queue[QueueItem] = asyncio.Queue()
    hub._subscribers[ev.task_id].add(queue)  # pyright: ignore[reportPrivateUsage]
    unknown = {"type": "presence.wave", "lane": "live", "task_id": str(ev.task_id)}
    nobody = {**json.loads(ev.model_dump_json()), "type": "presence.here", "actor": None}
    for _ in range(2):
        hub._on_notify(None, 0, "c", json.dumps(unknown))  # pyright: ignore[reportPrivateUsage]
        hub._on_notify(None, 0, "c", json.dumps(nobody))  # pyright: ignore[reportPrivateUsage]
    dropped = hub._unparseable_types  # pyright: ignore[reportPrivateUsage]
    assert queue.empty() and dropped == {"presence.wave", "presence.here"}

    hub._on_notify(None, 0, "c", ev.model_dump_json())  # pyright: ignore[reportPrivateUsage]
    assert queue.get_nowait() == ev
