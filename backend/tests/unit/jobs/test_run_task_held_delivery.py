# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A delivery a pause overtook holds (ADR-064): `deliver` reads the row under its lock and answers
`held` for a turn paused after the workflow's last boundary read; the workflow holds at the
boundary until the resume and asks for the delivery again. The workflow's own body, every effect
stubbed - no database, no DBOS runtime."""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest

from prismoid.jobs import durable, effects
from prismoid.jobs import run_task as run_task_job
from prismoid.operations.steering import STEER_TOPIC


async def test_a_held_delivery_holds_for_the_resume_then_delivers(monkeypatch: pytest.MonkeyPatch) -> None:
    verdicts = ["held", "delivered"]
    asked: list[str] = []
    resumed: list[str] = []
    paused = False  # the row, as the stubbed effects read it

    async def deliver_task(tid: str, summary: str, turn: int = 1) -> dict[str, Any]:
        nonlocal paused
        asked.append(summary)
        verdict = verdicts.pop(0)
        paused = verdict == "held"  # the pause the delivery found under its lock
        return {"verdict": verdict, "replaced": []}

    async def is_paused(tid: str) -> bool:
        return paused

    async def wait(topic: str, timeout_s: float) -> dict[str, Any]:
        nonlocal paused
        paused = False  # Ada resumes
        resumed.append(topic)
        return {"resume": True}

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    async def no(*args: object, **kwargs: object) -> bool:
        return False

    async def empty(*args: object, **kwargs: object) -> list[str]:
        return []

    for name, stub in {
        "mark_dequeued": nothing,
        "turn_is_express": no,
        "plan_task": nothing,
        "mark_running": nothing,
        "fanout_enabled": no,
        "frontier": empty,
        "pause_held": is_paused,
        "paused": is_paused,
        "is_cancelled": no,
        "deliver_task": deliver_task,
        "conversation_report": nothing,
        "remember_task": nothing,
    }.items():
        monkeypatch.setattr(effects, name, stub)
    monkeypatch.setattr(durable, "wait", wait)

    body = inspect.unwrap(run_task_job.run_task)  # the workflow's own body, under DBOS's wrappers
    assert await body("t-1", 2) == "delivered"  # turn 2: no intake round

    assert resumed == [STEER_TOPIC], "the boundary held the turn until the resume"
    assert len(asked) == 2, "the held delivery was asked again after it"


async def test_a_wave_held_by_a_pause_reads_the_frontier_again_after_the_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pause holds the turn at the frontier; while it holds, an editor's replan (legal at a
    boundary) replaces the unstarted steps. The resume dispatches the frontier as it is now, never
    the ids read before the pause - a deleted step dispatched fails the whole task."""
    paused = True
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    plan = [old]  # the unstarted steps, as the stubbed frontier reads them
    dispatched: list[str] = []

    async def frontier(tid: str) -> list[str]:
        return [s for s in plan if s not in dispatched]

    async def is_paused(tid: str) -> bool:
        return paused

    async def wait(topic: str, timeout_s: float) -> dict[str, Any]:
        nonlocal paused, plan
        plan = [new]  # the editor replans while it holds
        paused = False  # then Ada resumes
        return {"resume": True}

    class Handle:
        def __init__(self, step_id: str) -> None:
            self.step_id = step_id

        async def get_result(self, polling_interval_sec: float = 1.0) -> str:
            dispatched.append(self.step_id)
            return "the memo"

    class StepQueue:
        async def enqueue_async(self, fn: object, tid: str, step_id: str) -> Handle:
            return Handle(step_id)

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    async def no(*args: object, **kwargs: object) -> bool:
        return False

    async def deliver_task(tid: str, summary: str, turn: int = 1) -> dict[str, Any]:
        return {"verdict": "delivered", "replaced": []}

    for name, stub in {
        "mark_dequeued": nothing,
        "turn_is_express": no,
        "plan_task": nothing,
        "mark_running": nothing,
        "fanout_enabled": no,
        "frontier": frontier,
        "pause_held": is_paused,
        "paused": is_paused,
        "is_cancelled": no,
        "steer_boundary": no,
        "check_budget": nothing,
        "deliver_task": deliver_task,
        "conversation_report": nothing,
        "remember_task": nothing,
    }.items():
        monkeypatch.setattr(effects, name, stub)
    monkeypatch.setattr(durable, "wait", wait)
    monkeypatch.setattr(run_task_job, "step_queue", StepQueue())

    body = inspect.unwrap(run_task_job.run_task)
    assert await body("t-1", 2) == "delivered"
    assert dispatched == [new], "the wave is the plan as it stands after the resume"
