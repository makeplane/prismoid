# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The stream - WebSocket and SSE, **two framings of one generator**.

SSE is not a fallback. Every command in the product is already REST (resolve, steer, pause,
cancel), so **SSE + REST is a semantically complete client** - the WebSocket adds a duplex socket
preference, not a capability. For a browser, SSE is the *simpler correct choice*.

**The id rule - correctness, not style.** Only Lane-1 durable frames set `id: <seq>`. EventSource
reconnects with `Last-Event-ID: <last seen id>`, which the server treats exactly as `?since=<seq>`:
replay from the record, then live. If an ephemeral delta ever set an `id`, a reconnect after it
would silently *skip durable events*. Id-less deltas make the cursor move only on durable ground,
and the reconciliation rule (every delta stream terminates in a durable final or snapshot) then
guarantees nothing rendered is lost. Net effect: **the reconnect contract is implemented by the
browser itself** - zero client reconnect code.

**Deployment gotchas that silently kill SSE**, and the headers that answer them: flush per event ·
`Cache-Control: no-store` · `X-Accel-Buffering: no` (nginx-family buffering) · **no compression on
stream responses** · terminate at an HTTP/2-capable ingress (HTTP/1.1's six-connections-per-origin
limit is the one real SSE constraint; over h2 it vanishes). Written down in
`docs/runbooks/ingress.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

import logfire
from fastapi import Header, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from prismoid.api.deps import (
    ConversationStreamDep,
    SessionDep,
    TaskStreamDep,
    authenticate,
    bearer_header,
    credential_of,
    require_workspace,
    throttle_actor,
)
from prismoid.api.hub import ChatItem, Poison, QueueItem, StreamsFull, hub
from prismoid.api.serializers.conversations import MessageOut, message_out
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.event import REPLAY_PAGE, TaskEvent
from prismoid.db.models.message import Message
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden, NotFound, RateLimited, Unauthorized
from prismoid.operations import presence as presence_ops
from prismoid.operations import stream_tickets
from prismoid.schemas.conversation import CHAT_LIVE_CLASSES
from prismoid.schemas.events import TaskEvent as WireEvent
from prismoid.schemas.events import is_durable, redact_for
from prismoid.schemas.principal import Credential
from prismoid.schemas.stream_tickets import StreamSubject

HEARTBEAT_SECONDS = 15  # comment frames: they keep proxies alive without waking client handlers
RETRY_MS = 3000

STREAM_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",  # nginx buffers by default, and a buffered stream is not a stream
}


Prove = Callable[[AsyncSession, Actor], Awaitable[Task | Conversation]]


async def _still_authorized_for(
    credential: Credential, actor_id: UUID, prove: Prove, *, subject: str
) -> bool:
    """One re-proof for a live stream: the credential ROW that opened it still proves for the same
    actor AND the subject is still visible to it - `prove` is the subject's one fetch path. The
    stream holds the row's id, never the token, whether it opened with a header or a ticket.

    Covers every way a stream's standing can end mid-flight with one mechanism: password reset
    (which revokes sessions + API keys), logout, key revocation, session expiry crossing
    mid-stream, a project membership removed, the task deleted - and the WORKSPACE: the
    credential must still act in the subject's own workspace. A login session whose membership there
    was removed rebinds to another workspace at the door and would otherwise keep proving; the fence
    below severs it, as it severs a stream left behind by a switch. Fail-CLOSED on error: severing is
    cheap - the transport's reconnect contract replays from the cursor - while staying open on a
    hiccup would make the guard skippable exactly when the system is at its weakest.
    """
    try:
        async with session_scope() as session:
            # The door's checks by row id (stream_tickets.prove): the credential, the account's
            # standing (suspension severs within one re-proof) and the workspace membership (a removed
            # member's stream severs too), bound exactly as the door binds a request.
            principal = await stream_tickets.prove(session, credential, actor_id)
            if principal is None:
                return False
            subject_row = await prove(session, principal.actor)
            # The workspace fence: the credential acts in the subject's own workspace, or the stream severs.
            return subject_row.workspace_id == principal.workspace_id
    except (NotFound, Unauthorized, Forbidden):
        return False
    except Exception:
        logfire.warning("stream re-proof errored - severing", subject=subject)
        return False


async def _still_authorized(credential: Credential, actor_id: UUID, task_id: UUID) -> bool:
    return await _still_authorized_for(
        credential,
        actor_id,
        lambda session, actor: Task.get_for(session, task_id, actor),
        subject=str(task_id),
    )


async def _still_authorized_conversation(
    credential: Credential, actor_id: UUID, conversation_id: UUID
) -> bool:
    return await _still_authorized_for(
        credential,
        actor_id,
        lambda session, actor: Conversation.get_for(session, conversation_id, actor),
        subject=str(conversation_id),
    )


def _is_task_frame(item: QueueItem) -> bool:
    return not isinstance(item, (Poison, MessageOut, *CHAT_LIVE_CLASSES))


async def _events(
    task_id: UUID,
    since: int,
    follow: bool = True,
    revalidate: Callable[[], Awaitable[bool]] | None = None,
    workspace_id: UUID | None = None,
    on_open: Callable[[], Awaitable[None]] | None = None,
    on_proof: Callable[[], Awaitable[None]] | None = None,
    *,
    viewer: UUID | None = None,
) -> AsyncIterator[WireEvent]:
    """THE generator. Both transports frame exactly this.

    **Register, then replay, then drain** - in that order. Replay-then-subscribe has a hole
    exactly one event wide (anything committed between the last replayed row and the moment the
    listener attaches is simply gone), and it opens under precisely the load that makes it
    matter. Registering first turns that hole into a *duplicate*, and duplicates are free to
    discard: we track the highest seq we have yielded and skip anything at or below it.

    `follow=False` replays the record and **closes**. A live stream has no end, so without a
    finite mode `prismoid tasks events $ID` (no `--follow`) simply hangs - and so does any script
    that wants a task's history. Replay-and-close makes the flight recorder scriptable:

        curl -N -H "Authorization: Bearer $T" "$B/v1/tasks/$ID/events?since=0&follow=false"   # finite

    A browser never asks for it (EventSource would just reconnect), which is why the default is
    to follow.

    `revalidate` is the live lane's leash. A stream authenticates once at
    connect and can then outlive its credential - a password reset revokes the session row, but
    nothing about a row flips an open socket. Every `stream_reauth_seconds` the loop calls it;
    the first False ends the generator, both transports close, and the client's reconnect meets
    the door holding a dead credential (or tries to mint its next ticket with one, and is refused).
    Deadline-based, not timeout-based, so a busy stream re-proves on the same cadence as a quiet
    one - and the pending `queue.get()` is kept across timeouts, never cancelled mid-race, so no
    event can be lost to the check.

    Presence hangs off two moments here, and the transport passes both only on a
    follow stream. `on_open` runs right after the hub registration and before the replay: the
    stream says `presence.join` once it can already hear the room's debounced `here` replies,
    so no reply is lost to a slow register. `on_proof` rides the reauth wake: after each
    PASSING re-proof it says `presence.here`, so a watcher is present exactly as long as it is
    still authorized.
    """
    queue = await hub.register(task_id, viewer=viewer)  # StreamsFull past the viewer's seats
    pending: asyncio.Task[QueueItem] | None = None
    try:
        if on_open is not None:
            await on_open()
        # Replay is PAGED (REPLAY_PAGE per read), and each page gets its own short session: a
        # long-form task's record runs to thousands of frames, and the old single capped read
        # silently ended history at row 1000 for any late-joining client. The session closes
        # before the page is yielded so a slow consumer never holds a pooled connection.
        seen = since
        while True:
            async with session_scope(workspace_id) as session:
                page = await TaskEvent.replay(session, task_id, since=seen)
            for event in page:
                seen = event.seq
                yield event
            if len(page) < REPLAY_PAGE:
                break

        if not follow:
            return

        loop = asyncio.get_running_loop()
        interval = settings.api.stream_reauth_seconds
        next_proof = loop.time() + interval
        while True:
            if pending is None:
                pending = asyncio.ensure_future(queue.get())
            timeout = None if revalidate is None else max(0.05, next_proof - loop.time())
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            item: QueueItem | None = None
            if pending in done:
                item = pending.result()
                pending = None

            if revalidate is not None and loop.time() >= next_proof:
                if not await revalidate():
                    return  # severed: the credential (or the authorization) stopped proving
                next_proof = loop.time() + interval
                if on_proof is not None:
                    await on_proof()

            if item is None:
                continue
            if isinstance(item, Poison):
                return  # the hub dropped us as a slow subscriber - end so the client reconnects
            if not _is_task_frame(item):
                continue  # a conversation's frame on a task key cannot happen; never framed here
            event: WireEvent = item  # type: ignore[assignment]

            # **Lane 2 has no `seq`, and that is the design, not an omission**.
            #
            # A delta is ephemeral: it is never replayed, so it can never be *de*-duplicated against
            # the record, so it must never advance `Last-Event-ID` - a client that resumed from a
            # delta's id would ask the server to replay from a number no durable event ever had.
            # It carries `after_seq` instead: *"this belongs after that"*, which is all a client
            # needs to order it and all it can honestly be told.
            #
            # So the dedup below applies to Lane 1 only. Lane 2 passes straight through.
            if event.lane == "live":
                yield event
                continue

            if event.seq <= seen:
                continue  # already replayed - the register-first window, closing cleanly
            if event.seq > seen + 1:
                # A GAP: seqs are dense under the task row's lock, so the missing rows are
                # already on the record - two pointers fetched concurrently can arrive out
                # of order (found in review), and the lower one would be dropped as replayed.
                async with session_scope(workspace_id) as session:
                    gap = await TaskEvent.replay(session, task_id, since=seen)
                for earlier in gap:
                    if seen < earlier.seq < event.seq:
                        seen = earlier.seq
                        yield earlier
            seen = event.seq
            yield event
    finally:
        if pending is not None:
            pending.cancel()
        hub.unregister(task_id, queue)


async def _chat_events(
    conversation_id: UUID,
    since: int,
    follow: bool = True,
    revalidate: Callable[[], Awaitable[bool]] | None = None,
    workspace_id: UUID | None = None,
    *,
    viewer: UUID | None = None,
) -> AsyncIterator[ChatItem]:
    """The conversation stream's generator - `_events`, one table over. Lane 1 is
    the `messages` rows (replayed from `since`, then followed - the hub reads each committed
    row back from its NOTIFY pointer); Lane 2 is the reply's chat.* frames, id-less. Register,
    then replay, then drain, with the same seq dedup and the same reauth leash."""
    queue = await hub.register(conversation_id, viewer=viewer)
    pending: asyncio.Task[QueueItem] | None = None
    try:
        seen = since
        while True:
            async with session_scope(workspace_id) as session:
                # Serialized INSIDE the session: an ORM row read after its session closed is a
                # DetachedInstanceError waiting on the first expired attribute (the wire test
                # caught it); the task lane never met this because its replay yields values.
                page = [
                    message_out(row)
                    for row in await Message.since(session, conversation_id, since=seen, limit=REPLAY_PAGE)
                ]
            for item in page:
                seen = item.seq
                yield item
            if len(page) < REPLAY_PAGE:
                break

        if not follow:
            return

        loop = asyncio.get_running_loop()
        interval = settings.api.stream_reauth_seconds
        next_proof = loop.time() + interval
        while True:
            if pending is None:
                pending = asyncio.ensure_future(queue.get())
            timeout = None if revalidate is None else max(0.05, next_proof - loop.time())
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            item: QueueItem | None = None
            if pending in done:
                item = pending.result()
                pending = None

            if revalidate is not None and loop.time() >= next_proof:
                if not await revalidate():
                    return
                next_proof = loop.time() + interval

            if item is None:
                continue
            if isinstance(item, Poison):
                return
            if isinstance(item, MessageOut):
                if item.seq <= seen:
                    # A known seq again is an EDIT of a row already on the wire (a card started,
                    # a line answered or failed, a memory filed): pass it through as an update -
                    # the client replaces in place, and `id:` never moves backwards (below).
                    yield item
                    continue
                if item.seq > seen + 1:
                    # A GAP (found in review): seqs are dense under the conversation's row lock,
                    # so the rows between are on the record; two pointers fetched concurrently
                    # can land out of order, and the lower one must not be lost.
                    async with session_scope(workspace_id) as session:
                        gap = [
                            message_out(row)
                            for row in await Message.since(
                                session, conversation_id, since=seen, limit=item.seq - seen - 1
                            )
                        ]
                    for earlier in gap:
                        if seen < earlier.seq < item.seq:
                            seen = earlier.seq
                            yield earlier
                seen = item.seq
                yield item
            elif isinstance(item, CHAT_LIVE_CLASSES):
                yield item
    finally:
        if pending is not None:
            pending.cancel()
        hub.unregister(conversation_id, queue)


# ── SSE (co-primary) ──────────────────────────────────────────────────────────────────────────


def _refuse_a_full_viewer(viewer: UUID) -> None:
    """429 before a stream opens for a viewer already holding every seat (`hub.StreamSeats`)."""
    if hub.seats.full(viewer):
        raise RateLimited(f"a viewer holds at most {hub.seats.limit} live streams", retry_after=30)


async def task_sse(
    task_id: UUID,
    principal: TaskStreamDep,
    session: SessionDep,
    request: Request,
    since: int = 0,
    follow: bool = True,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """`curl -N -H "Authorization: Bearer $T" "$B/v1/tasks/$ID/events?since=0"` - the flight
    recorder, in a terminal. A browser, whose EventSource cannot set that header, opens it with
    `?ticket=` from `POST /v1/tasks/{id}/events/ticket` instead: single-use, thirty seconds, never
    the session token.

    `Last-Event-ID` beats `?since=` when present. That header is the browser telling us where it
    actually got to, and it is more authoritative than a query parameter written once at connect
    time. Those five lines are the entire reconnect implementation.
    """
    actor, credential = principal.actor, credential_of(principal)
    task = await Task.get_for(session, task_id, actor)  # the stream is not a way around authorization
    workspace_id = task.workspace_id  # the replay's own short sessions act for the task's workspace
    # Read before the rollback below expires both rows: the redaction's two ids,
    # and who this watcher is for presence - a value, never the row, rides the stream.
    viewer_id, creator_id = actor.id, task.creator_actor_id
    _refuse_a_full_viewer(viewer_id)
    # A finite replay (follow=false) is a read, not a presence: no watcher, no editor query.
    conn = request.query_params.get("conn")
    watcher = await presence_ops.watcher(session, task, actor, conn=conn) if follow else None
    # Release the route-scoped connection before the (possibly day-long) stream begins: `get_for`
    # opened a transaction, and holding it pins one idle-in-transaction backend per watcher - the
    # pool dies before the product does. task_ws avoids this with a short session_scope; the
    # generator below opens its own short sessions per replay page, so this session is now idle.
    await session.rollback()

    if last_event_id is not None and last_event_id.isdigit():
        since = int(last_event_id)

    # The credential row that authenticated this connect (by header or by ticket), re-proved on a
    # cadence by the generator: a revoked session or key must not keep a day-long socket flowing.
    async def revalidate() -> bool:
        return await _still_authorized(credential, viewer_id, task_id)

    async def publisher() -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in _events(
                task_id,
                since,
                follow=follow,
                revalidate=revalidate,
                workspace_id=workspace_id,
                on_open=watcher.join if watcher else None,
                on_proof=watcher.here if watcher else None,
                viewer=viewer_id,
            ):
                if watcher is not None:
                    watcher.saw(event)
                # A copy, never a mutation: the hub hands every subscriber the SAME event object.
                out = redact_for(event, viewer_id=viewer_id, creator_id=creator_id)
                frame: dict[str, Any] = {
                    "event": out.type,  # the discriminator IS the SSE event name
                    "data": out.model_dump_json(),
                    "retry": RETRY_MS,
                }
                if is_durable(event):
                    frame["id"] = str(event.seq)  # ← Lane 1 only: the whole point of the id.
                yield frame
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            raise
        except StreamsFull:
            return  # a racer past the door's check: the stream ends, the client's reconnect meets a 429
        except Exception:
            logfire.exception("sse publisher failed", task_id=str(task_id))
            raise
        finally:
            # A killed tab CANCELS this generator: it cannot await, so the
            # leave is sent detached - scheduled outside the cancellation.
            if watcher is not None:
                watcher.close()

    return EventSourceResponse(publisher(), ping=HEARTBEAT_SECONDS, headers=STREAM_HEADERS)


async def conversation_sse(
    conversation_id: UUID,
    principal: ConversationStreamDep,
    session: SessionDep,
    request: Request,
    since: int = 0,
    follow: bool = True,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """`curl -N -H "Authorization: Bearer $T" "$B/v1/conversations/$ID/events?since=0"` - a bot's
    reply, as it is written; a browser opens it with `?ticket=` from
    `POST /v1/conversations/{id}/events/ticket`. `message` events are the durable rows (`id: <seq>`,
    the same shape the list route returns); `chat.start` / `chat.delta` / `chat.activity` / `chat.end` are the
    reply's ephemeral frames. The reconnect contract is the task stream's, verbatim.
    """
    actor, credential = principal.actor, credential_of(principal)
    conv = await Conversation.get_for(session, conversation_id, actor)  # never a way around ownership
    workspace_id, actor_id = conv.workspace_id, actor.id  # values: the rollback below expires both rows
    _refuse_a_full_viewer(actor_id)
    await session.rollback()  # release the route-scoped connection before the long stream

    if last_event_id is not None and last_event_id.isdigit():
        since = int(last_event_id)

    async def revalidate() -> bool:
        return await _still_authorized_conversation(credential, actor_id, conversation_id)

    async def publisher() -> AsyncIterator[dict[str, Any]]:
        newest = since
        try:
            async for event in _chat_events(
                conversation_id,
                since,
                follow=follow,
                revalidate=revalidate,
                workspace_id=workspace_id,
                viewer=actor_id,
            ):
                frame: dict[str, Any] = {"data": event.model_dump_json(), "retry": RETRY_MS}
                if isinstance(event, MessageOut):
                    frame["event"] = "message"
                    if event.seq > newest:
                        # Lane 1 only - the id rule, one table over; an EDIT of a known row
                        # carries no id, so `Last-Event-ID` never walks backwards.
                        newest = event.seq
                        frame["id"] = str(event.seq)
                else:
                    frame["event"] = event.type
                yield frame
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            raise
        except StreamsFull:
            return  # a racer past the door's check: the stream ends, the client's reconnect meets a 429
        except Exception:
            logfire.exception("conversation sse publisher failed", conversation_id=str(conversation_id))
            raise

    return EventSourceResponse(publisher(), ping=HEARTBEAT_SECONDS, headers=STREAM_HEADERS)


# ── WebSocket (a duplex preference, the same events) ──────────────────────────────────────────


async def task_ws(websocket: WebSocket, task_id: UUID) -> None:
    """The same generator, framed as JSON messages.

    The SSE route's door, by the same two paths: the `Authorization` header when the client can set
    one, else a `?ticket=` from `POST /v1/tasks/{id}/events/ticket` (a browser cannot set a
    socket's headers either). The session token never rides the URL. The ticket is read here, never
    declared as a parameter, and the access log and the span redact it.
    """
    raw = bearer_header(websocket)
    ticket = websocket.query_params.get("ticket", "")
    since = int(websocket.query_params.get("since", "0") or 0)
    follow = websocket.query_params.get("follow", "true").lower() != "false"

    async with session_scope() as session:
        try:  # the door's own body: a suspended account or a workspace left never opens a socket
            if raw:
                principal = require_workspace(await authenticate(session, raw))
            else:
                principal = await stream_tickets.admit(session, ticket, StreamSubject.TASK, task_id)
        except (Unauthorized, Forbidden):
            await websocket.close(code=4401, reason="unauthorized")
            return
        try:  # the same backstop and seat count the SSE doors apply
            throttle_actor(principal.actor.id)
            _refuse_a_full_viewer(principal.actor.id)
        except RateLimited:
            await websocket.close(code=4429, reason="too many streams")
            return
        try:
            task = await Task.get_for(session, task_id, principal.actor)
        except NotFound:
            await websocket.close(code=4404, reason="not found")
            return
        workspace_id = task.workspace_id
        viewer_id, creator_id = principal.actor.id, task.creator_actor_id  # redaction
        credential = credential_of(principal)
        conn = websocket.query_params.get("conn")  # presence: follow streams only
        watcher = await presence_ops.watcher(session, task, principal.actor, conn=conn) if follow else None

    await websocket.accept()

    async def revalidate() -> bool:
        return await _still_authorized(credential, viewer_id, task_id)  # the SSE lane's leash, same cadence

    try:
        async for event in _events(
            task_id,
            since,
            follow=follow,
            revalidate=revalidate,
            workspace_id=workspace_id,
            on_open=watcher.join if watcher else None,
            on_proof=watcher.here if watcher else None,
            viewer=viewer_id,
        ):
            if watcher is not None:
                watcher.saw(event)
            out = redact_for(event, viewer_id=viewer_id, creator_id=creator_id)
            await websocket.send_text(out.model_dump_json())
    except WebSocketDisconnect:
        pass
    except StreamsFull:
        await websocket.close(code=4429, reason="too many streams")
    finally:
        if watcher is not None:
            watcher.close()  # before any await: a torn-down socket may be cancelled
        with contextlib.suppress(Exception):
            await websocket.close()
