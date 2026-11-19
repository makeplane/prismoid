# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The far end of a `CdpConnection`, a scripted Chromium (the verbs'
tests run once per connection type). `PipePeer` speaks the DevTools pipe (NUL-terminated JSON on
two OS pipes), as a `--remote-debugging-pipe` Chromium does for the daemon's `PipeTransport`;
`WsPeer` serves a WebSocket on localhost, as a remote CDP relay does for the worker's `WsCdp`.

Both answer each call through `answer` (usually a `FakeCdp.call`) and send events in the order
they are emitted, on the same stream as the replies. An answer that raises is sent as CDP's error
reply (`code`, `message` read off the exception), so no exception CLASS crosses the wire: the
worker's `CdpError` and the tests' `sidecar.cdp.CdpError` are different classes (the package path
and the tests' top-level alias), and only the wire shape matters here. Keep scripted messages
under 64 KiB: `PipePeer` writes on the loop's thread, and a full pipe would block it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from prismoid.ai.frames.cdp_ws import WsCdp
from prismoid.ai.frames.image_browser.sidecar.cdp import PipeTransport

Answer = Callable[[str, dict[str, Any], str | None], Awaitable[dict[str, Any]]]


def _reply(msg: dict[str, Any], result: dict[str, Any] | None, error: BaseException | None) -> dict[str, Any]:
    if error is None:
        return {"id": msg["id"], "result": result or {}}
    code = getattr(error, "code", -32000)
    message = getattr(error, "message", str(error))
    return {"id": msg["id"], "error": {"code": code, "message": message}}


class _Peer:
    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.received: list[dict[str, Any]] = []
        # method -> an event the reply waits for: a call Chromium has not answered yet
        self.holds: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def _send(self, msg: dict[str, Any]) -> None:
        raise NotImplementedError

    def _serve(self, msg: dict[str, Any]) -> None:
        self.received.append(msg)
        task = asyncio.create_task(self._serve_one(msg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_one(self, msg: dict[str, Any]) -> None:
        if (hold := self.holds.get(str(msg.get("method")))) is not None:
            await hold.wait()
        try:
            result = await self.answer(
                str(msg["method"]), dict(msg.get("params") or {}), msg.get("sessionId")
            )
        except Exception as e:
            await self._send(_reply(msg, None, e))
        else:
            await self._send(_reply(msg, result, None))

    async def emit(
        self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None
    ) -> None:
        msg: dict[str, Any] = {"method": method, "params": params or {}}
        if session is not None:
            msg["sessionId"] = session
        await self._send(msg)

    async def send_raw(self, msg: dict[str, Any]) -> None:
        """Any message as given, well-formed or not: for the frames a relay should never send."""
        await self._send(msg)

    async def _stop_tasks(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class PipePeer(_Peer):
    """Chromium's side of `PipeTransport`: it reads what the transport writes and writes what the
    transport reads."""

    def __init__(self, answer: Answer) -> None:
        super().__init__(answer)
        self._to_conn_r, self._to_conn_w = os.pipe()  # the peer writes, the transport reads
        self._from_conn_r, self._from_conn_w = os.pipe()  # the transport writes, the peer reads
        self._reader: asyncio.Task[None] | None = None
        self._read_transport: asyncio.ReadTransport | None = None

    async def connect(self) -> PipeTransport:
        transport = PipeTransport()
        await transport.start(*await self.listen())
        return transport

    async def listen(self) -> tuple[int, int]:
        """Start answering, and hand back the client's two ends - the fd it reads, the fd it writes
        - for any client of the DevTools pipe (`connect`'s transport, the device helper's Chrome)."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        self._read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(self._from_conn_r, "rb", 0)
        )
        self._reader = asyncio.create_task(self._loop(reader))
        return self._to_conn_r, self._from_conn_w

    async def _loop(self, reader: asyncio.StreamReader) -> None:
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                raw = await reader.readuntil(b"\0")
                self._serve(json.loads(raw[:-1]))

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._to_conn_w >= 0:
            with contextlib.suppress(OSError):
                os.write(self._to_conn_w, json.dumps(msg).encode() + b"\0")

    def write_raw(self, data: bytes) -> None:
        """Bytes straight onto the pipe, as Chromium's side would write them: several messages in one
        write, or a message cut where the test says."""
        os.write(self._to_conn_w, data)

    async def drop(self, code: int = 1011, reason: str = "") -> None:
        """Chromium exits: its end of the pipe closes (a pipe has no close code)."""
        if self._to_conn_w >= 0:
            os.close(self._to_conn_w)
            self._to_conn_w = -1

    async def aclose(self) -> None:
        await self._stop_tasks()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._read_transport is not None:
            self._read_transport.close()
        await self.drop()


class WsPeer(_Peer):
    """A remote CDP relay, as the worker sees it: one WebSocket, text frames of CDP JSON."""

    def __init__(self, answer: Answer) -> None:
        super().__init__(answer)
        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._open = asyncio.Event()
        self.path = ""  # the path and query the client dialled
        self.ended = False  # the client's connection is over (it closed, or the peer dropped it)

    async def connect(self) -> WsCdp:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        conn = await WsCdp.connect(
            f"ws://127.0.0.1:{port}/v1/computers/cmp_test/browser/cdp?ticket=tk_secret"
        )
        await self._open.wait()
        return conn

    async def _handler(self, ws: ServerConnection) -> None:
        self._ws = ws
        self.path = ws.request.path if ws.request is not None else ""
        self._open.set()
        try:
            with contextlib.suppress(ConnectionClosed):
                async for raw in ws:
                    self._serve(json.loads(raw))
        finally:
            self.ended = True

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is not None:
            with contextlib.suppress(ConnectionClosed):
                await self._ws.send(json.dumps(msg))

    async def drop(self, code: int = 1011, reason: str = "") -> None:
        if self._ws is not None:
            await self._ws.close(code, reason)

    async def aclose(self) -> None:
        await self._stop_tasks()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
