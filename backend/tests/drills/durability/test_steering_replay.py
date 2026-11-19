# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Steering's inbox read and the run guard's spend read are logged operations of the call log (ADR-089):
a resumed attempt reads what the dead one read at the same safe point, a failed read included, and a
steer that arrived meanwhile is taken at the first live safe point. Drill D3 runs on Prismoid's real step in
its own process (`tests/drills/durability/rig.py`), killed with SIGKILL; the others run a scripted step on
`AgentRunner` and `PrismoidDurability` over the real Postgres store, the death the scripted provider's own
(`Killed`), so nothing in product code knows."""

from __future__ import annotations

import inspect
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.durable_exec import DurableOperationId
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import (
    CallLogScope,
    PrismoidBackend,
    PrismoidDurability,
    ReplayDiverged,
    active,
)
from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.capabilities.steering import Steering
from prismoid.ai.deps import CallEntry, TaskDeps
from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import steering
from prismoid.operations.call_log import PgCallLog
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import BudgetFigures
from prismoid.schemas.steering import SafePoint, SteerNote, SteerPending
from tests.support.rigs.durability_drill import REQUESTS, Killed, World, notes_and_looks, rig
from tests.support.rigs.machine_execution_rig import task_with_steps
from tests.support.runners import PLAIN

pytestmark = [pytest.mark.integration, pytest.mark.drill, pytest.mark.xdist_group("call-log-drills")]

INBOX = "__capability__steering.inbox.pending#"
SPEND = "__capability__loops.spend#"


def _reads(entries: Mapping[tuple[str, str], CallEntry], unit: str) -> list[Any]:
    """The logged reads of one kind, in the order the run made them: each one's recorded value."""
    found = {
        int(name.rsplit("#", 1)[1]): entry.payload
        for (name, phase), entry in entries.items()
        if unit in name and phase == "result"
    }
    return [cast(dict[str, Any], found[n])["value"] for n in sorted(found)]


def _steers_in(read: dict[str, Any]) -> list[str]:
    pending = read["pending"]
    return [] if pending is None else [s["text"] for s in pending["steers"]]


async def _steer(task_id: uuid.UUID, text: str) -> None:
    """A steer by the task's creator, through the real door."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        actor = await session.get(Actor, task.creator_actor_id)
        assert actor is not None
        await steering.steer(session, actor, task_id, text)


async def test_a_steer_read_before_a_crash_is_seen_at_the_same_point_on_prismoids_step(
    tmp_path: Path,
) -> None:
    """Drill D3 on Prismoid's step: steer A is accepted before a writer's step starts, so the attempt's
    contract carries it; the step is killed as request 4 is sent; steer B arrives. The re-entered
    attempt keeps the epoch and the prompt it started with, replays requests 1..3 as the dead one
    made them (no fence, nothing bought twice) and takes B at the first safe point past the log."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    await _steer(task_id, "A: keep it to one page")
    ledger = tmp_path / "provider.log"
    code, _ = rig("step", task_id, step_id, ledger, "--die-before", "4")
    assert code == -9
    log, step = await _store(task_id), StepId(step_id)
    before = _reads((await log.latest(step)).entries, INBOX)
    await _steer(task_id, "B: also add a chart")

    code, said = rig("step", task_id, step_id, ledger)
    async with row_scope(Task, task_id) as session:
        row = await session.get(PlanStep, step_id)
        assert row is not None and row.state == "done", said
    assert code == 0 and len(ledger.read_text().splitlines()) == REQUESTS, "nothing was bought twice"
    latest = await log.latest(step)
    assert latest.key is not None and latest.key.generation == 0, "the replay never diverged into a fence"
    reads = _reads(latest.entries, INBOX)
    assert reads[: len(before)] == before, "the re-entered attempt read what the dead one read"
    seen = [_steers_in(read) for read in reads]
    prompt = cast(dict[str, Any], latest.entries[("segment#1", "intent")].payload)["line"]
    assert "keep it to one page" in prompt and not any(seen[: len(before)]), "A rode the attempt's contract"
    assert seen[len(before)] == ["B: also add a chart"], "B is taken at the first live safe point"


