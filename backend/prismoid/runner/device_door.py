# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device helper's door: its Starlette app, on `run/helper.sock`.

Transport only: each route reads the request, asks `prismoid.device.helper.Helper` or a `prismoid.device`
module with plain values, and says the answer in HTTP. A file-door refusal is a 4xx, and a frame that
is gone is a 404. `KeyGate` stands in front of every route, healthz and the CDP relay's WebSocket
included; `_relay_cdp` carries the agent's CDP between the socket and its `cdp_relay.TaskRelay`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import signal
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from prismoid.device import cdp_relay, files
from prismoid.device import exec as device_exec
from prismoid.device.chrome import Chrome, ChromeError
from prismoid.device.egress import apply_allow
from prismoid.device.frames import DeviceFrameState, GrantRefused, HelperStopping
from prismoid.device.helper import KEY_HEADER, Helper, read_device_name
from prismoid.schemas.device import (
    DirOut,
    EgressIn,
    ExecIn,
    FileDataOut,
    FileIn,
    FrameSpecIn,
    GrantsIn,
    InterruptedAckIn,
    PathIn,
    SignalIn,
)

_SIGNALS = {"stop": signal.SIGSTOP, "cont": signal.SIGCONT}
_DOOR_ERRORS = (OSError, ValueError, files.FileTooLarge)


def _helper(connection: HTTPConnection) -> Helper:
    return connection.app.state.helper


def _frame(request: Request) -> DeviceFrameState | None:
    return _helper(request).frame(request.path_params["ref"])


def _acting(request: Request) -> DeviceFrameState | None:
    return _helper(request).acting(request.path_params["ref"])


async def _body[In: BaseModel](request: Request, model: type[In]) -> In | Response:
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError) as e:
        return JSONResponse({"detail": str(e)}, status_code=422)


def _refused(detail: object, status: int) -> Response:
    return JSONResponse({"detail": str(detail)}, status_code=status)


def _no_frame() -> Response:
    return _refused("no such frame", 404)


def _done() -> Response:
    return Response(status_code=204)


def _door_refused(e: Exception) -> Response:
    """A file-door refusal, always a 4xx: a bad path (a NUL byte), bad base64 or a non-path."""
    if isinstance(e, IsADirectoryError):  # its own text names a descriptor or a rename's temporary
        return _refused("a folder, not a file", 400)
    if isinstance(e, files.FileTooLarge):
        status = 413
    elif isinstance(e, PermissionError):
        status = 403
    elif isinstance(e, FileNotFoundError):
        status = 404
    elif isinstance(e, ValueError):
        status = 422
    else:
        status = 400
    return _refused(e, status)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True, "device_name": _helper(request).name})


async def create_frame(request: Request) -> Response:
    if isinstance(spec := await _body(request, FrameSpecIn), Response):
        return spec
    helper = _helper(request)
    try:
        state = await helper.create_frame(spec)
    except GrantRefused as e:
        return _refused(e, 422)
    except ChromeError as e:
        return _refused(e, 503)
    return JSONResponse(helper.out(state))


async def list_frames(request: Request) -> Response:
    return JSONResponse(_helper(request).all_out())


async def get_frame(request: Request) -> Response:
    if (state := _frame(request)) is None:
        return _no_frame()
    return JSONResponse(_helper(request).out(state))


async def delete_frame(request: Request) -> Response:
    if not await _helper(request).delete_frame(request.path_params["ref"]):
        return _no_frame()
    return _done()


async def run_exec(request: Request) -> Response:
    if (state := _acting(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, ExecIn), Response):
        return req
    try:
        out = await _helper(request).run(state, req, request.is_disconnected)
    except device_exec.FenceRefused as e:
        return _refused(f"the fence refused this frame: {e}", 422)
    except HelperStopping as e:  # to the worker a helper gone away, never a command's result
        return _refused(e, 503)
    return JSONResponse(out.model_dump(mode="json"))


