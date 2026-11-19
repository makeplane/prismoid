# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A fetched body, read under the web door's byte cap (ADR-046).

`web_fetch` asks for `identity` and reads the raw stream, so no decoder runs ahead of the cap. A
server that sends gzip anyway is inflated in steps that never yield past it; any other coding is
refused, since httpx would decode brotli or zstd a whole network chunk at a time, and a few
kilobytes of brotli inflate to gigabytes in the shared worker before a cap could look. A refusal is
a `FetchDenied`, whose message is the fix.
"""

from __future__ import annotations

import zlib
from typing import Any

import httpx


class FetchDenied(Exception):
    """A refusal whose message IS the fix: journaled `ok=false` and RETURNED to the model (a
    `ModelRetry` would burn the schema-retry budget)."""

    spent: bool = False  # the read was tried, so it cost a unit of the budget


def _inflater(response: httpx.Response, url: str) -> Any:
    """The decoder for the body's `Content-Encoding`: None for an unencoded one, a zlib inflater the
    cap can bound for gzip; any other coding is refused (the module docstring says why)."""
    coding = response.headers.get("content-encoding", "").strip().lower()
    if coding in ("", "identity"):
        return None
    if coding in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    raise FetchDenied(
        f"{url} sent a {coding}-encoded body, which this tool does not read - "
        "attach the file to the task as an input instead"
    )


def _inflate(inflater: Any, data: bytes, max_length: int, url: str) -> bytes:
    """One bounded inflate step; a body marked gzip that is not is a refusal, never a crash."""
    try:
        return inflater.decompress(data, max_length)
    except zlib.error:
        raise FetchDenied(
            f"{url} sent a body marked gzip that could not be inflated - the server may be broken"
        ) from None


async def read_capped(response: httpx.Response, max_bytes: int, url: str) -> bytes:
    """The body, inflated when it is gzip, never more than `max_bytes`; past that a `FetchDenied`."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchDenied(
            f"{url} is {int(declared) // 1_000_000} MB - over the {max_bytes // 1_000_000} MB "
            "fetch cap. Attach the file to the task as an input instead."
        )
    inflater = _inflater(response, url)
    buf = bytearray()
    async for chunk in response.aiter_raw():
        pending = chunk
        while pending and len(buf) <= max_bytes:
            if inflater is None:
                buf += pending
                break
            # max_length is never 0 here (0 means unbounded): the loop stops once buf passes the cap.
            buf += _inflate(inflater, pending, max_bytes + 1 - len(buf), url)
            pending = inflater.unconsumed_tail
        if len(buf) > max_bytes:
            raise FetchDenied(
                f"{url} passed the {max_bytes // 1_000_000} MB fetch cap mid-download. "
                "Attach the file to the task as an input instead."
            )
    return bytes(buf)
