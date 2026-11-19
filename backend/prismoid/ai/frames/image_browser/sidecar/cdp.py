# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The DevTools pipe. Chromium started with `--remote-debugging-pipe` reads JSON messages
terminated by `\\0` on fd 3 and writes them on fd 4. No port, no WebSocket, no dependency."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from typing import Any, Protocol


class CdpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CdpClosed(Exception):
    """The pipe ended: Chromium exited or was killed."""


class CdpConnection(Protocol):
    """What the verbs speak: flattened CDP - one connection, sessions
    named on each call. `PipeTransport` is the daemon's (Chromium's `--remote-debugging-pipe`); a
    worker's WebSocket to a computer's browser is `cdp_ws.WsCdp`."""

    def on(self, event: str, handler: Callable[[dict[str, Any]], None]) -> None: ...

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class PipeTransport:
    def __init__(self) -> None:
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._session_of: dict[int, str] = {}  # a pending call -> the session it went to
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._reader: asyncio.StreamReader | None = None
        self._read_transport: Any = None
        self._writer_fd: int | None = None
        self._write_lock = asyncio.Lock()
        self._pump: asyncio.Task[None] | None = None
        self._closed = False

    async def _write(self, data: bytes) -> None:
        assert self._writer_fd is not None
        loop = asyncio.get_running_loop()
        async with self._write_lock:
            view = memoryview(data)
            while view:
                n = await loop.run_in_executor(None, os.write, self._writer_fd, view)
                view = view[n:]

    def on(self, event: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def start(self, read_fd: int, write_fd: int) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        self._reader = reader
        self._read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", 0)
        )
        self._writer_fd = write_fd
        self._pump = asyncio.create_task(self._read_loop())

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        if self._closed or self._writer_fd is None:
            raise CdpClosed
        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        if session_id:
            self._session_of[msg_id] = session_id
        try:
            try:
                data = json.dumps(msg).encode() + b"\0"
                await self._write(data)
            except OSError:
                self._pending.pop(msg_id, None)
                self._fail_all()
                raise CdpClosed from None
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)
            self._session_of.pop(msg_id, None)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readuntil(b"\0")
                msg = json.loads(raw[:-1])
                if "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut is None or fut.done():
                        continue
                    if "error" in msg:
                        fut.set_exception(
                            CdpError(msg["error"].get("code", 0), msg["error"].get("message", ""))
                        )
                    else:
                        fut.set_result(msg.get("result", {}))
                else:
                    if msg.get("method") == "Target.detachedFromTarget":
                        self._fail_session(msg.get("params", {}).get("sessionId"))
                    for h in self._handlers.get(msg.get("method", ""), []):
                        with contextlib.suppress(Exception):
                            h(msg)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            OSError,
            asyncio.LimitOverrunError,
            ValueError,
        ):
            self._fail_all()
        except asyncio.CancelledError:
            self._fail_all()
            raise

    def _fail_session(self, session_id: object) -> None:
        """A tab's session detached (the tab closed): Chromium never answers what was in flight
        on it, so those calls fail now instead of at their timeout, with the verb lock held."""
        for msg_id, session in tuple(self._session_of.items()):
            fut = self._pending.get(msg_id)
            if session == session_id and fut is not None and not fut.done():
                fut.set_exception(CdpError(-32001, "the tab closed"))

    def _fail_all(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CdpClosed())
        self._pending.clear()

    async def close(self) -> None:
        self._closed = True
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, CdpClosed):
                await self._pump
        if self._read_transport is not None:
            with contextlib.suppress(Exception):
                self._read_transport.close()
            self._read_transport = None
        if self._writer_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._writer_fd)
            self._writer_fd = None
