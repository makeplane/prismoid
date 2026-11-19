# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deployment's pause slots (`PRISMOID_STEERING_MAX_PAUSE_PARKS`), held at the moment it matters.

Each pause park holds a queue slot, so only `max_pause_parks` may be open across the deployment;
at the cap a paused step does not park, and the frontier holds the pause. Read only when a step
STARTS, the cap would let every step equipped under it park later - pausing a task with eight
running seats under a cap of four would open eight gates. Two halves, pinned here without a
database: the safe point re-reads the slots when it sees the pause, and the park itself counts
and opens under one lock, so two steps never both take the last slot.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.operations.execution import inbox as inbox_module
from prismoid.operations.execution import parks
from prismoid.operations.execution.deferrals import PauseDeferral
from prismoid.operations.execution.handlers import deferral as handlers
from prismoid.operations.execution.handlers.state import AGAIN, Ladder
from prismoid.operations.execution.inbox import StepInbox
from prismoid.schemas.plan import TaskStatus


class _Session:
    """What the pause doors touch: statements (the lock), a commit, and the task by id."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> None:
        self.statements.append(str(statement))

    async def commit(self) -> None: ...

    async def get(self, model: Any, ident: Any) -> Any:
        return SimpleNamespace(state=TaskStatus.PAUSED, is_terminal=lambda: False)


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A deployment with ONE pause slot; the list is the open pause gates, by step."""
    monkeypatch.setattr(settings.steering, "max_pause_parks", 1)
    opened: list[str] = []

    async def count(session: Any) -> int:
        return len(opened)

    monkeypatch.setattr(Gate, "open_pause_count", count)
    return opened


async def test_a_paused_safe_point_rereads_the_slots(
    monkeypatch: pytest.MonkeyPatch, one_slot: list[str]
) -> None:
    """Equipped while the slot was free, the step must not park once another took it."""

    @asynccontextmanager
    async def scope(model: Any, ident: Any) -> AsyncGenerator[_Session]:
        yield _Session()

    async def nothing(*args: Any, **kwargs: Any) -> tuple[()]:
        return ()

    monkeypatch.setattr(inbox_module, "row_scope", scope)
    monkeypatch.setattr(inbox_module.Steer, "newer_than", nothing)
    monkeypatch.setattr(inbox_module.steering, "notes", nothing)

    inbox = StepInbox(uuid.uuid4(), uuid.uuid4(), 1, told=0)
    assert (await inbox.pending(0)).paused and inbox.park_pause, "a free slot: the pause may park"
    one_slot.append("another step")  # the only slot, taken while this step ran
    assert (await inbox.pending(0)).paused
    assert not inbox.park_pause, "at the cap the step finishes and the frontier holds"


async def test_two_steps_racing_for_the_last_slot_park_once(
    monkeypatch: pytest.MonkeyPatch, one_slot: list[str]
) -> None:
    """Both saw the free slot at their safe points; only the first to the park takes it. The
    second continues in place, its calls told they did not run, and its re-issued calls run."""

    async def held(session: Any, task_id: Any) -> Any:
        return SimpleNamespace(id=task_id, state=TaskStatus.PAUSED, is_terminal=lambda: False)

    async def open_gate(session: Any, task: Any, step: Any, spec: Any, gate: Any) -> str:
        opened.append(step.title)
        return f"asking:{step.title}"

    def park_on_pause(session: Any, task: Any, step: Any, outcome: Any) -> None:
        return None

    async def lifted(session: Any, task: Any, outcome: PauseDeferral) -> dict[str, object]:
        return {"pause_calls": list(outcome.calls), "pause_resumed": True, "cancelled": False}

    opened = one_slot
    monkeypatch.setattr(Task, "held", held)
    monkeypatch.setattr(parks, "open_gate", open_gate)
    monkeypatch.setattr(parks, "park_on_pause", park_on_pause)
    monkeypatch.setattr(parks, "pause_lifted", lifted)

    def ladder(title: str) -> tuple[Ladder, _Session]:
        session = _Session()
        task = SimpleNamespace(id=uuid.uuid4())
        step = SimpleNamespace(id=uuid.uuid4(), title=title)
        return Ladder(session, task, step, None, None, False, False, []), session  # pyright: ignore[reportArgumentType]

    outcome = PauseDeferral(calls=("c1",), told=0)
    first, first_session = ladder("seat one")
    second, _ = ladder("seat two")

    assert await handlers.pause(first, outcome) == "asking:seat one"
    assert await handlers.pause(second, outcome) is AGAIN
    assert opened == ["seat one"], "one slot, one park"
    assert second.resume is not None and second.resume["pause_calls"] == ["c1"]
    assert any("pg_advisory_xact_lock" in s for s in first_session.statements), (
        "the count and the open share one lock, so two parks never both take the last slot"
    )
