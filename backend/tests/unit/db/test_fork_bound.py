# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A fork reads its parent up to the seq it branched at (`task.created.parent_seq`): the
parent's later turns are the other branch. A fork made before that field existed carries none and
reads its parent whole, as every fork did then. No user path mints such a fork any more - `fork`
always stamps the bound - so the legacy reading is pinned here, on `render_thread`'s wiring: which
bound each ancestor is read with. The bound's effect on real rows is test_followup_context's."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.operations import thread as thread_ops
from prismoid.schemas.events import TaskCreated
from prismoid.schemas.ids import TaskId
from prismoid.schemas.plan import TaskMode

_AT = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


class _Session:
    """Only what the walk touches: the reader's row."""

    async def get(self, _model: object, _id: object) -> object:
        return object()


async def _bounds(monkeypatch: pytest.MonkeyPatch, *, parent_seq: int | None) -> dict[str, int | None]:
    """Render a fork's thread and return the `until` each task's events were read with."""
    creator = uuid.uuid4()
    parent = Task(id=uuid.uuid4(), creator_actor_id=creator, parent_task_id=None)
    child = Task(id=uuid.uuid4(), creator_actor_id=creator, parent_task_id=parent.id)
    read: dict[uuid.UUID, int | None] = {}

    async def get_for(_session: object, task_id: uuid.UUID, _reader: object) -> Task:
        assert task_id == parent.id
        return parent

    async def latest_of_type(_session: object, task_id: uuid.UUID, _type: str) -> TaskCreated:
        return TaskCreated(
            seq=1, task_id=TaskId(task_id), at=_AT, goal="g", mode=TaskMode.STANDARD, parent_seq=parent_seq
        )

    async def of_types(
        _session: object, task_id: uuid.UUID, _types: Any, *, until: int | None = None
    ) -> list[Any]:
        read[task_id] = until
        return [TaskCreated(seq=1, task_id=TaskId(task_id), at=_AT, goal="g", mode=TaskMode.STANDARD)]

    async def no_prelude(*_a: object, **_k: object) -> list[Any]:
        return []

    monkeypatch.setattr(Task, "get_for", get_for)
    monkeypatch.setattr(TaskEvent, "latest_of_type", latest_of_type)
    monkeypatch.setattr(TaskEvent, "of_types", of_types)
    monkeypatch.setattr(thread_ops, "conversation_prelude", no_prelude)
    await thread_ops.render_thread(_Session(), child, budget_tokens=4000)  # pyright: ignore[reportArgumentType]
    return {"parent": read[parent.id], "child": read[child.id]}


async def test_a_fork_reads_its_parent_up_to_the_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _bounds(monkeypatch, parent_seq=7) == {"parent": 7, "child": None}


async def test_a_fork_made_before_the_bound_reads_its_parent_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _bounds(monkeypatch, parent_seq=None) == {"parent": None, "child": None}
