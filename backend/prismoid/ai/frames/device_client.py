# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker's side of the device helper's door: one HTTP request over its Unix socket, with its key.

A device is never "gone", only away: a key or a socket that is not there, a refused key or a helper
that errs is `FrameUnreachable`, and every other answer is the caller's to read. `device_driver.py`
is what speaks through this.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from prismoid.ai import config
from prismoid.ai.frames.driver import FrameUnreachable

KEY_HEADER = "X-Prismoid-Helper-Key"
HELPER_HOST = "helper"  # the socket is the address; httpx wants a name to put in the Host header
_QUICK_S = 30.0


def helper_key() -> str:
    """The key the helper keeps in its home. One that is not there is a helper that is not
    running: unreachable, never gone."""
    try:
        return (config.current().local.home / "helper.key").read_text().strip()
    except OSError as e:
        raise FrameUnreachable(f"the device helper's key is unreadable ({e.strerror or e})") from e


def helper_socket() -> str:
    return str(config.current().local.home / "run" / "helper.sock")


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    """The helper's door, opened with its key. A key or a socket that is not there is a helper that
    is not running: unreachable, never gone."""
    key = helper_key()
    return httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(uds=helper_socket()),
        base_url=httpx.URL(scheme="http", host=HELPER_HOST),
        headers={KEY_HEADER: key},
    )


async def call(
    transport: httpx.AsyncBaseTransport | None,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    timeout: float = _QUICK_S,  # noqa: ASYNC109
) -> httpx.Response:
    """One request. Transport trouble, a refused key and a helper 5xx are `FrameUnreachable`; every
    other answer (the 4xx the helper means) is the caller's to read."""
    async with _client(transport) as client:
        try:
            response = await client.request(method, path, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise FrameUnreachable(f"the device helper did not answer ({type(e).__name__}: {e})") from e
    if response.status_code == 401 or response.status_code >= 500:
        raise FrameUnreachable(f"the device helper answered {response.status_code}")
    return response


async def helper_name(transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The name of the computer whose helper answers its door (`prismoid dev device-ping`), or
    `FrameUnreachable` saying why it did not."""
    return str((await call(transport, "GET", "/healthz")).json()["device_name"])


def detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))
    except (ValueError, AttributeError):
        return response.text
