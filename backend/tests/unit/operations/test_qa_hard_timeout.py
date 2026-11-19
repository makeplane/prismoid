# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The judge's HARD timeout (ADR-034 live finding): a seat that ignores cancellation must not
hold the gate - `_hard_timeout` abandons it at the deadline instead of waiting for it."""

from __future__ import annotations

import asyncio
import time

import pytest

from prismoid.operations.deadline import hard_timeout as _hard_timeout


async def _stubborn() -> str:
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        await asyncio.sleep(30)  # a provider stream that keeps going after cancel
        raise
    return "never"


async def _prompt() -> str:
    return "report"


async def test_hard_timeout_abandons_a_seat_that_ignores_cancel() -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _hard_timeout(_stubborn(), 0.2)
    assert time.monotonic() - started < 2.0, (
        "the gate must degrade at the deadline, not when the seat feels like it"
    )


async def test_hard_timeout_returns_a_prompt_result() -> None:
    assert await _hard_timeout(_prompt(), 1.0) == "report"
