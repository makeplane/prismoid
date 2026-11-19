# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The event hub - ONE process-wide LISTEN, fanning out to per-task subscriber sets.

The naive design gives every connected client its own `LISTEN` connection. At 200 watchers that
is 200 Postgres backends doing nothing but waiting, and the connection pool dies before the
product does. So: **one** listener for the whole process, an in-memory fan-out to whoever is
subscribed to that task, and a `Queue` per subscriber so a slow client cannot stall a fast one.

The hub sits behind a small interface on purpose: when one process is no longer enough, a
dedicated pub/sub replaces the guts of this file and **not one emitter changes**.

Both lanes ride it. Lane 1 (durable) arrives via `pg_notify` from the committing transaction -
which means a subscriber can never see an event that a rollback then erased, because Postgres
delivers notifications on commit. Lane 2 (ephemeral deltas) publishes straight into the
fan-out, bounded at 4 KB/frame by the coalescer so it fits NOTIFY's payload ceiling.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from contextlib import suppress
from uuid import UUID

import asyncpg
import logfire
import orjson
from pydantic import ValidationError

from prismoid.api.serializers.conversations import MessageOut, message_out
from prismoid.conf import settings
from prismoid.db.models.event import NOTIFY_CHANNEL
from prismoid.db.session import session_scope
from prismoid.schemas.conversation import ChatLiveAdapter, ChatLiveEvent
from prismoid.schemas.events import DurableEventAdapter, LiveEventAdapter
from prismoid.schemas.events import TaskEvent as WireEvent

# A slow client is a slow client's problem. If a subscriber cannot keep up with this much backlog
# it is dropped rather than allowed to apply back-pressure to the listener - one stalled browser
# tab must not stop the flight recorder for everyone else.
QUEUE_DEPTH = 256

# How many live streams one viewer holds in this process: a person's open tabs, with room to spare.
# Each stream is a socket, a queue, a task and a re-proof query every interval on the shared API,
# and any account may open streams on its own task, so the count is capped, not trusted.
STREAMS_PER_VIEWER = 32


class StreamsFull(Exception):
    """The viewer already holds `STREAMS_PER_VIEWER` live streams in this process."""


class StreamSeats:
    """Live streams per viewer, counted where they register and given back where they unregister,
    so a stream that never started holds no seat. The doors read `full` to refuse early (429, or
    close 4429); `take` refuses again for the racer that got past the door."""

    def __init__(self, limit: int = STREAMS_PER_VIEWER) -> None:
        self.limit = limit
        self._held: Counter[UUID] = Counter()

    def full(self, viewer: UUID) -> bool:
        return self._held[viewer] >= self.limit

    def take(self, viewer: UUID) -> None:
        if self.full(viewer):
            raise StreamsFull(f"a viewer holds at most {self.limit} live streams")
        self._held[viewer] += 1

    def give(self, viewer: UUID) -> None:
        self._held[viewer] -= 1
        if self._held[viewer] <= 0:
            del self._held[viewer]


class Poison:
    """The pill pushed to a dropped subscriber's queue. A slow subscriber is discarded from the
    fan-out, but its generator is still parked on `queue.get()` - on a queue no one will ever `put`
    to again, so it hangs forever while the client keeps receiving heartbeats on a dead stream. We
    free one slot and hand it this sentinel instead: the parked `get()` returns it, the generator
    recognises it and breaks, and an EventSource client then reconnects cleanly. Public because the
    stream generator (the consumer) must recognise it - it is part of the queue contract."""

    __slots__ = ()


POISON = Poison()
# What a subscriber queue carries: a task's frames, a conversation's (its durable
# `message` rows and its chat.* deltas ride the same LISTEN, keyed by the conversation), or
# the one pill. The keys are distinct id namespaces, so a subscriber only ever sees its own.
ChatItem = ChatLiveEvent | MessageOut
QueueItem = WireEvent | ChatItem | Poison


def _raw_dsn() -> str:
    """asyncpg wants a libpq DSN; SQLAlchemy's URL carries a `+asyncpg` driver tag it cannot parse."""
    return settings.api.database_url.replace("postgresql+asyncpg://", "postgresql://")


