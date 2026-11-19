# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker's CDP connection to a remote computer's browser (ADR-063): flattened CDP over one
WebSocket to the provider's relay, dialled with a single-use ticket
in its URL (`Ticket.dial_url`), never a header and never logged. A device's Prismoid Chrome is the
same connection, dialled over the helper's Unix socket with the helper key in a header
(`connect_unix`).

It keeps the contract every `CdpConnection` keeps (the daemon's `PipeTransport` is the other), and
the browser verbs rely on each clause (`tests/unit/test_cdp_connection_contract.py` pins them on
both):

* `call` raises `CdpError` (code, message) for a command Chromium refused, and `CdpClosed` for a
  connection that is gone - every call in flight when it went, and every call after;
* a session's calls in flight fail with `CdpError(-32001)` the moment `Target.detachedFromTarget`
  names it, instead of waiting out their timeouts with the verb lock held;
* event handlers run synchronously on the loop, in arrival order, before any later message is
  read; a handler that raises is suppressed;
* a person taking the computer's screen closes the socket 1013 (the provider's CDP gate,
  `GATE_CLOSE`): `close_code` says so once the connection is gone, so its owner can tell "a person
  is driving" from "the connection dropped"."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any, Final, cast

import logfire
from websockets.asyncio.client import ClientConnection, connect, unix_connect
from websockets.exceptions import ConnectionClosed

from prismoid.ai.frames.image_browser.sidecar.cdp import CdpClosed, CdpError

GATE_CLOSE: Final = 1013  # a CDP gate closes every CDP client of a screen a person takes
MAX_MESSAGE: Final = 64 * 1024 * 1024  # the pipe reader's limit: a full-page screenshot fits
OPEN_TIMEOUT_S: Final = 10.0

Handler = Callable[[dict[str, Any]], None]


class WsCdp:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._session_of: dict[int, str] = {}  # a pending call -> the session it went to
        self._handlers: dict[str, list[Handler]] = {}
        self._closed = False
        self._pump = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, url: str, *, open_timeout: float = OPEN_TIMEOUT_S) -> WsCdp:
        """Dial `url` (a ticket's `dial_url`: its single-use ticket rides the query). No
        compression: CDP's screenshots are base64 JPEG and PNG, which deflate cannot shrink.

        A failed dial raises `ConnectionError` naming no URL: `websockets` puts the URI in its
        connect exceptions, so they are dropped (`from None`) rather than chained into a logged
        traceback. Callers must never log `url` either."""
        try:
            ws = await connect(url, max_size=MAX_MESSAGE, open_timeout=open_timeout, compression=None)
        except Exception as e:
            raise ConnectionError(f"could not dial the computer's browser ({type(e).__name__})") from None
        return cls(ws)

    @classmethod
    async def connect_unix(
        cls, socket_path: str, url: str, *, headers: dict[str, str], open_timeout: float = OPEN_TIMEOUT_S
    ) -> WsCdp:
        """Dial `url` over the Unix socket at `socket_path` (the device helper's relay): no port is
        opened, and the credential rides `headers`, never
        the URL. A failed dial raises `ConnectionError` naming neither the socket nor a header."""
        try:
            ws = await unix_connect(
                socket_path,
                url,
                additional_headers=headers,
                max_size=MAX_MESSAGE,
                open_timeout=open_timeout,
                compression=None,
            )
        except Exception as e:
            raise ConnectionError(f"could not dial the device's browser ({type(e).__name__})") from None
        return cls(ws)

    @property
    def close_code(self) -> int | None:
        """The code the connection closed with; None while it is open."""
        return self._ws.close_code if self._closed else None

    def on(self, event: str, handler: Handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 - the transport's own deadline, as PipeTransport's
    ) -> dict[str, Any]:
        if self._closed:
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
                await self._ws.send(json.dumps(msg))
            except ConnectionClosed:
                self._fail_all()
                raise CdpClosed from None
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)
            self._session_of.pop(msg_id, None)
            if fut.done() and not fut.cancelled():
                fut.exception()  # read it: a call that raised its own error leaves none unretrieved

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(cast("dict[str, Any]", msg))
                except Exception:  # a malformed frame never kills the pump, but it leaves a trace
                    logfire.warn("a CDP frame could not be dispatched; the pump goes on", _exc_info=True)
        except ConnectionClosed:
            pass
        finally:
            self._fail_all()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg:
            msg_id = msg["id"]
            fut = self._pending.get(msg_id) if type(msg_id) is int else None
            if fut is None or fut.done():
                return
            error = msg.get("error")
            if isinstance(error, dict):
                refusal = cast("dict[str, Any]", error)  # JSON object keys are always strings
                try:
                    code = int(refusal.get("code", 0))
                except (TypeError, ValueError, OverflowError):
                    code = 0  # a malformed refusal still resolves its call
                fut.set_exception(CdpError(code, str(refusal.get("message", ""))))
            else:
                result = msg.get("result")
                fut.set_result(cast("dict[str, Any]", result) if isinstance(result, dict) else {})
            return
        method = msg.get("method")
        if not isinstance(method, str):
            return
        if method == "Target.detachedFromTarget":
            params = msg.get("params")
            if isinstance(params, dict):
                self._fail_session(cast("dict[str, Any]", params).get("sessionId"))
        for handler in self._handlers.get(method, []):
            try:
                handler(msg)
            except Exception:  # one listener's bug never starves the others, but it is seen
                logfire.warn("a CDP event handler for {method} raised", method=method, _exc_info=True)

    def _fail_session(self, session_id: object) -> None:
        """A tab's session detached (the tab closed): Chromium never answers what was in flight
        on it, so those calls fail now (PipeTransport's rule)."""
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
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pump
        self._fail_all()
