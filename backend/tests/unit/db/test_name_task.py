# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task's title is minted from the person's typed instruction only: a long paste they attached
is their material, never their ask, and never reaches the namer. Stubbed - no database, no model."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.operations import planning
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.outputs import TaskTitle

INSTRUCTION = "Turn this into a launch announcement for the team"
PASTE = TaskInput(filename="pasted-release-notes.txt", sha256="d" * 64, bytes=3000, media_type="text/plain")


class _Session:
    def __init__(self, task: Task) -> None:
        self.task = task

    async def get(self, model: object, row_id: object) -> Task:
        return self.task


async def _title_input(goal: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """The text the namer is handed for a task whose goal is `goal`, carrying a 3k pasted part."""
    seen: list[str] = []

    async def namer(session: Any, task: Any, name: str, brief: str) -> TaskTitle:
        seen.append(brief)
        return TaskTitle(title="Launch announcement")

    async def next_seq(self: Task, session: Any) -> int:
        return 1

    async def append(session: Any, event: Any) -> None:
        return None

    monkeypatch.setattr(planning, "run_light_agent", namer)
    monkeypatch.setattr(Task, "next_seq", next_seq)
    monkeypatch.setattr(TaskEvent, "append", append)
    task = Task(id=uuid.uuid4(), goal=goal, turn=1, mode="standard", inputs=[PASTE.model_dump()])
    assert await planning.generate_title(cast(AsyncSession, _Session(task)), task.id) == "Launch announcement"
    (brief,) = seen
    return brief


async def test_the_title_input_is_the_typed_instruction_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = await _title_input(f"  {INSTRUCTION}​\n", monkeypatch)
    assert brief == f"Name this task.\n\nThe ask:\n{INSTRUCTION}"
    assert PASTE.filename not in brief
