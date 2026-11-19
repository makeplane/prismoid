# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Presence (ADR-062): ephemeral frames on the task stream. Nothing here touches a table.

Who is on a task, right now: a stream says `join` when it opens, `here` on its reauth wake (and
once, debounced, when it sees a newcomer join), and `leave` when it closes; a composer says
`typing` through `POST /v1/tasks/{id}/presence`. Every frame is Lane 2 - no row, no seq, no
`id:` - and best-effort by construction: a frame that fails to send is dropped silently, because
presence is state, not content, and a failed send must neither cost a stream nor put a frame
into a log line (the payload names a person and a connection).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
import uuid
from collections import deque
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import VIEWERS_WATCH
from prismoid.db.models.task import Task
from prismoid.errors import Forbidden
from prismoid.operations import live, steering
from prismoid.operations.attribution import actor_ref
from prismoid.schemas.events import ActorRef, PresenceHere, PresenceJoin, PresenceLeave, PresenceTyping
from prismoid.schemas.ids import ProjectId, TaskId

_CONN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_WINDOW_S, _PER_WINDOW = 1.0, 2
_SWEEP_THRESHOLD = 4096
REANNOUNCE_S = 0.25  # a newcomer learns the room within this, not at the next 10 s wake
_hits: dict[tuple[uuid.UUID, uuid.UUID], deque[float]] = {}
_pending: set[asyncio.Task[None]] = set()  # fire-and-forget sends outlive a cancelled stream
_KINDS = {"join": PresenceJoin, "here": PresenceHere, "leave": PresenceLeave}

Kind = Literal["join", "here", "leave"]
Mode = Literal["follow_up", "steer", "answer"]


def clean_conn(raw: str | None) -> str:
    """The client's `?conn=` when it is a well-formed id, else a minted one - a hostile or
    oversized value never reaches a frame."""
    return raw if raw is not None and _CONN.fullmatch(raw) else f"c{secrets.token_hex(8)}"


def allow(actor_id: uuid.UUID, task_id: uuid.UUID) -> bool:
    """2 frames per second per person per task, per process - best effort; excess is dropped.

    Keyed on who and where, NEVER on the body's `conn`: a key the caller picks is no cap at all
    (a fresh conn per POST would buy a fresh budget). `operations/throttle.FixedWindowLimiter`
    is the house limiter, but its window is a fixed minute and its answer a 429; this door needs
    a one-second window whose excess is silently dropped, so it keeps its own deque -
    with throttle's eviction rule: past the floor, every key whose window went stale is swept.
    """
    now = time.monotonic()
    q = _hits.setdefault((actor_id, task_id), deque())
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    if len(q) >= _PER_WINDOW:
        return False
    q.append(now)
    if len(_hits) > _SWEEP_THRESHOLD:
        for stale in [k for k, hits in _hits.items() if not hits or now - hits[-1] > _WINDOW_S]:
            del _hits[stale]
    return True


async def _send(frame: PresenceJoin | PresenceHere | PresenceLeave | PresenceTyping) -> None:
    # Suppressed, never logged: a DB error's text can carry the NOTIFY parameters (the frame).
    with contextlib.suppress(Exception):
        await live.notify(frame)


async def announce(
    kind: Kind,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID | None,
    conn: str,
    actor: ActorRef,
    editor: bool,
) -> None:
    """One stream's edge or heartbeat. `actor` is the VALUE, captured while the stream's door
    session was open: an ORM row expires on the door's rollback and must not ride a day-long
    stream (or a detached send) anyway."""
    frame = _KINDS[kind](
        task_id=TaskId(task_id),
        project_id=ProjectId(project_id) if project_id else None,
        at=utcnow(),
        conn=conn,
        actor=actor,
        editor=editor,
    )
    await _send(frame)


def announce_detached(
    kind: Kind,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID | None,
    conn: str,
    actor: ActorRef,
    editor: bool,
) -> None:
    """For `leave` from a finally: a cancelled coroutine cannot await (a killed tab cancels the
    publisher), so the send runs as its own task, held until it lands."""
    t = asyncio.get_running_loop().create_task(
        announce(kind, task_id=task_id, project_id=project_id, conn=conn, actor=actor, editor=editor)
    )
    _pending.add(t)
    t.add_done_callback(_pending.discard)


class Watcher:
    """One open task stream's presence - the same shape for SSE and the WebSocket. Holds VALUES
    only (ids, the ActorRef, the editor bit), so it may outlive the door's session."""

    def __init__(
        self, *, task_id: uuid.UUID, project_id: uuid.UUID | None, conn: str, actor: ActorRef, editor: bool
    ) -> None:
        self.task_id = task_id
        self.project_id = project_id
        self.conn = conn
        self.actor = actor
        self.editor = editor  # read once at the door: a role change shows at the next connect
        self._reannounce: asyncio.Task[None] | None = None

    async def announce(self, kind: Kind) -> None:
        await announce(
            kind,
            task_id=self.task_id,
            project_id=self.project_id,
            conn=self.conn,
            actor=self.actor,
            editor=self.editor,
        )

    async def join(self) -> None:
        """The stream's first voice - sent once it is REGISTERED on the hub (`_events`'s
        `on_open`), so the room's debounced `here` replies can never beat the registration."""
        await self.announce("join")

    async def here(self) -> None:
        """The reauth wake's voice: still here, and still authorized (it follows a passing proof)."""
        await self.announce("here")

    def saw(self, event: object) -> None:
        """Another connection joined: answer once, debounced, so the newcomer learns the room."""
        if not isinstance(event, PresenceJoin) or event.conn == self.conn:
            return
        if self._reannounce is None or self._reannounce.done():
            self._reannounce = asyncio.ensure_future(self._here_later())

    async def _here_later(self) -> None:
        await asyncio.sleep(REANNOUNCE_S)
        await self.here()

    def close(self) -> None:
        """From the transport's finally - sync on purpose: a cancelled coroutine cannot await."""
        if self._reannounce is not None:
            self._reannounce.cancel()
        announce_detached(
            "leave",
            task_id=self.task_id,
            project_id=self.project_id,
            conn=self.conn,
            actor=self.actor,
            editor=self.editor,
        )


async def watcher(session: AsyncSession, task: Task, actor: Actor, *, conn: str | None) -> Watcher:
    """Who an opening FOLLOW stream is, read while its door session is still open - the
    transport rolls that session back (expiring both rows) before the long stream begins. A
    `follow=false` replay is a read, not a presence: its transport never calls this."""
    return Watcher(
        task_id=task.id,
        project_id=task.project_id,
        conn=clean_conn(conn),
        actor=actor_ref(actor),
        editor=await steering.may_edit(session, task, actor),
    )


async def typing(
    session: AsyncSession, actor: Actor, task_id: uuid.UUID, *, conn: str, on: bool, mode: Mode
) -> None:
    """A composer's keystroke, debounced by the client. Editor or above (viewers only watch);
    over the rate limit the frame is dropped and the answer is still 204."""
    task = await Task.get_for(session, task_id, actor)
    if not await steering.may_edit(session, task, actor):
        raise Forbidden(VIEWERS_WATCH)
    project_id, who = task.project_id, actor_ref(actor)  # read before the rollback expires both rows
    await session.rollback()  # no transaction held across the notify
    if not allow(who.id, task_id):
        return
    await _send(
        PresenceTyping(
            task_id=TaskId(task_id),
            project_id=ProjectId(project_id) if project_id else None,
            at=utcnow(),
            conn=conn,
            actor=who,
            editor=True,
            on=on,
            mode=mode,
        )
    )
