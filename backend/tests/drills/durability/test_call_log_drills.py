# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log's drills on Prismoid's real step (ADR-089): a writer's step on paper through the real
`execute_step`, and through the real `run_step` workflow under DBOS, each attempt its own process
(`tests/drills/durability/rig.py`) killed with SIGKILL where the drill says. Only the model is scripted; the
provider's ledger is a file, so what was bought survives the kill. The overhead drill measures the
backend's own time per unit on the real Postgres store."""

from __future__ import annotations

import statistics
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.server.fastmcp import FastMCP
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.durable_exec import DurableOperationId
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import FunctionToolset

from prismoid import storage
from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import LOG_VERSION, CallLogScope, PrismoidBackend, PrismoidDurability
from prismoid.ai.capabilities.tool_record import UNKNOWN_REASON, ToolRecorder
from prismoid.ai.deps import CallEntry, Generation, RunKey, TaskDeps
from prismoid.conf import settings
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations.call_log import PgCallLog
from prismoid.schemas.ids import StepId
from tests.support.rigs.durability_drill import REQUESTS, Killed, World, bought, rig
from tests.support.rigs.machine_execution_rig import task_with_steps
from tests.support.runners import PLAIN

pytestmark = [pytest.mark.integration, pytest.mark.drill, pytest.mark.xdist_group("call-log-drills")]


