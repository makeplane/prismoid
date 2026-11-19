# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The namer never holds the plan: `intake_task` answers (it asks nothing) while the namer is still naming, and the turn's closing `title_task` waits for that same namer instead of asking the
model twice. Stubbed operations - no database, no model, no DBOS runtime."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from prismoid.jobs import effects
from prismoid.operations import planning

TASK = "00000000-0000-0000-0000-0000000000aa"


class Namer:
    """A namer that names only when released, counting its calls."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, session: object, task_id: object) -> str | None:
        self.calls += 1
        await self.release.wait()
        return "A Title"


@pytest.fixture
def namer(monkeypatch: pytest.MonkeyPatch) -> Namer:
    @contextlib.asynccontextmanager
    async def scope(model: object, row_id: object) -> AsyncGenerator[Any]:
        class _Session:
            async def commit(self) -> None:
                return None

        yield _Session()

    async def voice(task_id: str) -> Any:
        async def say(*args: object, **kwargs: object) -> None:
            return None

        return say

    n = Namer()
    monkeypatch.setattr(effects, "row_scope", scope)
    monkeypatch.setattr(effects, "_phase_voice", voice)
    monkeypatch.setattr(planning, "generate_title", n)
    return n


async def test_intake_answers_before_the_namer_finishes(namer: Namer) -> None:
    intake = inspect.unwrap(effects.intake_task)
    gate = await asyncio.wait_for(intake(TASK), timeout=1.0)  # the namer is still blocked
    assert gate is None
    await asyncio.sleep(0)
    assert namer.calls == 1  # it IS naming - beside the plan, not before it
    namer.release.set()
    title = inspect.unwrap(effects.title_task)
    assert await asyncio.wait_for(title(TASK), timeout=1.0) == "A Title"
    assert namer.calls == 1  # the closing title waited for that namer: one model call, never two


async def test_the_closing_title_names_when_no_namer_is_running(namer: Namer) -> None:
    # A recovered workflow (intake replayed from the journal, no namer in this process) or an
    # express turn: the closing effect names the task itself - the title stays durable.
    namer.release.set()
    title = inspect.unwrap(effects.title_task)
    assert await title(TASK) == "A Title"
    assert namer.calls == 1
