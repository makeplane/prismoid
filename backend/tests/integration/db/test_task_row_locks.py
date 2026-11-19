# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The task row lock: one order, one strength.

**Order.** Every writer that touches a task's gates or memories takes the task's row first -
`gates.resolve` and the timeout sweep did; `expire_open` locked its gates before it claimed the
seq, and memory extraction superseded memories before theirs, while `context.record` goes
task -> memories. Two writers meeting in opposite orders is a deadlock waiting for its moment.

**Strength.** `FOR NO KEY UPDATE`, the mode `next_seq`'s own `UPDATE tasks SET event_seq` takes
(it changes no key column): it still queues against every seq claim, but a child row's insert
(its foreign key's `KEY SHARE` - plan steps, events, usage, gates) is not blocked behind it.

The statements are read off the engine, so what is pinned is what Postgres is actually sent.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.memory import MemoryKind, MemoryScope
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.session import get_engine, session_scope
from prismoid.operations import context as context_ops
from prismoid.operations import gates as gate_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from prismoid.schemas.context import ContextItem, ContextLane, ContextPack
from prismoid.schemas.events import TaskDelivered
from prismoid.schemas.ids import TaskId
from prismoid.schemas.outputs import MemoryExtract, QuotedMemory
from prismoid.schemas.plan import GateAction, GateKind, GateSpec, GateState, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


@contextmanager
def sent() -> Generator[list[str]]:
    """Every SQL statement the engine sends inside the block, whitespace-squashed."""
    seen: list[str] = []
    engine = get_engine().sync_engine

    def _on(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _on)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _on)


_PLAIN_FOR_UPDATE = re.compile(r"(?<!KEY )FOR UPDATE")


def _locks_task(stmt: str) -> bool:
    """A statement that takes the task's row lock: a locking read of `tasks`, or the seq claim."""
    return stmt.startswith("UPDATE tasks ") or ("FROM tasks" in stmt and "FOR NO KEY UPDATE" in stmt)


def _first(stmts: list[str], pred: object) -> int:
    hits = [i for i, s in enumerate(stmts) if pred(s)]  # pyright: ignore[reportCallIssue]
    return hits[0] if hits else len(stmts)


def _task_lock_modes(stmts: list[str]) -> list[str]:
    """The lock clause of every locking read of `tasks` the block sent."""
    return [
        "FOR NO KEY UPDATE" if "FOR NO KEY UPDATE" in s else "FOR UPDATE"
        for s in stmts
        if "FROM tasks" in s and ("FOR NO KEY UPDATE" in s or _PLAIN_FOR_UPDATE.search(s))
    ]


async def _question_gate(s: AsyncSession, task: Task) -> Gate:
    gate = Gate(
        task_id=task.id,
        kind=GateKind.APPROVE.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.APPROVE, title="Allow it?").model_dump(mode="json"),
    )
    s.add(gate)
    await s.flush()
    return gate


async def _ada_and_task(s: AsyncSession) -> tuple[Actor, Task]:
    ada, _ = await member(s, "Ada")
    task = await task_ops.create_task(s, ada, goal="compare three trackers")
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    await s.commit()
    return ada, task


# ── order (M1) ────────────────────────────────────────────────────────────────────────────────


async def test_expire_open_locks_the_task_before_its_gates(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, task = await _ada_and_task(s)
        await _question_gate(s, task)
        await _question_gate(s, task)
        await s.commit()

        with sent() as stmts:
            await gate_ops.expire_open(s, task)
            await s.flush()
        await s.commit()

        # The gates are READ under the task's lock too, not just written after it: a gate read
        # unlocked can be answered (resolve takes the task lock) between the read and the write,
        # and the stale row would then stamp EXPIRED over the answer - the sweep's own race.
        first_gate_read = _first(stmts, lambda x: x.startswith("SELECT") and "FROM gates" in x)
        first_gate_write = _first(stmts, lambda x: x.startswith("UPDATE gates "))
        assert first_gate_write < len(stmts), "control: the gates were written"
        assert _first(stmts, _locks_task) < min(first_gate_read, first_gate_write), stmts
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]