@dataclass
class _Inbox:
    """A steering inbox in memory; reads named in `fail` raise, as a database blip would."""

    told: int = 0
    park_pause: bool = False
    queue: list[SteerNote] = field(default_factory=list[SteerNote])
    fail: set[int] = field(default_factory=set[int])
    reads: int = 0

    def steer(self, text: str) -> None:
        epoch = len(self.queue) + 1
        self.queue.append(
            SteerNote(command_id=uuid.uuid4(), epoch=epoch, author="Ada", text=text, by_creator=True)
        )

    async def pending(self, after_epoch: int) -> SteerPending:
        self.reads += 1
        if self.reads in self.fail:
            raise RuntimeError("db blip")
        return SteerPending(steers=tuple(s for s in self.queue if s.epoch > after_epoch))

    async def applied(self, steers: Sequence[SteerNote], point: SafePoint) -> None:
        return None

    async def paused_here(self, point: SafePoint) -> None:
        return None


def _agent(world: World, inbox: _Inbox, *, guard: bool = False) -> Agent[TaskDeps, Any]:
    """A scripted step with the safe points mounted the way `build()` mounts them, on the call log."""
    toolset: FunctionToolset[Any] = FunctionToolset(id="drill")

    def note(text: str) -> str:
        """Write a note."""
        world.effects.append(text)
        return f"noted {text}"

    def look(query: str) -> str:
        """Read the notes."""
        return f"saw {query} with {len(world.effects)} notes"

    toolset.add_function(note)
    toolset.add_function(look)
    return Agent(
        world.model(),
        name="drill",
        deps_type=TaskDeps,
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
        capabilities=[
            Steering(inbox),
            *([RunGuard()] if guard else []),
            PrismoidDurability(),
        ],
    )


async def _store(task_id: uuid.UUID) -> PgCallLog:
    """The task's workspace's call log."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return PgCallLog(task.workspace_id)


async def _step() -> tuple[PgCallLog, StepId]:
    """A real step row for the call log to hang off, and its workspace's store."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    return await _store(task_id), StepId(step_id)


async def _run(agent: Agent[TaskDeps, Any], deps: TaskDeps, log: PgCallLog, step: StepId) -> Any:
    """One attempt on the step's latest generation, raw: a divergence raises instead of fencing."""
    scope = await CallLogScope.open(log, step, fresh=False)
    with active(scope):
        return await agent.run("write the notes", deps=deps)


