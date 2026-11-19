# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a wake waits (ADR-063): a provider that does not answer, or a host with no
room, is asked again with backoff, and the step says so once."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import logfire

from prismoid.ai import frames
from prismoid.ai.frames.driver import FrameUnreachable, NativeSleep
from prismoid.conf import settings
from prismoid.errors import HostFull, PrismoidError

Phase = Callable[[str], Awaitable[None]]

WAKE_SLOW = "The Machine is taking longer to wake"
RETRY_FIRST_S = 1.0
RETRY_MAX_S = 15.0


class Backoff:
    """One retry loop's wait: `left` is what remains of `wait_s`; `wait` says `say` to the step once,
    then sleeps from 1 s, doubling to 15 s, never past the deadline. `reset` starts the doubling over."""

    def __init__(self, wait_s: float, on_phase: Phase | None) -> None:
        self._loop = asyncio.get_running_loop()
        self.deadline = self._loop.time() + wait_s
        self._on_phase = on_phase
        self._delay = RETRY_FIRST_S
        self._told = False

    def left(self) -> float:
        return self.deadline - self._loop.time()

    async def wait(self, say: str, *, reset: bool = False) -> None:
        if self._on_phase is not None and not self._told:
            await self._on_phase(say)
            self._told = True
        await asyncio.sleep(min(self._delay, self.left()))
        self._delay = RETRY_FIRST_S if reset else min(self._delay * 2, RETRY_MAX_S)


async def wake_native(frame: frames.FrameDriver, machine_id: uuid.UUID, on_phase: Phase | None) -> bool:
    """`resume` + `bind`, until the provider answers (ADR-063): True when the computer is up
    and bound, False when the provider says it is gone (a RuntimeError from either call). A Wake it
    does not answer (`FrameUnreachable`: the provider restarting, its API unreachable) is asked again
    with backoff, and each Wake is cut at what is left of `wake_retry_s`, so a slow host never holds
    the step past it; the step is told once that the Machine is taking longer to wake. When the
    budget runs out the last `FrameUnreachable` is raised, naming the budget. Anything else raises
    as it is. Both calls are idempotent, so a Wake retried after the provider did answer changes
    nothing."""
    if not isinstance(frame, NativeSleep):  # the caller read `sleep_kind`
        raise PrismoidError(f"a {frame.driver} frame cannot sleep natively")
    budget = settings.machines.wake_retry_s
    backoff = Backoff(budget, on_phase)
    while True:
        try:
            async with asyncio.timeout_at(backoff.deadline if budget > 0 else None):
                await frame.resume()
                await frame.bind()
            return True
        except RuntimeError:
            return False
        except (FrameUnreachable, TimeoutError) as unanswered:
            why = str(unanswered) or "the Wake did not return"
            if backoff.left() <= 0:
                raise FrameUnreachable(f"{why}, for {budget} s") from unanswered
        logfire.warn(
            "machine {id}: its computer did not answer the wake ({why}); asking again",
            id=str(machine_id),
            why=why,
        )
        await backoff.wait(WAKE_SLOW)


async def create_with_room(
    frame: frames.FrameDriver, machine_id: uuid.UUID, *, wait_s: int, on_phase: Phase | None, **create: Any
) -> None:
    """`create`, asked again while a host has no room, as `wake_native` asks a Wake: a
    computer the asleep cap released rebuilds through here, and room comes back when one sleeps.
    A provider that refuses before it keeps the key makes a retry a new create. Past `wait_s`, `HostFull`
    raises as it always did: one infra attempt, the checkpoint kept."""
    backoff = Backoff(wait_s, on_phase)
    while True:
        try:
            await frame.create(**create)
            return
        except HostFull as full:
            if backoff.left() <= 0:
                raise
            logfire.warn("machine {id}: no room for it yet ({why})", id=str(machine_id), why=str(full))
        await backoff.wait(WAKE_SLOW)