class EventHub:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[QueueItem]]] = defaultdict(set)
        self.seats = StreamSeats()
        self._viewer_of: dict[asyncio.Queue[QueueItem], UUID] = {}
        self._connection: asyncpg.Connection | None = None  # pyright: ignore[reportMissingTypeArgument]
        self._started = False
        self._start_lock = asyncio.Lock()  # serialise start() so two registers cannot double-LISTEN
        self._fetch_tasks: set[asyncio.Task[None]] = set()  # strong refs - asyncio only keeps weak ones
        self._unparseable_types: set[str] = set()  # live frame types this process could not parse

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:  # re-check under the lock: another register() may have won the race
                return
            connection = await asyncpg.connect(_raw_dsn())
            await connection.add_listener(NOTIFY_CHANNEL, self._on_notify)
            self._connection = connection
            self._started = True
            logfire.info("event hub listening on {channel}", channel=NOTIFY_CHANNEL)

    async def stop(self) -> None:
        if self._connection is not None:
            with suppress(Exception):
                await self._connection.remove_listener(NOTIFY_CHANNEL, self._on_notify)
                await self._connection.close()
        self._connection = None
        self._started = False

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        """asyncpg calls this from the connection's reader task - it must not block or await."""
        try:
            body = orjson.loads(payload)
        except orjson.JSONDecodeError:
            logfire.warn("undecodable notify payload")
            return

        if "conversation_id" in body:
            # A conversation's frames: a chat.* delta publishes as it is; a
            # durable `message` arrives as a POINTER (the row is committed - NOTIFY fires on
            # commit) and is read back, exactly as an oversized task event is.
            conversation_id = UUID(body["conversation_id"])
            if not self._subscribers.get(conversation_id):
                return
            if body.get("lane") == "live":
                try:
                    chat = ChatLiveAdapter.validate_python(body)
                except ValidationError:
                    self._drop_unparseable(body)
                    return
                self._publish(conversation_id, chat)
            elif body.get("kind") == "message":
                fetch = asyncio.get_running_loop().create_task(
                    self._fetch_and_publish_message(conversation_id, int(body["seq"]))
                )
                self._fetch_tasks.add(fetch)
                fetch.add_done_callback(self._fetch_tasks.discard)
            return

        task_id = UUID(body["task_id"])
        if not self._subscribers.get(task_id):
            return  # nobody is watching this task; the record is already durable

        # **Lane 2 rides the same socket**, so this is the one place the lanes are told
        # apart - and it is one field. Boring beats clever: one hub, one connection, one thing to
        # operate. A dedicated pub/sub can replace it at scale without touching a single emitter.
        if body.get("lane") == "live":
            try:
                event = LiveEventAdapter.validate_python(body)
            except ValidationError:
                # A frame this process cannot parse - a type it does not know (a newer emitter
                # mid-deploy), or a known one that fails validation: drop it. Raising here would
                # surface inside asyncpg's NOTIFY callback, for every such frame.
                self._drop_unparseable(body)
                return
            self._publish(task_id, event)
            return

        if body.get("pointer"):
            # The event was too big for NOTIFY's 8000-byte ceiling. Read the row back - it is
            # already committed, because NOTIFY is delivered on commit. (A Lane-2 frame is never a
            # pointer: it is bounded to 4 KB by the coalescer, and a delta you have to go and fetch
            # is a delta that has stopped being a delta.)
            fetch = asyncio.get_running_loop().create_task(self._fetch_and_publish(task_id, body["seq"]))
            self._fetch_tasks.add(fetch)  # hold the ref so the loop cannot GC the task mid-flight
            fetch.add_done_callback(self._fetch_tasks.discard)
            return

        self._publish(task_id, DurableEventAdapter.validate_python(body))

    def _drop_unparseable(self, body: dict[str, object]) -> None:
        kind = str(body.get("type", "?"))
        if kind not in self._unparseable_types:  # log once per type per process - never the body
            self._unparseable_types.add(kind)
            logfire.warn("dropping an unparseable live frame {kind}", kind=kind)

    async def _fetch_and_publish(self, task_id: UUID, seq: int) -> None:
        from prismoid.db.models.event import TaskEvent

        # PLATFORM, deliberately: one LISTEN connection fans every workspace's frames out,
        # and this read is a by-id pointer follow - the subscribers were authorized (and bound)
        # at their own stream's door; nothing here decides visibility.
        async with session_scope(platform=True) as session:
            event = await TaskEvent.one(session, task_id, seq)
        if event is not None:
            self._publish(task_id, event)

    async def _fetch_and_publish_message(self, conversation_id: UUID, seq: int) -> None:
        from prismoid.db.models.message import Message

        async with session_scope(platform=True) as session:  # the same by-id pointer follow
            row = await Message.get(session, conversation_id, seq)
            if row is not None:
                self._publish(conversation_id, message_out(row))

    def _publish(self, task_id: UUID, event: WireEvent | ChatItem) -> None:
        for queue in tuple(self._subscribers.get(task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logfire.warn("subscriber too slow; dropping it", task_id=str(task_id))
                self._subscribers[task_id].discard(queue)
                # Wake the parked generator rather than leave it on a queue no one will feed again:
                # free one slot, then drop in the pill so it breaks and (for SSE) reconnects.
                with suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait(POISON)

    async def register(self, task_id: UUID, *, viewer: UUID | None = None) -> asyncio.Queue[QueueItem]:
        """Start receiving live events for a task, **right now**. A `viewer` takes one of their
        seats first (`StreamsFull` past the limit) and `unregister` gives it back.

        Deliberately *not* an async generator. A generator does not run a line of its body until
        the first `__anext__()`, so `subscribe()` would not actually register the queue until the
        caller started iterating - and a caller who registers, *then replays history, then*
        iterates would have a hole exactly one event wide between the end of the replay and the
        start of the live feed. That hole opens under precisely the load that makes it matter.

        Register first, replay second, drain third. The overlap is handled by the caller skipping
        seqs it has already yielded - duplicates are cheap, gaps are not.
        """
        if viewer is not None:
            self.seats.take(viewer)
        try:
            await self.start()
        except BaseException:
            if viewer is not None:
                self.seats.give(viewer)
            raise
        queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self._subscribers[task_id].add(queue)
        if viewer is not None:
            self._viewer_of[queue] = viewer
        return queue

    def unregister(self, task_id: UUID, queue: asyncio.Queue[QueueItem]) -> None:
        self._subscribers[task_id].discard(queue)
        if not self._subscribers[task_id]:
            self._subscribers.pop(task_id, None)
        viewer = self._viewer_of.pop(queue, None)
        if viewer is not None:
            self.seats.give(viewer)

    async def subscribe(self, task_id: UUID) -> AsyncIterator[WireEvent | ChatItem]:
        """Live events only, no replay. Prefer `register()` when history matters (it usually does)."""
        queue = await self.register(task_id)
        try:
            while True:
                event = await queue.get()
                if isinstance(event, Poison):
                    return  # dropped as a slow subscriber - end cleanly instead of hanging
                yield event
        finally:
            self.unregister(task_id, queue)

    def subscriber_count(self, task_id: UUID) -> int:
        return len(self._subscribers.get(task_id, ()))


hub = EventHub()
