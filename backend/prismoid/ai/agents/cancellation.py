# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Cancel stops the stream (ADR-064, ADR-089): a step's run carries a `CancellationToken`, fired the
moment a poll sees its task cancelled, so a long stream stops spending within about a second
instead of at its next safe point.

The poll asks operations' probe once a second while the run lasts (no NOTIFY channel: a cancel is
rare, and one primary-key read a second per running step is cheaper than a listener per run). Only a
cancel fires the token: it is terminal, so a cut attempt is never replayed, and the model request it
cut leaves no result row in the call log. A steer or a pause waits for its safe point as before.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar
from typing import Final

import logfire
from pydantic_ai import CancellationToken

POLL_S: Final = 1.0  # how often a run asks whether its task was cancelled

type Cancelled = Callable[[], Awaitable[bool]]  # operations' probe: was the task cancelled?

_TOKEN: ContextVar[CancellationToken | None] = ContextVar("prismoid_cancellation_token", default=None)


def token() -> CancellationToken | None:
    """The token of the run in flight, or None for a run nobody can cancel."""
    return _TOKEN.get()


@contextlib.asynccontextmanager
async def watched(cancelled: Cancelled | None) -> AsyncGenerator[None]:
    """Run the code inside under a fresh token that the poll fires; with no probe, under none (a run
    nested in a watched one does not inherit its token)."""
    reset = _TOKEN.set(None)
    poll = None
    if cancelled is not None:
        fresh = CancellationToken()
        _TOKEN.set(fresh)
        poll = asyncio.create_task(_poll(fresh, cancelled))
    try:
        yield
    finally:
        _TOKEN.reset(reset)
        if poll is not None:
            poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll


async def _poll(fired: CancellationToken, cancelled: Cancelled) -> None:
    """Ask every `POLL_S` until the task is cancelled, then fire. Fail-soft: a probe that cannot read
    is logged and asked again next tick (the safe points still stop the run)."""
    while True:
        await asyncio.sleep(POLL_S)
        try:
            if await cancelled():
                fired.cancel()
                return
        except Exception:
            logfire.exception("the cancel poll could not read its task; asking again")