async def read_file(request: Request) -> Response:
    if (state := _acting(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, PathIn), Response):
        return req
    try:
        data = await asyncio.to_thread(files.read, state, req.path, nofollow=req.nofollow)
    except _DOOR_ERRORS as e:
        return _door_refused(e)
    return JSONResponse(FileDataOut(data_b64=base64.b64encode(data).decode()).model_dump())


async def write_file(request: Request) -> Response:
    if (state := _acting(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, FileIn), Response):
        return req
    try:
        await asyncio.to_thread(files.write, state, req)
    except _DOOR_ERRORS as e:
        return _door_refused(e)
    return _done()


async def list_dir(request: Request) -> Response:
    if (state := _acting(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, PathIn), Response):
        return req
    try:
        names = await asyncio.to_thread(files.list_dir, state, req.path)
    except _DOOR_ERRORS as e:
        return _door_refused(e)
    return JSONResponse(DirOut(names=names).model_dump())


async def send_signal(request: Request) -> Response:
    if (state := _frame(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, SignalIn), Response):
        return req
    state.groups.signal(_SIGNALS[req.signal])
    return _done()


async def hand_over(request: Request) -> Response:
    if (state := _frame(request)) is None:
        return _no_frame()
    _helper(request).chrome.hand_over(state.ref)
    return _done()


async def put_egress(request: Request) -> Response:
    """Answers the list. Nothing pushes one under a parked step: a push is its resume, or the park
    given back with an empty list, and neither leaves a hand-off open (`Helper.acting`). First, so
    that what a page is off is the list the pass ran under."""
    if (state := _acting(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, EgressIn), Response):
        return req
    status, body = apply_allow(state, req.domains, req.version)
    return JSONResponse(body, status_code=status)


async def put_grants(request: Request) -> Response:
    """Answers the frame, its roots changed (`Helper.regrant`)."""
    if (state := _frame(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, GrantsIn), Response):
        return req
    helper = _helper(request)
    try:
        await helper.regrant(state, req)
    except GrantRefused as e:
        return _refused(e, 422)
    return JSONResponse(helper.out(state))


async def denials(request: Request) -> Response:
    if (state := _frame(request)) is None:
        return _no_frame()
    return JSONResponse({"denials": list(state.denials)})


async def stop_services(request: Request) -> Response:
    """The Machine sleeps: no step is parked on a person."""
    if (state := _acting(request)) is None:
        return _no_frame()
    await _helper(request).stop_services(state)
    return _done()


async def forget_quarantine(request: Request) -> Response:
    """A task was deleted: what its frames moved out of the person's folders goes with it. A
    frame's end alone keeps it (a reset, a reconciled frame). Answers with or without a frame."""
    if not _helper(request).registry.forget_quarantine(request.path_params["ref"]):
        return _no_frame()
    return _done()


async def reset_mounts(request: Request) -> Response:
    if (state := _frame(request)) is None:
        return _no_frame()
    await asyncio.to_thread(_helper(request).registry.reset_mounts, state)
    return _done()


async def interrupted(request: Request) -> Response:
    if (state := _frame(request)) is None:
        return _no_frame()
    cut = state.journal.interrupted()
    return _done() if cut is None else JSONResponse(cut.model_dump(mode="json"))


async def ack_interrupted(request: Request) -> Response:
    """The worker has parked or resumed on the cut-off command: it stops being reported."""
    if (state := _frame(request)) is None:
        return _no_frame()
    if isinstance(req := await _body(request, InterruptedAckIn), Response):
        return req
    state.journal.ack(req.exec_id)
    return _done()


async def cdp(websocket: WebSocket) -> None:
    """The task's agent drives its window, and only its window (`cdp_relay`)."""
    ref = websocket.path_params["ref"]
    helper = _helper(websocket)
    state = helper.frame(ref)
    if state is None or state.spec.profile != "browser":  # no such frame, or no browser in it
        await websocket.close(code=1008)
        return
    await _relay_cdp(websocket, helper.chrome, ref)


