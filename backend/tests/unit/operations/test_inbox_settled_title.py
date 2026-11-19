# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A settled task's inbox title says what its answer did: only a done answer reads "Delivered"."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prismoid.db.models.task import Task
from prismoid.operations import inbox
from prismoid.schemas.plan import TaskEnding


@pytest.mark.parametrize(
    ("ending", "title"),
    [
        (TaskEnding.DONE, "Delivered: Draft the memo"),
        (TaskEnding.PARTIAL, "Partly done: Draft the memo"),
        (TaskEnding.COULD_NOT, "Couldn't do it: Draft the memo"),
    ],
)
async def test_the_title_wears_the_ending(
    monkeypatch: pytest.MonkeyPatch, ending: TaskEnding, title: str
) -> None:
    knocked: dict[str, Any] = {}

    async def knock(session: object, task: Task, **fields: Any) -> Any:
        knocked.update(fields)
        return None

    monkeypatch.setattr(inbox, "_knock", knock)
    task = Task(id=uuid.uuid4(), goal="Draft the memo", inputs=[])
    await inbox.task_completed(None, task, "summary", ending)  # pyright: ignore[reportArgumentType]
    assert knocked["title"] == title
    assert knocked["payload"] == {"ending": ending.value}
