# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What `web_fetch` showed the model (its return value, with or without the shelf state it carries),
and a site that answers it the way a real server does. A plain module, so a test imports it and never
another test module."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic_ai import ToolReturn


def shown(fetch: Any) -> Any:
    """`fetch` (the raw tool function), answering what the model reads."""

    async def call(*args: Any, **kwargs: Any) -> Any:
        result = await fetch(*args, **kwargs)
        return result.return_value if isinstance(result, ToolReturn) else result

    return call


class Wire(httpx.AsyncByteStream):
    """A body exactly as the wire carries it. httpx reads (and decodes) a `content=` response the
    moment it is built, which no real server's response is; the fetch reads the raw stream."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


def on_the_wire(handler: Any) -> Any:
    """`handler` (a `MockTransport` handler, sync or async) answering off the wire: a `content=`
    response is sent again as a raw stream."""

    def raw(response: httpx.Response) -> httpx.Response:
        if not response.is_stream_consumed:  # built with stream=Wire(...) already
            return response
        return httpx.Response(response.status_code, headers=response.headers, stream=Wire(response.content))

    def serve(request: httpx.Request) -> Any:
        answer = handler(request)
        if not inspect.isawaitable(answer):
            return raw(answer)

        async def later() -> httpx.Response:
            return raw(await answer)

        return later()

    return serve