@pytest.mark.parametrize("logged", [True, False])
async def test_a_steer_read_before_a_crash_is_seen_at_the_same_point_after_resume(
    logged: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drill D3: steer A is in the inbox when the step starts, the attempt dies as request 3 is
    sent, and steer B arrives. The resumed attempt replays requests 1..2 with A where the dead one
    had it, buys nothing twice, and takes B at the first safe point past the log, never earlier.
    The control: with the read off the log, the replay reads the live inbox, meets B at request 1,
    and the fingerprint catches it as `ReplayDiverged` - never a silent fork."""
    if not logged:
        monkeypatch.setattr(Steering, "_read", inspect.unwrap(vars(Steering)["_read"]))
    log, step = await _step()
    world, inbox = World(notes_and_looks(), die_before=3), _Inbox()
    inbox.steer("A: keep it short")
    agent = _agent(world, inbox)
    deps = TaskDeps(step_id=step, steering=inbox)
    with pytest.raises(Killed):
        await _run(agent, deps, log, step)
    before = _reads((await log.latest(step)).entries, INBOX)
    inbox.steer("B: add a chart")
    inbox.told, world.die_before = 0, None  # a re-entered attempt starts told what the dead one was
    if not logged:
        with pytest.raises(ReplayDiverged, match=r"model\.request"):
            await _run(agent, deps, log, step)
        return
    assert (await _run(agent, deps, log, step)).output == f"Done after {REQUESTS} requests."
    assert world.provider_calls == REQUESTS, "the replay matched; nothing was bought twice"
    reads = _reads((await log.latest(step)).entries, INBOX)
    assert reads[: len(before)] == before, "the resumed attempt read what the dead one read"
    seen = [_steers_in(read) for read in reads]
    assert seen[0] == ["A: keep it short"] and not any(seen[1 : len(before)])
    assert seen[len(before)] == ["B: add a chart"], "B is taken at the first live safe point"


@dataclass
class _Spend:
    """The task's spend in memory: 60% of a $1 cap; reads named in `fail` raise."""

    fail: set[int] = field(default_factory=set[int])
    reads: int = 0

    async def __call__(self) -> BudgetFigures | None:
        self.reads += 1
        if self.reads in self.fail:
            raise RuntimeError("db blip")
        return BudgetFigures(ceiling_usd=Decimal(1), spent_usd=Decimal("0.6"))


@pytest.mark.parametrize("read", ["inbox", "spend"])
async def test_a_failed_read_replays_as_a_failure(read: str) -> None:
    """The first safe point's read fails, so request 1 goes without the steer (or the half-budget
    notice) the next one carries. The resumed attempt's sources are healthy, yet its replayed first
    read is the failure: request 1 regenerates as it was sent, and nothing is bought twice."""
    log, step = await _step()
    world, inbox, spend = World(notes_and_looks(), die_before=3), _Inbox(), _Spend()
    if read == "inbox":
        inbox.steer("A: keep it short")
    (inbox.fail if read == "inbox" else spend.fail).add(1)
    agent = _agent(world, inbox, guard=True)
    deps = TaskDeps(step_id=step, steering=inbox, spend=spend)
    with pytest.raises(Killed):
        await AgentRunner(agent, deps, PLAIN).run(
            "write the notes", log=await CallLogScope.open(log, step, fresh=False)
        )
    inbox.fail.clear()
    spend.fail.clear()
    inbox.told, world.die_before = 0, None
    scope = await CallLogScope.open(log, step, fresh=False)
    result = await AgentRunner(_agent(world, inbox, guard=True), deps, PLAIN).run(
        "write the notes", log=scope
    )
    assert result.output == f"Done after {REQUESTS} requests."
    assert scope.key.generation == 0, "the replay never diverged into a fence"
    assert world.provider_calls == REQUESTS
    first = _reads((await log.latest(step)).entries, INBOX if read == "inbox" else SPEND)[0]
    assert first == (
        {"pending": None, "park_pause": False} if read == "inbox" else {"figures": None, "failed": True}
    )


# ── overhead ──────────────────────────────────────────────────────────────────────────────────

LOOKS = 19  # 19 tool turns and the answer: a 20-request step


def _timed(overheads: list[tuple[str, float]]) -> Callable[..., Awaitable[object]]:
    """`PrismoidBackend.execute`, timing what it adds around the unit's own body, per unit name."""
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
        overheads.append((name, (time.perf_counter() - started - inside) * 1000))
        return result

    return execute


@pytest.mark.perf  # wall-clock budget, skewed by -n neighbours: run alone with `-m perf` on this file
async def test_logged_reads_keep_the_call_log_under_its_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R1 with both reads logged: p50 <= 5 ms and p95 <= 20 ms of backend time per unit on the real
    store, reads included. A model step's whole cost (its request and the reads and tool units after
    it) is printed with and without the reads, for the record."""
    overheads: list[tuple[str, float]] = []
    monkeypatch.setattr(PrismoidBackend, "execute", _timed(overheads))
    log, step = await _step()
    world, inbox = World([[("look", {"query": f"q{n}"})] for n in range(LOOKS)]), _Inbox()
    agent = _agent(world, inbox, guard=True)
    deps = TaskDeps(step_id=step, steering=inbox, spend=_Spend())
    scope = await CallLogScope.open(log, step, fresh=False)
    await AgentRunner(agent, deps, PLAIN).run("look it all up", log=scope)

    def read(name: str) -> bool:
        return INBOX[:-1] in name or SPEND[:-1] in name

    measured = {
        "unit": [ms for _, ms in overheads],
        "read": [ms for name, ms in overheads if read(name)],
        "model step": _per_step(overheads, reads=True),
        "model step without reads": _per_step(overheads, reads=False),
    }
    p = {what: (statistics.median(xs), statistics.quantiles(xs, n=20)[-1]) for what, xs in measured.items()}
    with capsys.disabled():
        for what, (p50, p95) in p.items():
            print(
                f"\ncall log overhead per {what}: n={len(measured[what])} p50={p50:.2f} ms p95={p95:.2f} ms"
            )
    assert world.provider_calls == LOOKS + 1
    assert p["unit"][0] <= 5 and p["unit"][1] <= 20
    assert p["read"][0] <= 5 and p["read"][1] <= 20


def _per_step(overheads: list[tuple[str, float]], *, reads: bool) -> list[float]:
    """Each model step's summed overhead: its request and every unit up to the next request."""
    steps: list[float] = []
    for name, ms in overheads:
        if "model.request" in name:
            steps.append(0.0)
        if steps and (reads or not (INBOX[:-1] in name or SPEND[:-1] in name)):
            steps[-1] += ms
    return steps
