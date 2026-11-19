# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The step workflow waits out a paced rung durably: the ladder hands it the rest of
`wait_s` when a step could not get its Machine at once, and the workflow sleeps on DBOS's clock -
a restart resumes the wait, it never restarts or skips it - before it runs the ladder again."""

from __future__ import annotations

import contextlib
import inspect
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from prismoid.jobs import durable, effects
from prismoid.operations import execution


async def test_a_paced_rung_is_waited_out_on_dbos_then_the_ladder_runs_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = iter([f"{execution.PACING}598", "(published)"])
    slept: list[float] = []
    counts: list[int | None] = []

    async def ladder(
        session: Any, task_id: Any, step_id: Any, *, seat: bool = False, handed_back: int | None = None
    ) -> str:
        counts.append(handed_back)
        return next(outcomes)

    async def nothing(*args: Any) -> None:
        return None

    @contextlib.asynccontextmanager
    async def scope(*args: Any, **kwargs: Any) -> AsyncGenerator[None]:
        yield None

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(execution, "execute_step", ladder)
    monkeypatch.setattr(effects, "mark_step_dequeued", nothing)
    monkeypatch.setattr(effects, "row_scope", scope)
    monkeypatch.setattr(durable, "sleep", sleep)
    workflow = inspect.unwrap(effects.execute_step)  # the body, under DBOS's two wrappers
    assert await workflow(str(uuid.uuid4()), str(uuid.uuid4())) == "(published)"
    assert slept == [598.0]
    assert counts == [0, 1], "the ladder is told how many hand-backs this run of the body took"
