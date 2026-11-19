# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A parked turn, frozen and resumed: the durable rows a park leaves behind are the contract a
recovered workflow reads, so a change to how a park is saved must still resume the parks already
sitting in a database.

`freeze` runs a scenario to its first park and stops the worker there (the park's job is cancelled,
as a stopping worker's is); `dump` reads what that left in Postgres - the task, its steps, the open
gate, the record, the connectors it mounted - as plain JSON, which a fixture file keeps. `restore`
puts those rows back under a fresh workspace and person (every id is made new, so a fixture is
never in the way of itself; only the columns the fixture holds are written, so a column added later
takes its default as a migrated row would, and a gate's clock restarts now), and `resume` runs the parked step's workflow again, as the boot sweep
does after a restart: it finds its gate open, parks on it again, is answered, and finishes.

What a fixture holds is the park as the engine saved it on the day it was recorded; what a test holds
the resumed step to is the shape of the record the resume wrote (`shape.shape_of`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import Workspace
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import effects
from prismoid.operations import dispatch
from prismoid.schemas.events import DurableEvent
from tests.contract.harness import rig, shape
from tests.contract.harness.answers import Act, at_park
from tests.contract.harness.people import Room, as_workflow
from tests.contract.harness.rig import Scenario
from tests.contract.harness.shape import UUID
from tests.contract.harness.voices import Reply
from tests.support.cast import member

FIXTURES = Path(__file__).parents[1] / "fixtures"

# What a parked step's workflow reads, in the order it can be put back (a row after the rows it
# points at): the table and the column that ties it to the task, or to the person who owns it.
TABLES: tuple[tuple[str, str], ...] = (
    ("connectors", "owner"),
    ("tasks", "id"),
    ("plan_steps", "task"),
    ("plan_edges", "task"),
    ("machines", "task"),
    ("gates", "task"),
    ("task_events", "task"),
    ("usage_events", "task"),
)

type Rows = dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class Resume:
    """A parked turn and how it comes back: the scenario parked, what the models say after, and the
    act at each park of the resumed step."""

    name: str
    parked: Scenario
    voices: Mapping[str, tuple[Reply, ...]]
    answers: tuple[Act, ...]


async def freeze(parked: Scenario, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Run the scenario to its first park and stop there; the task it left parked."""

    async def stop(room: Room) -> None:
        raise asyncio.CancelledError(str(room.task_id))

    scenario = dataclasses.replace(parked, stage=at_park(stop))
    with pytest.raises(asyncio.CancelledError) as stopped:
        await rig.run_turn(scenario, monkeypatch)
    return uuid.UUID(str(stopped.value.args[0]))


async def dump(task_id: uuid.UUID) -> Rows:
    """The rows a park left, as JSON the database itself writes."""
    async with row_scope(Task, task_id) as session:
        owner = await session.scalar(
            text("select creator_actor_id from tasks where id = :id"), {"id": task_id}
        )
        keys = {"id": task_id, "task": task_id, "owner": owner}
        rows: Rows = {}
        for table, column in TABLES:
            tie = {"id": "id", "task": "task_id", "owner": "owner_actor_id"}[column]
            found = await session.execute(
                text(f"select to_jsonb(t) from {table} t where {tie} = :key order by to_jsonb(t)::text"),
                {"key": keys[column]},
            )
            rows[table] = [row for (row,) in found]
        return rows


async def restore(rows: Rows) -> uuid.UUID:
    """Put the rows back under a fresh workspace and owner, every other id made new; the task."""
    (task,) = rows["tasks"]
    async with session_scope() as session:
        owner, workspace = await member(session, "Resumed")
        fresh: dict[str, str] = {
            task["workspace_id"]: str(workspace.id),
            task["creator_actor_id"]: str(owner.id),
        }
        body = json.loads(
            UUID.sub(lambda m: fresh.setdefault(m.group(), str(uuid.uuid4())), json.dumps(rows))
        )
        for table, _ in TABLES:
            if not body[table]:
                continue
            columns = ", ".join(body[table][0])
            await session.execute(
                text(
                    f"insert into {table} ({columns}) "
                    f"select {columns} from jsonb_populate_recordset(null::{table}, :rows)"
                ),
                {"rows": json.dumps(body[table])},
            )
        await session.execute(
            text("update gates set expires_at = now() + (expires_at - created_at) where task_id = :id"),
            {"id": fresh[task["id"]]},
        )
        await session.commit()
    return uuid.UUID(fresh[task["id"]])


async def resume(
    resume: Resume, monkeypatch: pytest.MonkeyPatch, task_id: uuid.UUID, seen: int
) -> tuple[str, list[DurableEvent]]:
    """The parked step's workflow run again and answered: the step's state at its end, and the record
    the resume wrote after the `seen` events the fixture held."""
    scenario = dataclasses.replace(resume.parked, voices=resume.voices, stage=at_park(*resume.answers))
    cast = rig.install(monkeypatch, scenario)
    room = Room(scenario.stage)
    async with row_scope(Task, task_id) as session:
        step_id = await session.scalar(
            text("select step_id from gates where task_id = :id and state = 'open'"), {"id": task_id}
        )
        task = await session.get(Task, task_id)
        assert task is not None
        owner = await session.get(Actor, task.creator_actor_id)
        workspace = await session.get(Workspace, task.workspace_id)
        assert owner is not None and workspace is not None
        await room.seat(session, owner, workspace)
    with room.present(task_id, monkeypatch):
        step = inspect.unwrap(effects.execute_step)(str(task_id), str(step_id), False)
        await as_workflow(dispatch.step_job_key(step_id), step)
    cast.check()
    async with row_scope(Task, task_id) as session:
        state = await session.scalar(text("select state from plan_steps where id = :id"), {"id": step_id})
    events, _ = await rig.record_of(task_id, since=seen)
    return str(state), events


def read(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def write(name: str, rows: Rows, resumed: shape.Shape) -> None:
    """The parked rows, then the shape their resume wrote: one file, so the pair cannot drift."""
    FIXTURES.mkdir(exist_ok=True)
    (FIXTURES / f"{name}.json").write_text(json.dumps({"resumed": resumed, "parked": rows}, indent=1) + "\n")
