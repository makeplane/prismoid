# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log's store: a step attempt's units encrypted at rest, a duplicate a no-op, a repeated
payload stored once per generation, a large one in the blob store, and every row gone at each
terminal transition of its task (delivered, failed, cancelled, and a run stopped after the end).

The real `PgCallLog` on real rows, the blob store on the local disk."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue
from sqlalchemy import text

from prismoid import storage
from prismoid.ai.deps import CallEntry, RunKey
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import call_log, execution, steering
from prismoid.operations import tasks as task_ops
from prismoid.operations.call_log import PgCallLog
from prismoid.operations.execution.handlers import produce
from prismoid.operations.execution.handlers.state import Ladder, Ran
from prismoid.schemas.call_log import CallPhase
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration

SECRET = "the-plaintext-marker-7f3a"


@dataclass
class Running:
    actor: Actor
    task_id: uuid.UUID
    step: StepId
    workspace_id: uuid.UUID
    log: PgCallLog


@pytest.fixture
def blobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings.worker, "storage_backend", "fs")
    monkeypatch.setattr(settings.local, "home", tmp_path)
    return tmp_path


@pytest.fixture
async def running(silent_dispatcher: object, blobs: Path) -> AsyncIterator[Running]:
    """Ada's running task with one running step, and the step's call log."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="a memo", clarify="never")
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        step = PlanStep(task_id=task.id, ordinal=0, title="write it", state=StepState.RUNNING.value)
        s.add(step)
        await s.commit()
        yield Running(ada, task.id, StepId(step.id), workspace.id, PgCallLog(workspace.id))


def _key(run: Running, generation: int = 0) -> RunKey:
    return RunKey(step=run.step, generation=generation, log_version=1)


async def _rows(step: uuid.UUID) -> list[dict[str, object]]:
    async with session_scope() as s:
        found = await s.execute(text("select * from call_log where step_id = :s order by at"), {"s": step})
        return [dict(row) for row in found.mappings()]


def _blob_files(home: Path) -> list[Path]:
    return [p for p in home.rglob("*") if p.is_file() and "call_log" in p.parts]


def _inventory(tools: int = 30) -> JsonValue:
    """A connector's tool list: about 40 KB of names, descriptions and schemas."""
    return {
        "tools": [
            {
                "name": f"crm_tool_{i}",
                "description": f"Tool {i}. " + "Reads or writes records in the CRM. " * 30,
                "parameters": {
                    "type": "object",
                    "properties": {f"arg_{j}": {"type": "string"} for j in range(8)},
                },
            }
            for i in range(tools)
        ]
    }


async def test_a_payload_round_trips_encrypted(running: Running) -> None:
    call: JsonValue = {"args": {"query": SECRET}, "ok": True}
    await running.log.record(_key(running), CallEntry("crm.search@call_1", CallPhase.INTENT, call))
    await running.log.record(
        _key(running), CallEntry("model#0", CallPhase.RESULT, {"text": SECRET}, "0123456789abcdef")
    )

    rows = await _rows(running.step)
    assert len(rows) == 2
    for row in rows:
        assert isinstance(row["payload"], bytes) and SECRET.encode() not in row["payload"]
    latest = await running.log.latest(running.step)
    assert latest.key == _key(running)
    assert latest.entries[("crm.search@call_1", "intent")].payload == call
    assert latest.entries[("model#0", "result")].fingerprint == "0123456789abcdef"


async def test_a_duplicate_insert_is_a_no_op(running: Running) -> None:
    await running.log.record(_key(running), CallEntry("model#0", CallPhase.RESULT, {"text": "first"}))
    await running.log.record(_key(running), CallEntry("model#0", CallPhase.RESULT, {"text": "second"}))

    assert len(await _rows(running.step)) == 1
    latest = await running.log.latest(running.step)
    assert latest.entries[("model#0", "result")].payload == {"text": "first"}, "the first row stands"


async def test_an_inventory_read_at_every_model_step_is_stored_once(running: Running) -> None:
    inventory = _inventory()
    for n in range(20):
        await running.log.record(_key(running), CallEntry(f"crm.get_tools#{n}", CallPhase.RESULT, inventory))

    rows = await _rows(running.step)
    assert sum(row["payload"] is not None for row in rows) == 1
    assert sum(row["payload_ref"] is not None for row in rows) == 19
    assert not any(row["blob_ref"] for row in rows)
    latest = await PgCallLog(running.workspace_id).latest(running.step)
    assert len(latest.entries) == 20
    assert all(entry.payload == inventory for entry in latest.entries.values()), "a fresh store resolves refs"


async def test_only_the_latest_generation_is_read(running: Running) -> None:
    await running.log.record(_key(running, 0), CallEntry("model#0", CallPhase.RESULT, "old"))
    await running.log.record(_key(running, 1), CallEntry("model#0", CallPhase.RESULT, "new"))

    latest = await running.log.latest(running.step)
    assert latest.key is not None and latest.key.generation == 1
    assert [e.payload for e in latest.entries.values()] == ["new"]