async def test_memory_extract_locks_the_task_before_its_memories(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quote that replaces Ada's row supersedes it (an UPDATE of an existing memory) - the
    row `context.record` bumps under the task lock on the next turn's planning."""

    async def extract(*_: object, **__: object) -> MemoryExtract:
        return MemoryExtract(
            items=[
                QuotedMemory(kind="fact", lasts="standing", quote="The codeword is PINEAPPLE-43", replaces=1)
            ]
        )

    monkeypatch.setattr("prismoid.operations.planning.run_light_agent", extract)
    async with session_scope() as s:
        ada, task = await _ada_and_task(s)
        task.goal = "The codeword is PINEAPPLE-43 now; compare three trackers"
        await memory_ops.create(
            s, ada, scope=MemoryScope.USER, kind=MemoryKind.FACT, text="My codeword is PINEAPPLE-42"
        )
        seq = await task.next_seq(s)
        await TaskEvent.append(
            s, TaskDelivered(seq=seq, task_id=TaskId(task.id), at=utcnow(), summary="Done.")
        )
        await s.commit()

        with sent() as stmts:
            assert await memory_ops.remember(s, task) == 1
        await s.commit()

        first_memory_write = _first(
            stmts, lambda x: x.startswith(("UPDATE memories ", "INSERT INTO memories "))
        )
        assert first_memory_write < len(stmts), "control: memory_extract wrote"
        assert any(x.startswith("UPDATE memories ") for x in stmts), "control: a row was superseded"
        assert _first(stmts, _locks_task) < first_memory_write, stmts
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]


# ── strength (M2) ─────────────────────────────────────────────────────────────────────────────


async def test_resolve_locks_the_task_for_no_key_update(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, task = await _ada_and_task(s)
        gate = await _question_gate(s, task)
        await s.commit()
        with sent() as stmts:
            await gate_ops.resolve(s, ada, gate.id, GateAction.ACCEPT)
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]


async def test_the_timeout_sweep_locks_the_task_for_no_key_update(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, task = await _ada_and_task(s)
        gate = await _question_gate(s, task)
        await s.commit()
        with sent() as stmts:
            assert await gate_ops._resolve_by_timeout(s, gate, GateAction.DECLINE)  # pyright: ignore[reportPrivateUsage]
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]


async def test_record_and_move_lock_the_task_for_no_key_update(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, task = await _ada_and_task(s)
        skills = ContextLane(lane="skills", items=[ContextItem(text="Use the memo skill.")], tokens=6)
        with sent() as stmts:
            await context_ops.record(s, task, ContextPack(lanes=[skills], total_tokens=6))
        await s.commit()
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]

        project = (await project_ops.create(s, ada, name="locks")).project
        with sent() as stmts:
            await steering.move(s, ada, task.id, project.id)
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]


async def test_a_budget_raise_locks_the_task_for_no_key_update(silent_dispatcher: None) -> None:
    """B3: the raise writes `tasks.budget` and claims a seq, so it queues behind every other task
    writer at the same strength - after the role check, never before it."""
    async with session_scope() as s:
        ada, task = await _ada_and_task(s)
        with sent() as stmts:
            await steering.set_budget(s, ada, task.id, Decimal("5"))
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]


async def test_the_row_lock_lets_child_rows_in_and_still_queues_the_seq(silent_dispatcher: None) -> None:
    """Postgres semantics, pinned: while one transaction holds the task row, another can still
    insert a child row (the FK's KEY SHARE is compatible with NO KEY UPDATE) - but its seq claim
    queues, which is what keeps `(task_id, seq)` dense under every writer."""
    async with session_scope() as holder:
        _ada, task = await _ada_and_task(holder)
        workspace_id = tenant.bound_workspace(holder)
        assert workspace_id is not None

        await Task.hold(holder, task.id)  # held, uncommitted
        try:
            async with session_scope(workspace_id) as other:
                await other.execute(text("SET LOCAL lock_timeout = '1s'"))
                other.add(PlanStep(task_id=task.id, ordinal=0, title="a child row"))
                await other.flush()  # would time out behind a plain FOR UPDATE
                with pytest.raises(DBAPIError, match="lock timeout"):
                    await Task.claim_seq(other, task.id)
                await other.rollback()
        finally:
            await holder.rollback()


async def test_an_editors_resolve_locks_the_task_for_no_key_update(silent_dispatcher: None) -> None:
    """The Project path: an editor's answer takes the same lock after the role check."""
    async with session_scope() as s:
        ada, task = await _ada_and_task(s)
        bob, _ = await member(s, "Bob")
        project = (await project_ops.create(s, ada, name="race")).project
        await steering.move(s, ada, task.id, project.id)
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        gate = await _question_gate(s, task)
        await s.commit()
        with sent() as stmts:
            resolution = await gate_ops.resolve(s, bob, gate.id, GateAction.DECLINE)
        assert resolution.resolved_by == "Bob"
        assert _task_lock_modes(stmts) == ["FOR NO KEY UPDATE"]
