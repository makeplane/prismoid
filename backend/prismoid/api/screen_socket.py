# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's WebSocket (ADR-065): its whole life, on this process's screen relay. The checks and
the verbs are the operation's (`operations/screen.py`: `admit`, `prove`, `settle_role`,
`on_message`, `on_event`, `reprove`); this module is the socket's speak - accept, the three clocks
it waits on, and `bye{reason}` with its close code. It reads `screen_ops.relay` at call time, so a
test that swaps the process's relay reaches the socket too."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any, Protocol

import logfire
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from prismoid.conf import settings
from prismoid.operations import screen as screen_ops
from prismoid.operations.screen import CLOSE_ERROR, Closing
from prismoid.operations.screen_state import SOCKET_TIMEOUT_S, Viewer
from prismoid.operations.tickets import ScreenClaims
from prismoid.schemas.screen import encode


class Feed(Protocol):
    """The API's event hub as the socket uses it (`api/hub.py`); the view hands it in."""

    async def register(self, task_id: uuid.UUID) -> asyncio.Queue[Any]: ...

    def unregister(self, task_id: uuid.UUID, queue: asyncio.Queue[Any]) -> None: ...


async def serve_socket(socket: WebSocket, task_id: uuid.UUID, ticket: str, *, feed: Feed) -> None:
    """`GET /v1/tasks/{id}/screen?ticket=st_…[&pane=1]`. The socket is accepted FIRST,
    so every refusal reaches the client as its close code (a close before accept is an HTTP 403 to
    a browser, and "off" would look like "bad ticket"); until every check has passed it hears
    nothing but `bye{reason}` and that code. In order: the ticket is peeked (signature, lifetime,
    unused, this task - 4401); the switch (4410); the ticket is redeemed (4401: a racing replay);
    the door (`prove`: 4401 or 4404). The hub feed is registered before the relay attaches, so no
    lease change can fall between the attach's read and the subscription. Then the loop, and on the
    way out the relay lets go (`detach`: a holder's bound connection starts the grace) and the
    socket hears `bye{reason}` and its close code."""
    await socket.accept()
    claims = screen_ops.admit(ticket, task_id)
    if isinstance(claims, int):
        await _bye(socket, screen_ops.refusal(claims))
        return
    standing = await screen_ops.prove(claims)
    if isinstance(standing, int):
        await _bye(socket, screen_ops.refusal(standing))
        return
    viewer = Viewer(
        socket=socket,
        actor_id=claims.actor,
        conn=claims.conn,
        task_id=task_id,
        machine_id=standing.machine_id,
        pane=socket.query_params.get("pane") == "1",
    )
    queue: asyncio.Queue[Any] | None = None
    closing: Closing | None = None
    relay_said_bye = False
    try:
        queue = await feed.register(task_id)
        await screen_ops.relay.attach(viewer)
        await screen_ops.settle_role(viewer, standing)
        closing = await _loop(socket, viewer, claims, queue)
    except WebSocketDisconnect:
        closing = None
    except Exception:
        logfire.exception("screen socket failed", task_id=str(task_id))
        closing = (CLOSE_ERROR, "error")
    finally:
        if queue is not None:
            feed.unregister(task_id, queue)
        # Read BEFORE `detach`, which closes the viewer too: closed already means the relay let go
        # of this socket itself (a reset, too slow, a tombstone, a dead send) and owns its goodbye.
        relay_said_bye = viewer.closed
        await screen_ops.relay.detach(viewer)
    if closing is not None and not relay_said_bye:
        await _bye(socket, closing)


async def _loop(
    socket: WebSocket, viewer: Viewer, claims: ScreenClaims, queue: asyncio.Queue[Any]
) -> Closing | None:
    """The socket's life on three clocks, the task stream's way (`api/stream._events`): pending
    reads are kept across wake-ups and never cancelled mid-race, and the proof is deadline-based, so
    a busy socket re-proves on the same cadence as a quiet one. None: the client went away, or the
    relay let go of the socket itself (a reset, too slow) and said its own goodbye."""
    loop = asyncio.get_running_loop()
    interval = settings.api.stream_reauth_seconds
    next_proof = loop.time() + interval
    inbound: asyncio.Future[Message] | None = None
    event: asyncio.Future[Any] | None = None
    try:
        while not viewer.closed:
            if inbound is None:
                inbound = asyncio.ensure_future(socket.receive())
            if event is None:
                event = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait(
                {inbound, event},
                timeout=max(0.05, next_proof - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if viewer.closed:
                return None
            if inbound in done:
                message, inbound = inbound.result(), None
                if message["type"] == "websocket.disconnect":
                    return None
                if (malformed := await screen_ops.on_message(viewer, message)) is not None:
                    return malformed
            if event in done:
                item, event = event.result(), None
                if (ending := await screen_ops.on_event(viewer, item)) is not None:
                    return ending
            if loop.time() >= next_proof:
                if (failed := await screen_ops.reprove(viewer, claims)) is not None:
                    return failed
                next_proof = loop.time() + interval
        return None
    finally:
        for pending in (inbound, event):
            if pending is not None:
                pending.cancel()


async def _bye(socket: WebSocket, closing: Closing) -> None:
    """`bye{reason}`, then the close code - best-effort and bounded: the client may be
    gone, or not reading. A socket the relay already closed is left alone."""
    code, reason = closing
    if socket.application_state != WebSocketState.CONNECTED:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(socket.send_bytes(encode({"t": "bye", "reason": reason})), SOCKET_TIMEOUT_S)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(socket.close(code=code, reason=reason), SOCKET_TIMEOUT_S)