async def test_a_payload_over_64_kb_goes_to_the_blob_store(running: Running, blobs: Path) -> None:
    screenshot: JsonValue = {"image": SECRET + "x" * (70 * 1024)}
    await running.log.record(_key(running), CallEntry("screen.capture@call_9", CallPhase.RESULT, screenshot))

    (row,) = await _rows(running.step)
    assert row["payload"] is None and str(row["blob_ref"]).startswith(f"call_log/{running.step}/")
    stored = _blob_files(blobs)
    assert len(stored) == 1 and SECRET.encode() not in stored[0].read_bytes(), "the blob is ciphertext"
    latest = await running.log.latest(running.step)
    assert latest.entries[("screen.capture@call_9", "result")].payload == screenshot

    async with session_scope(running.workspace_id) as s:
        await call_log.forget(s, running.task_id)
        assert _blob_files(blobs), "the blob stays until the transaction commits"
        await s.commit()
    await call_log.flush_sweeps()
    assert not _blob_files(blobs), "forget takes the blob once committed"


async def _blob_logged(run: Running, blobs: Path) -> None:
    await run.log.record(_key(run), CallEntry("screen.capture@call_9", CallPhase.RESULT, "x" * (70 * 1024)))
    assert len(_blob_files(blobs)) == 1


async def test_a_forget_rolled_back_keeps_its_rows_and_blobs(running: Running, blobs: Path) -> None:
    await _blob_logged(running, blobs)
    async with session_scope(running.workspace_id) as s:
        await call_log.forget(s, running.task_id)
        await s.rollback()
        await s.commit()  # a later commit on the same session sweeps nothing the rollback kept
    await call_log.flush_sweeps()

    assert len(await _rows(running.step)) == 1
    assert len(_blob_files(blobs)) == 1
    latest = await running.log.latest(running.step)
    assert latest.entries[("screen.capture@call_9", CallPhase.RESULT)].payload == "x" * (70 * 1024)


async def test_a_store_outage_at_commit_never_fails_the_transition(
    running: Running, blobs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _blob_logged(running, blobs)

    async def down(prefix: str) -> None:
        raise ConnectionError("the store is down")

    monkeypatch.setattr(storage, "adelete_prefix", down)
    async with session_scope(running.workspace_id) as s:
        await execution.fail(s, running.task_id, "it broke")
        await s.commit()
    await call_log.flush_sweeps()

    assert await _rows(running.step) == []
    async with session_scope(running.workspace_id) as s:
        task = await s.get(Task, running.task_id)
        assert task is not None and task.status == TaskStatus.FAILED.value
    assert len(_blob_files(blobs)) == 1, "the orphaned blob is only a storage cost"


async def _logged(run: Running) -> None:
    await run.log.record(_key(run), CallEntry("model#0", CallPhase.RESULT, {"text": SECRET}))
    assert len(await _rows(run.step)) == 1


async def test_rows_are_gone_once_the_task_is_delivered(running: Running) -> None:
    await _logged(running)
    async with session_scope(running.workspace_id) as s:
        step = await s.get(PlanStep, running.step)
        assert step is not None
        step.state = StepState.DONE.value
        await s.flush()
        await execution.deliver(s, running.task_id, "done")
        await s.commit()
    assert await _rows(running.step) == []


async def test_rows_are_gone_once_the_task_fails(running: Running) -> None:
    await _logged(running)
    async with session_scope(running.workspace_id) as s:
        await execution.fail(s, running.task_id, "it broke")
        await s.commit()
    assert await _rows(running.step) == []


async def test_rows_are_gone_once_the_task_is_cancelled(running: Running) -> None:
    await _logged(running)
    async with session_scope(running.workspace_id) as s:
        actor = await s.get(Actor, running.actor.id)
        assert actor is not None
        await steering.cancel(s, actor, running.task_id)
    assert await _rows(running.step) == []


async def test_rows_a_stopped_run_logs_after_the_end_are_gone(running: Running) -> None:
    """A gate's cancel ends the task (`advance`); the run still logs a unit before its safe point
    stops it, and the stopped step takes that row with it."""
    await _logged(running)
    async with session_scope(running.workspace_id) as s:
        await execution.advance(s, running.task_id, TaskStatus.CANCELLED)
        await s.commit()
    assert await _rows(running.step) == []

    await _logged(running)
    contract = Contract(
        goal="a memo",
        acceptance=["it exists"],
        inputs=[],
        output=OutputRef(type=ArtifactType.DOCUMENT, title="memo"),
    )
    spec = StepSpec(kind=StepKind.STEP, agent=AgentName("writer"), title="write it", contract=contract)
    async with session_scope(running.workspace_id) as s:
        task, step = await s.get(Task, running.task_id), await s.get(PlanStep, running.step)
        assert task is not None and step is not None
        ladder = Ladder(s, task, step, spec, contract, seat=False, seated=False, upstream=[])
        await produce.nothing_published(ladder, Ran(narration="", produced=[], outline=None, undelivered="x"))
    assert await _rows(running.step) == []