async def _step(step_id: uuid.UUID, task_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        session.expunge(step)
        return step


async def _log(task_id: uuid.UUID) -> PgCallLog:
    """The call log of the task's workspace."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return PgCallLog(task.workspace_id)


async def test_an_uninterrupted_step_buys_each_request_once(tmp_path: Path) -> None:
    """The control every drill is measured against: one pass, five requests, done on attempt 1."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    code, said = rig("step", task_id, step_id, ledger)
    step = await _step(step_id, task_id)
    assert code == 0 and step.state == "done" and step.attempt == 1, said
    assert bought(ledger) == REQUESTS


@pytest.mark.parametrize("k", range(1, REQUESTS))
async def test_a_sigkill_after_request_k_rebuys_nothing(k: int, tmp_path: Path) -> None:
    """Drill D1: the step re-enters the attempt it died in, replays what it bought and goes on."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    code, _ = rig("step", task_id, step_id, ledger, "--die-before", str(k + 1))
    assert code == -9 and bought(ledger) == k
    code, said = rig("step", task_id, step_id, ledger)
    step = await _step(step_id, task_id)
    assert code == 0 and step.state == "done", said
    assert bought(ledger) == REQUESTS, "no request was bought twice"
    assert step.attempt == 1, "a crash re-enters its attempt; it never counts a new one"


async def test_a_sigkill_inside_a_request_rebuys_that_one_request(tmp_path: Path) -> None:
    """Drill D1b: the provider answered request 3 and the process died before its row was written.
    That request, and only that one, is bought again: the window the log cannot close."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    code, _ = rig("step", task_id, step_id, ledger, "--die-inside", "3")
    assert code == -9
    rig("step", task_id, step_id, ledger)
    assert (await _step(step_id, task_id)).state == "done"
    assert bought(ledger) == REQUESTS + 1


async def test_a_step_logged_under_another_version_starts_fresh_told_what_never_reported(
    tmp_path: Path,
) -> None:
    """Drill D6: the step dies inside its second read, after the read happened; the next pass runs
    code of the next `LOG_VERSION`. It never replays the old generation: it opens the next one, told
    which call never reported an outcome, and buys the step again from its first request."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    code, _ = rig("step", task_id, step_id, ledger, "--die-in-read", "2")
    assert code == -9 and bought(ledger) == 2
    code, said = rig("step", task_id, step_id, ledger, "--log-version", str(LOG_VERSION + 1))
    assert code == 0 and (await _step(step_id, task_id)).state == "done", said
    assert bought(ledger) == 2 + REQUESTS, "the old generation was not replayed"
    scope = await CallLogScope.open(
        await _log(task_id), StepId(step_id), fresh=False, version=LOG_VERSION + 1
    )
    assert scope.key.generation == 1
    assert scope.fence_note is not None and "notes-2.md" in scope.fence_note


def _operations(step_id: uuid.UUID) -> list[str]:
    """The workflow's journaled operations, in order, from the engine's system database."""
    base = settings.worker.database_url
    with psycopg.connect(f"{base}_dbos_sys") as db:
        rows = db.execute(
            "select function_name from dbos.operation_outputs where workflow_uuid = %s order by function_id",
            (f"step-{step_id}",),
        ).fetchall()
    return [str(row[0]) for row in rows]


async def test_under_dbos_a_kill_at_request_3_recovers_with_five_requests(tmp_path: Path) -> None:
    """The workflow body replays on recovery; the agent's calls are not DBOS steps, so the body re-enters
    the call log and buys nothing twice. The engine's journal matches an uncrashed run's."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    code, _ = rig("dbos", task_id, step_id, ledger, "--start", "--die-before", "4")
    assert code == -9 and bought(ledger) == 3
    code, said = rig("dbos", task_id, step_id, ledger)
    assert code == 0 and said == "RESULT done", said
    assert bought(ledger) == REQUESTS

    calm_task, (calm_step,) = await task_with_steps(1, agent="writer")
    rig("dbos", calm_task, calm_step, tmp_path / "calm.log", "--start")
    assert _operations(step_id) == _operations(calm_step)


# ── compaction ────────────────────────────────────────────────────────────────────────────────


def _usage_rows(task_id: uuid.UUID) -> int:
    """The task's UsageEvents in the ledger."""
    with psycopg.connect(settings.worker.database_url) as db:
        row = db.execute("select count(*) from usage_events where task_id = %s", (task_id,)).fetchone()
    return int(row[0]) if row else 0


async def test_a_sigkill_after_a_handoff_resumes_on_the_logged_summary(tmp_path: Path) -> None:
    """A long writer's step hands off, its summary logged, and dies as the first request
    carrying the handoff is sent. The re-entered attempt replays onto the same compacted history,
    reads the logged summary instead of calling the summarizer again, buys nothing twice and bills
    each call it made exactly once, as a calm run of the same step does."""
    calm_task, (calm_step,) = await task_with_steps(1, agent="writer")
    calm = tmp_path / "calm" / "provider.log"
    calm.parent.mkdir()
    code, said = rig("step", calm_task, calm_step, calm, "--compact")
    assert code == 0 and (await _step(calm_step, calm_task)).state == "done", said
    calm_summaries = bought(calm.with_name("summaries.log"))
    assert calm_summaries >= 1, "the calm run handed off"

    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    summaries = ledger.with_name("summaries.log")
    code, _ = rig("step", task_id, step_id, ledger, "--compact", "--die-at-handoff")
    assert code == -9 and bought(summaries) == 1, "it died after its first summary"
    died_at = bought(ledger)
    code, said = rig("step", task_id, step_id, ledger, "--compact")
    step = await _step(step_id, task_id)

    assert code == 0 and step.state == "done" and step.attempt == 1, said
    assert bought(ledger) == bought(calm) > died_at, "no request was bought twice"
    assert bought(summaries) == calm_summaries, "the logged summary was read back, never bought again"
    assert _usage_rows(task_id) == bought(ledger) + bought(summaries) == _usage_rows(calm_task), (
        "one UsageEvent per call the provider served, none twice"
    )


# ── overhead ──────────────────────────────────────────────────────────────────────────────────

LOOKS = 19  # 19 tool turns and the answer: a 20-request step


def _crm(tools: int, served: list[str] | None = None) -> MCPToolset[Any]:
    """A fake connector of `tools` read tools, served in process; `served` collects each call."""
    server = FastMCP("crm")
    for n in range(tools):

        def read(record: str, n: int = n) -> str:
            if served is not None:
                served.append(record)
            return f"record {record} from table {n}"

        server.tool(name=f"read_{n:02d}", description=f"Read a record from CRM table {n}. " * 4)(read)
    return MCPToolset(server, id="crm")


def _timed(overheads: list[float]) -> Callable[..., Awaitable[object]]:
    """`PrismoidBackend.execute`, timing what it adds around the unit's own body."""
    original = PrismoidBackend.execute

    async def execute(
        self: PrismoidBackend,
        *,
        operation_id: DurableOperationId,
        name: str,
        body: Callable[[], Awaitable[object]],
        cache_key: tuple[object, ...],
        config: None,
    ) -> object:
        inside = 0.0

        async def timed_body() -> object:
            nonlocal inside
            started = time.perf_counter()
            try:
                return await body()
            finally:
                inside += time.perf_counter() - started

        started = time.perf_counter()
        result = await original(
            self, operation_id=operation_id, name=name, body=timed_body, cache_key=cache_key, config=config
        )
        overheads.append((time.perf_counter() - started - inside) * 1000)
        return result

    return execute


@pytest.mark.perf  # wall-clock budget, skewed by -n neighbours: run alone with `-m perf` on this file
@pytest.mark.parametrize("connector", [False, True])
async def test_the_call_log_costs_under_its_budget_per_unit(
    connector: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R1: p50 <= 5 ms and p95 <= 20 ms of backend time per unit on the real store; a connector's
    inventory read at every model step costs one full row, and nothing goes to the blob store."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    log = await _log(task_id)
    blobs: list[str] = []
    real_put = storage.aput_file

    async def counted_put(key: str, data: Any) -> Any:
        blobs.append(key)
        return await real_put(key, data)

    monkeypatch.setattr(storage, "aput_file", counted_put)
    overheads: list[float] = []
    monkeypatch.setattr(PrismoidBackend, "execute", _timed(overheads))

    world = World([[("look", {"query": f"q{n}"})] for n in range(LOOKS)])
    looks: FunctionToolset[Any] = FunctionToolset(id="drill")

    def look(query: str) -> str:
        """Look one thing up."""
        return f"saw {query}"

    looks.add_function(look)
    agent = Agent(
        world.model(),
        name="drill",
        deps_type=TaskDeps,
        toolsets=[looks, _crm(30)] if connector else [looks],
        output_type=[str, DeferredToolRequests],
        capabilities=[PrismoidDurability()],
    )
    scope = await CallLogScope.open(log, StepId(step_id), fresh=False)
    await AgentRunner(agent, TaskDeps(step_id=StepId(step_id)), PLAIN).run("look it all up", log=scope)

    p50 = statistics.median(overheads)
    p95 = statistics.quantiles(overheads, n=20)[-1]
    with capsys.disabled():
        print(
            f"\ncall log overhead (connector={connector}): units={len(overheads)} p50={p50:.2f} ms p95={p95:.2f} ms"
        )
    assert world.provider_calls == LOOKS + 1
    assert blobs == [], "a 30-tool inventory stays in the row; its repeats are refs"
    assert p50 <= 5 and p95 <= 20


def _inventory_rows(step_id: uuid.UUID) -> list[tuple[bool, bool]]:
    """The step's logged connector inventory reads: (held in the row, held as a ref to an earlier one)."""
    with psycopg.connect(settings.worker.database_url) as db:
        rows = db.execute(
            "select payload is not null, payload_ref is not null from call_log"
            " where step_id = %s and unit like %s and phase = 'result' order by at",
            (step_id, "%crm%get_tools%"),
        ).fetchall()
    return [(bool(full), bool(ref)) for full, ref in rows]


async def test_a_30_tool_inventory_is_logged_once_and_replays_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drill D8: a 20-request step over a 30-tool connector dies as its last request is sent. Its
    inventory is one row held in full, never in the blob store; the re-entered attempt replays every
    unit, the inventory included, calls no connector tool again and buys only the request it died
    before."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    log, step = await _log(task_id), StepId(step_id)
    blobs: list[str] = []
    real_put = storage.aput_file

    async def counted_put(key: str, data: Any) -> Any:
        blobs.append(key)
        return await real_put(key, data)

    monkeypatch.setattr(storage, "aput_file", counted_put)
    served: list[str] = []
    world = World([[(f"read_{n:02d}", {"record": f"r{n}"})] for n in range(LOOKS)], die_before=LOOKS + 1)

    async def attempt() -> Any:
        agent = Agent(
            world.model(),
            name="drill",
            deps_type=TaskDeps,
            toolsets=[_crm(30, served)],
            output_type=[str, DeferredToolRequests],
            capabilities=[PrismoidDurability()],
        )
        scope = await CallLogScope.open(log, step, fresh=False)
        return await AgentRunner(agent, TaskDeps(step_id=step), PLAIN).run("look it all up", log=scope)

    with pytest.raises(Killed):
        await attempt()
    world.die_before = None
    result = await attempt()
    assert result.output == f"Done after {LOOKS + 1} requests."
    assert world.provider_calls == LOOKS + 1, "nothing bought twice"
    assert served == [f"r{n}" for n in range(LOOKS)], "no connector call ran twice"
    assert _inventory_rows(step_id) == [(True, False)], "one inventory row, in the row itself"
    assert blobs == []


# ── OUTCOME UNKNOWN ───────────────────────────────────────────────────────────────────────────


def _called(task_id: uuid.UUID) -> list[tuple[str, bool, str, str]]:
    """The task's `tool.called` rows, in order: tool, ok, reason, summary."""
    with psycopg.connect(settings.worker.database_url) as db:
        rows = db.execute(
            "select payload->>'tool', (payload->'ok')::boolean, payload->>'denied_reason', payload->>'summary'"
            " from task_events where task_id = %s and type = 'tool.called' order by seq",
            (task_id,),
        ).fetchall()
    return [(str(t), bool(ok), str(reason), str(summary)) for t, ok, reason, summary in rows]


@pytest.mark.parametrize("before_effect", [False, True])
async def test_a_sigkill_inside_a_read_runs_the_read_again(before_effect: bool, tmp_path: Path) -> None:
    """Drill D2 for a read: the step dies inside its second read, after the read happened or before.
    The intent is logged and its result is not; a read changes nothing, so the re-entered attempt
    reads that note again, journals one row per call, and buys nothing twice. (A write the death cuts
    off is OUTCOME UNKNOWN: the connector drill below.)"""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"
    flags = ("--die-in-read", "2", *(("--before-effect",) if before_effect else ()))
    code, _ = rig("step", task_id, step_id, ledger, *flags)
    assert code == -9 and bought(ledger) == 2
    code, said = rig("step", task_id, step_id, ledger)
    assert code == 0 and (await _step(step_id, task_id)).state == "done", said
    assert bought(ledger) == REQUESTS

    reads = (tmp_path / "effects.log").read_text().splitlines()
    again = [] if before_effect else ["/work/notes-2.md"]
    assert reads == ["/work/notes-1.md", *again, *(f"/work/notes-{n}.md" for n in range(2, 5))]
    rows = [row for row in _called(task_id) if row[0] == "frame.read_file"]
    assert [row[3] for row in rows] == [f"/work/notes-{n}.md" for n in range(1, 5)], "one row per call"
    assert UNKNOWN_REASON not in {row[2] for row in rows}


class _DiesBeforeResult:
    """The call log, dying (in process) as the `n`th tool result is about to be written."""

    def __init__(self, log: PgCallLog, n: int) -> None:
        self.log, self.left = log, n

    async def latest(self, step: StepId) -> Generation:
        return await self.log.latest(step)

    async def record(self, key: RunKey, entry: CallEntry) -> None:
        if entry.phase == "result" and "@" in entry.unit:
            self.left -= 1
            if self.left == 0:
                raise Killed
        await self.log.record(key, entry)


async def test_an_mcp_write_killed_before_its_result_is_outcome_unknown_and_never_runs_again() -> None:
    """Drill D2 on a connector: the CRM wrote the second note and the attempt died before the call
    log held its result. The re-entered attempt tells the model OUTCOME UNKNOWN; the note is never
    written twice."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    log = await _log(task_id)
    notes: list[str] = []
    server = FastMCP("crm")

    def crm_create_note(text: str) -> str:
        """Write a note on a contact (a side effect)."""
        notes.append(text)
        return f"note {len(notes)}"

    server.tool(name="crm_create_note")(crm_create_note)
    world = World([[("crm_create_note", {"text": t})] for t in ("a", "b", "c")])
    rows: list[tuple[str, bool, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, ok, denied_reason))

    async def attempt(log: Any) -> Any:
        agent = Agent(
            world.model(),
            name="drill",
            deps_type=TaskDeps,
            toolsets=[MCPToolset(server, id="crm")],
            output_type=[str, DeferredToolRequests],
            capabilities=[ToolRecorder(), PrismoidDurability()],
        )
        scope = await CallLogScope.open(log, StepId(step_id), fresh=False)
        deps = TaskDeps(step_id=StepId(step_id), journal_tool=journal)
        return await AgentRunner(agent, deps, PLAIN).run("note it", log=scope)

    with pytest.raises(Killed):
        await attempt(_DiesBeforeResult(log, 2))
    result = await attempt(log)
    assert notes == ["a", "b", "c"], "the interrupted write never ran again"
    assert world.provider_calls == 4 and result.output == "Done after 4 requests."
    assert rows == [
        ("crm_create_note", True, ""),
        ("crm_create_note", False, UNKNOWN_REASON),
        ("crm_create_note", True, ""),
    ]