async def _relay_cdp(websocket: WebSocket, chrome: Chrome, ref: str) -> None:
    """One agent connection to the task `ref`'s window: Chrome comes up and the window opens first
    (a helper that restarted reopens it here), then CDP flows until either side goes. A Chrome that
    cannot start, or goes, closes the socket 1011 saying why."""
    await websocket.accept()
    try:
        await chrome.window_for(ref)
    except ChromeError as e:
        await websocket.close(code=1011, reason=str(e)[:120])
        return
    out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    relay = cdp_relay.TaskRelay(chrome, ref, out.put_nowait)

    async def write() -> None:
        while (msg := await out.get()) is not None:
            await websocket.send_text(json.dumps(msg))

    async def read() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            try:
                msg = json.loads(message.get("text") or message.get("bytes") or b"")
            except ValueError:
                continue
            if isinstance(msg, dict):
                relay.handle(msg)

    writer, reader = asyncio.create_task(write()), asyncio.create_task(read())
    lost = asyncio.create_task(relay.lost.wait())
    try:
        await asyncio.wait({writer, reader, lost}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        relay.close()
        out.put_nowait(None)  # what was queued still goes, then the writer ends
        reader.cancel()
        lost.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer, 2.0)
        writer.cancel()
        if relay.lost.is_set():
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason=relay.why[:120])


class KeyGate:
    """`KEY_HEADER` on every connection, before any route reads anything."""

    def __init__(self, inner: ASGIApp, key: str) -> None:
        self.inner = inner
        self.key = key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            given = dict(scope["headers"]).get(KEY_HEADER.lower().encode(), b"").decode()
            if not given or not hmac.compare_digest(given.encode(), self.key.encode()):
                if scope["type"] == "websocket":  # refused at the handshake: a 403, never a socket
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None]:
    helper: Helper = app.state.helper
    await helper.start()
    try:
        yield
    finally:
        await helper.shutdown()


ROUTES = [
    Route("/healthz", healthz, methods=["GET"]),
    Route("/frames", create_frame, methods=["POST"]),
    Route("/frames", list_frames, methods=["GET"]),
    Route("/frames/{ref}", get_frame, methods=["GET"]),
    Route("/frames/{ref}", delete_frame, methods=["DELETE"]),
    Route("/frames/{ref}/exec", run_exec, methods=["POST"]),
    Route("/frames/{ref}/files/read", read_file, methods=["POST"]),
    Route("/frames/{ref}/files/write", write_file, methods=["POST"]),
    Route("/frames/{ref}/files/list", list_dir, methods=["POST"]),
    Route("/frames/{ref}/signal", send_signal, methods=["POST"]),
    Route("/frames/{ref}/hand-over", hand_over, methods=["POST"]),
    Route("/frames/{ref}/egress", put_egress, methods=["PUT"]),
    Route("/frames/{ref}/grants", put_grants, methods=["PUT"]),
    Route("/frames/{ref}/denials", denials, methods=["GET"]),
    Route("/frames/{ref}/reset_mounts", reset_mounts, methods=["POST"]),
    Route("/frames/{ref}/stop-services", stop_services, methods=["POST"]),
    Route("/frames/{ref}/quarantine", forget_quarantine, methods=["DELETE"]),
    Route("/frames/{ref}/interrupted", interrupted, methods=["GET"]),
    Route("/frames/{ref}/interrupted/ack", ack_interrupted, methods=["POST"]),
    WebSocketRoute("/frames/{ref}/cdp", cdp),
]


def create_app(
    home: Path, device_name: Callable[[], str] = read_device_name, chrome_path: str | None = None
) -> Starlette:
    helper = Helper(home, device_name, chrome_path)
    app = Starlette(routes=ROUTES, lifespan=_lifespan)
    app.add_middleware(KeyGate, key=helper.key)
    app.state.helper = helper
    app.state.egress, app.state.chrome, app.state.registry = helper.egress, helper.chrome, helper.registry
    app.state.dockers, app.state.ports, app.state.halt = helper.dockers, helper.ports, helper.halt
    return app
