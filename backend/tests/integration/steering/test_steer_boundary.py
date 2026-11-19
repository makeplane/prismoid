# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The frontier steer boundary (ADR-064, ADR-092): unstarted steps a steer has overtaken on a turn the Lead
planned are revised by the Lead's replan seat (`turn.replan.lead_replan`) - the turn's done steps stand,
are told to the Lead, and feed the new roots - and every plan applies the steers it read
(`applied{replan}`). Recipe lanes and every turn the Lead did not plan keep their plan: their steps take
the steer when they start.

Real rows, the replan run stubbed (`tests/support/rigs/steer_boundary_rig.py`). The stub commits its session before
anything else, exactly as the real run does before its model call, so a steer made inside it (its own
session) or its own-session usage record never waits on a row lock the re-plan's session holds."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

import prismoid.ai.agents.base as agents_base
from prismoid.db.models.actor import Actor
from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import row_scope
from prismoid.jobs import effects
from prismoid.jobs import run_task as run_task_job
from prismoid.operations import execution, steering
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.execution import mounts
from prismoid.operations.turn import replan as turn_replan
from prismoid.schemas.events import PlanEdited, SteerApplied
from prismoid.schemas.lead import SubmitPlan
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanEditKind,
    PlanProposal,
    ProposedStep,
    StepState,
    TaskStatus,
)
from prismoid.schemas.steering import SafePoint
from tests.support.rigs.steer_boundary_rig import (
    CHURN,
    STEER,
    LeadCall,
    draft,
    exploding,
    failing_lead,
    lead_replans,
    narrated,
    project_task,
    researched,
    rig_boundary,
    rig_drafted,
    rig_events,
    rig_steer,
    rig_steers,
    rig_steps,
)
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

# A re-plan that did not finish keeps the plan and says why.
_UNFINISHED = f"{turn_replan.KEPT}: {turn_replan.UNFINISHED}"


async def test_a_stale_unstarted_step_replans_and_the_steer_applies(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched()
    before = {s.title: s for s in await rig_steps(task_id)}
    row = await rig_steer(bob_id, task_id)
    assert row.epoch == 2
    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls)

    assert await effects.steer_boundary(str(task_id)) is True

    after = await rig_steps(task_id)
    assert [(s.title, s.step_state) for s in after] == [
        ("research", StepState.DONE),
        ("outline", StepState.READY),
        ("write the memo", StepState.PENDING),
    ]
    assert after[0].id == before["research"].id, "the done step stands"
    assert before["write"].id not in {s.id for s in after}, "the stale unstarted step is gone"
    assert [(s.epoch, s.turn_epoch) for s in after[1:]] == [(2, 1), (2, 1)], "drafted under the steer"
    assert [n.text for n in calls[0].taken] == [STEER], "the Lead answers the steer"

    (settled,) = await rig_steers(task_id)
    assert settled.state == "applied" and settled.safe_point == "replan" and settled.applied_step_id is None
    (applied,) = await rig_events(task_id, SteerApplied)
    assert (applied.command_id, applied.author, applied.safe_point, applied.step_id) == (
        row.command_id,
        "Bob",
        SafePoint.REPLAN,
        None,
    )
    replans = [e for e in await rig_events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]
    (edit,) = replans
    assert edit.edit.reason == f"Re-planned for Bob's steer: '{STEER}'"
    assert edit.actor is None, "a system edit names no one; the steer names its author"

    async with row_scope(Task, task_id) as s:
        labels = list(
            await s.scalars(
                select(UsageEvent.actor_id).where(UsageEvent.task_id == task_id, UsageEvent.step_id.is_(None))
            )
        )
        task = await s.get(Task, task_id)
        assert task is not None and task.criteria["must"] == ["German only"]
    assert labels == [bob_id], "the re-plan's spend wears the steerer's epoch"


async def test_the_redrafted_root_depends_on_the_done_step_and_mounts_its_files(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls)

    assert await rig_boundary(task_id)

    assert calls[0].done == ("- research: Findings",), "the Lead is told what is done"
    research, outline, memo = await rig_steps(task_id)
    async with row_scope(Task, task_id) as s:
        edges = {(e.from_step_id, e.to_step_id) for e in await PlanEdge.for_task(s, task_id)}
        task = await s.get(Task, task_id)
        assert task is not None
        mounted = [a.title for a in await mounts.upstream_artifacts(s, task, outline)]
    assert edges == {(research.id, outline.id), (outline.id, memo.id)}, (
        "the done sink feeds only the new root"
    )
    assert mounted == ["Findings"]
    assert outline.step_state is StepState.READY, "a root over done work is ready at once"


async def test_no_unstarted_step_means_no_replan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched(write_done=True)
    await rig_steer(bob_id, task_id)
    monkeypatch.setattr(turn_replan, "_run", exploding)

    assert await rig_boundary(task_id) is False
    (row,) = await rig_steers(task_id)
    assert row.state == "accepted", "a settled turn is fence 2's"


async def test_a_steer_the_unstarted_steps_already_carry_replans_nothing(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched()
    row = await rig_steer(bob_id, task_id)
    async with row_scope(Task, task_id) as s:
        for step in await PlanStep.for_task(s, task_id):
            step.epoch = row.epoch
        await s.commit()
    monkeypatch.setattr(turn_replan, "_run", exploding)

    assert await rig_boundary(task_id) is False


async def test_a_step_that_started_while_the_lead_drafted_keeps_the_plan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Draft first, then re-check under the lock: a frontier that moved during the Lead's call
    loses the draft, never the running work - and says it moved, so the workflow reads it again
    instead of dispatching a stale wave."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    write_id = next(s.id for s in await rig_steps(task_id) if s.title == "write")

    async def start_write() -> None:
        async with row_scope(Task, task_id) as s:
            step = await s.get(PlanStep, write_id)
            assert step is not None
            step.transition(StepState.RUNNING)
            await s.commit()

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, during=start_write)

    assert await effects.steer_boundary(str(task_id)) is True, "the frontier moved: read it again"
    assert [(s.title, s.step_state) for s in await rig_steps(task_id)] == [
        ("research", StepState.DONE),
        ("write", StepState.RUNNING),
    ]
    (row,) = await rig_steers(task_id)
    assert row.state == "accepted", "the running step takes it at its own safe points"
    assert not [e for e in await rig_events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]


async def test_a_step_in_flight_asks_no_lead(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-wave is not a boundary (as the editor's `replan` refuses): no model call is spent."""
    task_id, bob_id = await rig_drafted(
        [("research", StepState.RUNNING, None), ("write", StepState.PENDING, None)], [("research", "write")]
    )
    await rig_steer(bob_id, task_id)
    monkeypatch.setattr(turn_replan, "_run", exploding)

    assert await rig_boundary(task_id) is False


async def test_a_steer_retracted_while_it_drafted_keeps_the_plan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched()
    row = await rig_steer(bob_id, task_id)
    before = [(s.id, s.epoch) for s in await rig_steps(task_id)]

    async def bob_withdraws() -> None:
        async with row_scope(Task, task_id) as s:
            bob = await s.get(Actor, bob_id)
            assert bob is not None
            await steering.retract(s, bob, task_id, row.command_id)

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, during=bob_withdraws)

    assert await rig_boundary(task_id) is False
    assert calls, "the Lead was asked"
    assert [(s.id, s.epoch) for s in await rig_steps(task_id)] == before, "the old plan stands"
    (settled,) = await rig_steers(task_id)
    assert settled.state == "retracted"
    assert not [e for e in await rig_events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]
    assert not await rig_events(task_id, SteerApplied)


async def test_a_failed_lead_keeps_the_plan_and_the_steps_take_the_steer(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing else changed: False, so a provider that keeps failing is never asked again in a loop."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    before = [(s.id, s.step_state, s.epoch) for s in await rig_steps(task_id)]
    said: list[tuple[str, str]] = []
    narrated(monkeypatch, said)
    failing_lead(monkeypatch)

    assert await effects.steer_boundary(str(task_id)) is False
    assert [(s.id, s.step_state, s.epoch) for s in await rig_steps(task_id)] == before
    (row,) = await rig_steers(task_id)
    assert row.state == "accepted", "the unstarted step takes it when it starts (before_model)"
    assert said[-1] == ("end", _UNFINISHED)


@pytest.mark.parametrize("breaks", ["done_work", "pin_bots"])
async def test_an_error_beside_the_lead_keeps_the_plan_and_ends_the_phase(
    breaks: str, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that raises once the phase started - the brief, the pins, a write under the lock -
    keeps the plan: the phase ends "Kept the plan", and the task never fails for it. Nothing moved,
    so the boundary says so (False): the error would only happen again."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    before = [(s.id, s.step_state, s.epoch) for s in await rig_steps(task_id)]
    said: list[tuple[str, str]] = []
    narrated(monkeypatch, said)
    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls)

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("a bug, not the Lead")

    monkeypatch.setattr(turn_replan, breaks, broken)

    assert await effects.steer_boundary(str(task_id)) is False
    assert [(s.id, s.step_state, s.epoch) for s in await rig_steps(task_id)] == before
    (row,) = await rig_steers(task_id)
    assert row.state == "accepted", "the unstarted step takes it when it starts (before_model)"
    assert said[0][0] == "start" and said[-1] == ("end", _UNFINISHED)
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.state is TaskStatus.RUNNING


async def test_an_error_beside_the_lead_after_the_task_paused_says_moved(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error arm reads the frontier as the Lead's failure does: Bob paused while it drafted,
    so the boundary says moved and the workflow holds the pause before the wave. The pins raise
    inside an open transaction (the real `pin_bots` reads actors after the run's commit), so the
    rollback expires the task: the frontier is read by its id."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)

    async def bob_pauses() -> None:
        async with row_scope(Task, task_id) as s:
            bob = await s.get(Actor, bob_id)
            assert bob is not None
            await steering.pause(s, bob, task_id)

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, during=bob_pauses)

    async def broken(steps: object, session: Any, task: Task) -> object:
        await session.execute(select(1))  # a transaction is open when it breaks
        raise RuntimeError("a bug, not the Lead")

    monkeypatch.setattr(turn_replan, "pin_bots", broken)

    assert await effects.steer_boundary(str(task_id)) is True, "go round: the pause holds first"
    assert await effects.pause_held(str(task_id)) is True


async def test_a_read_error_over_an_editors_replan_reads_the_frontier_again(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ada replans by hand while the boundary reads the done work, then that read breaks with its
    transaction open: the frontier, read again by id under the lock, has moved - so the wave read next
    holds only live steps, never a deleted id."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    stale = {s.id for s in await rig_steps(task_id) if s.step_state is StepState.READY}
    real_done = turn_replan.done_work
    reads = 0

    async def done_work(session: Any, steps_now: Any, turn_epoch: int) -> Any:
        nonlocal reads
        reads += 1
        if reads > 1:  # Ada's own replan reads it too
            return await real_done(session, steps_now, turn_epoch)
        async with row_scope(Task, task_id) as s:  # Ada replans by hand meanwhile
            task = await s.get(Task, task_id)
            assert task is not None
            ada = await s.get(Actor, task.creator_actor_id)
            assert ada is not None
            await task_ops.replan(s, ada, task_id, reason="start the memo over")
        await session.execute(select(1))
        raise RuntimeError("a bug in the read")

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, titles=("Ada's outline", "Ada's memo"))
    monkeypatch.setattr(turn_replan, "done_work", done_work)

    assert await effects.steer_boundary(str(task_id)) is True
    wave = await effects.frontier(str(task_id))
    assert wave and not stale & {uuid.UUID(sid) for sid in wave}, "nothing deleted is dispatched"
    assert [s.title for s in await rig_steps(task_id)] == ["research", "Ada's outline", "Ada's memo"]


@pytest.mark.parametrize("breaks", ["done_work", "pin_bots"])
async def test_a_replan_error_that_keeps_happening_never_loops_the_workflow(
    breaks: str, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the workflow: `run_task`'s own body, its I/O effects stubbed and the boundary
    real. A re-plan error that would happen on every pass lets the frontier go on - the stale step
    dispatches exactly once, the Lead is asked at most once, the phase ends "Kept the plan",
    and the turn delivers."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    (write,) = [s.id for s in await rig_steps(task_id) if s.step_state is StepState.READY]
    said: list[tuple[str, str]] = []
    narrated(monkeypatch, said)
    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls)

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("a bug, every time")

    monkeypatch.setattr(turn_replan, breaks, broken)

    boundaries: list[bool] = []
    real_boundary = effects.steer_boundary

    async def boundary(tid: str) -> bool:
        moved = await real_boundary(tid)
        boundaries.append(moved)
        assert len(boundaries) <= 3, "the boundary loops"
        return moved

    dispatched: list[str] = []

    class Handle:
        def __init__(self, tid: str, step_id: str) -> None:
            self.tid, self.step_id = tid, step_id

        async def get_result(self, polling_interval_sec: float = 1.0) -> str:
            dispatched.append(self.step_id)
            async with row_scope(Task, uuid.UUID(self.tid)) as s:  # the step ran and finished
                step = await s.get(PlanStep, uuid.UUID(self.step_id))
                assert step is not None
                step.transition(StepState.RUNNING)
                step.transition(StepState.DONE)
                await s.commit()
            return "the memo"

    class StepQueue:
        async def enqueue_async(self, fn: object, tid: str, step_id: str) -> Handle:
            return Handle(tid, step_id)

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    async def no(*args: object, **kwargs: object) -> bool:
        return False

    failed: list[str] = []

    async def fail_task(tid: str, reason: str, turn: int = 1) -> None:
        failed.append(reason)

    async def deliver_task(tid: str, summary: str, turn: int = 1) -> dict[str, Any]:
        return {"verdict": "delivered", "replaced": []}

    for name, stub in {
        "mark_dequeued": nothing,
        "turn_is_express": no,
        "plan_task": nothing,
        "mark_running": nothing,
        "fanout_enabled": no,
        "check_budget": nothing,
        "fail_task": fail_task,
        "deliver_task": deliver_task,
        "conversation_report": nothing,
        "remember_task": nothing,
        "steer_boundary": boundary,
    }.items():
        monkeypatch.setattr(effects, name, stub)
    monkeypatch.setattr(run_task_job, "step_queue", StepQueue())

    body = inspect.unwrap(run_task_job.run_task)  # the workflow's own body, under DBOS's wrappers
    outcome = await body(str(task_id), 2)  # turn 2: no intake round

    assert (outcome, failed) == ("delivered", [])
    assert dispatched == [str(write)], "the stale step dispatched exactly once"
    assert boundaries == [False]
    assert len(calls) == (0 if breaks == "done_work" else 1), "the Lead is asked at most once"
    assert [p for p, _ in said] == ["start", "end"] and said[-1] == ("end", _UNFINISHED)


async def test_a_failed_lead_over_an_editors_replan_reads_the_frontier_again(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editor's Replan rewrote the remainder while the boundary's Lead failed: the boundary says the
    frontier moved, so the wave read next holds only live steps - never a deleted id."""
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    stale = {s.id for s in await rig_steps(task_id) if s.step_state is StepState.READY}

    async def ada_replans() -> None:
        async with row_scope(Task, task_id) as s:  # the boundary's run: Ada replans by hand meanwhile
            task = await s.get(Task, task_id)
            assert task is not None
            ada = await s.get(Actor, task.creator_actor_id)
            assert ada is not None
            await task_ops.replan(s, ada, task_id, reason="start the memo over")

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, during=ada_replans, titles=("Ada's outline", "Ada's memo"), fails=True)

    assert await effects.steer_boundary(str(task_id)) is True
    wave = await effects.frontier(str(task_id))
    assert wave and not stale & {uuid.UUID(sid) for sid in wave}, "nothing deleted is dispatched"
    assert [s.title for s in await rig_steps(task_id)] == ["research", "Ada's outline", "Ada's memo"]


async def test_a_failed_lead_while_the_task_paused_holds_before_the_wave(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)

    async def bob_pauses() -> None:
        async with row_scope(Task, task_id) as s:
            bob = await s.get(Actor, bob_id)
            assert bob is not None
            await steering.pause(s, bob, task_id)

    failing_lead(monkeypatch, during=bob_pauses)

    assert await effects.steer_boundary(str(task_id)) is True, "go round: the pause holds first"
    assert await effects.pause_held(str(task_id)) is True


async def test_the_replan_says_what_it_is_doing_before_the_lead_runs(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched()
    await rig_steer(bob_id, task_id)
    said: list[tuple[str, str]] = []

    async def the_record_holds_it() -> None:
        replans = [e for e in await rig_events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]
        said.append(("committed", str(len(replans))))

    narrated(monkeypatch, said, on_end=the_record_holds_it)

    async def lead_runs() -> None:
        said.append(("lead", "runs"))

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, during=lead_runs)

    assert await effects.steer_boundary(str(task_id)) is True
    assert said == [
        ("start", f"Re-planning for Bob's steer: '{STEER}'"),
        ("lead", "runs"),
        ("end", "Re-planned - 2 steps"),
        ("committed", "1"),
    ], "the end is said once the record holds the new plan"


async def test_every_done_step_with_files_feeds_the_new_roots(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip edge (research → write) keeps mounting after the re-draft."""
    task_id, bob_id = await rig_drafted(
        [
            ("research", StepState.DONE, "Findings"),
            ("outline", StepState.DONE, "Outline"),
            ("write", StepState.READY, None),
        ],
        [("research", "outline"), ("outline", "write"), ("research", "write")],
    )
    await rig_steer(bob_id, task_id)
    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, titles=("write the German memo", "proofread"))

    assert await rig_boundary(task_id)

    assert calls[0].done == ("- research: Findings", "- outline: Outline")
    research, outline, memo, proof = await rig_steps(task_id)
    assert memo.title == "write the German memo"
    async with row_scope(Task, task_id) as s:
        edges = {(e.from_step_id, e.to_step_id) for e in await PlanEdge.for_task(s, task_id)}
        task = await s.get(Task, task_id)
        assert task is not None
        mounted = sorted(a.title for a in await mounts.upstream_artifacts(s, task, memo))
    assert edges == {
        (research.id, outline.id),
        (research.id, memo.id),
        (outline.id, memo.id),
        (memo.id, proof.id),
    }
    assert mounted == ["Findings", "Outline"]


async def test_a_long_form_plan_is_not_replanned_and_its_next_step_takes_the_steer(
    fake_frames: Any, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, bob_id = await researched(write_lane="section")
    row = await rig_steer(bob_id, task_id)
    monkeypatch.setattr(turn_replan, "_run", exploding)

    assert await rig_boundary(task_id) is False
    assert (await rig_steers(task_id))[0].state == "accepted"

    async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        await deps.frame.write_bytes("/work/section.md", b"# section")
        await deps.frame.publish("/work/section.md", "document", "write", ())
        return SimpleNamespace(output="wrote the section")

    monkeypatch.setattr(ex_harvest, "extract_previews", _no_previews)
    monkeypatch.setattr(agents_base, "build", scripted(run))
    write_id = next(s.id for s in await rig_steps(task_id) if s.title == "write")
    async with row_scope(Task, task_id) as s:
        await execution.execute_step(s, task_id, write_id)

    (settled,) = await rig_steers(task_id)
    assert (settled.command_id, settled.state, settled.safe_point, settled.applied_step_id) == (
        row.command_id,
        "applied",
        "before_model",
        write_id,
    )


@pytest.mark.parametrize(
    ("route", "source"),
    [
        ("standard", "planner"),
        ("standard", "hatch"),
        ("standard", "dial"),
        ("direct", "dial"),
        ("fleet", "dial"),
    ],
)
async def test_only_a_turn_the_lead_planned_is_replanned(
    route: str, source: str, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recipe's plan, a direct thread's bot and a plan made before the Lead keep their plan at the boundary:
    their steps take the steer when they start, and no model is asked."""
    task_id, bob_id = await researched(route=route, source=source)
    await rig_steer(bob_id, task_id)
    monkeypatch.setattr(turn_replan, "_run", exploding)

    assert await rig_boundary(task_id) is False
    assert (await rig_steers(task_id))[0].state == "accepted"


def _lead_ends_with_a_plan(seen: list[str], during: Callable[[Any], Awaitable[None]] | None = None) -> Any:
    """A scripted Lead run: what `during` does at its safe point, then a one-step writer plan; `seen` keeps its prompt."""

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        seen.append(str(prompt))
        if during is not None:
            await during(deps)
        step = ProposedStep(label="s1", agent="writer", title="Memo", goal="Write the memo", publishes=True)
        plan = PlanProposal(steps=[step], deliverable=DeliverableKind.FILES)
        return SimpleNamespace(output=SubmitPlan(say="Writing it.", plan=plan))

    return scripted(run)


async def _lead_step(task_id: uuid.UUID) -> uuid.UUID:
    """Seat the drafted turn's Lead and set the task running; the Lead's step id."""
    (lead,) = await rig_steps(task_id)
    assert (lead.spec or {}).get("agent") == "lead"
    async with row_scope(Task, task_id) as s:
        await execution.advance(s, task_id, TaskStatus.RUNNING)
        await s.commit()
    return lead.id


async def test_a_steer_while_the_lead_decides_is_the_plans_own(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steer that lands while the Lead runs is taken at its next safe point, so the plan it ends with is
    drafted under the steer's epoch and the first boundary has nothing left to re-plan."""
    task_id, bob_id = await project_task(CHURN)
    await draft(task_id)
    lead_id = await _lead_step(task_id)

    async def bob_steers(deps: Any) -> None:
        await rig_steer(bob_id, task_id)
        told = await deps.steering.pending(deps.steering.told)  # the next safe point takes it
        await deps.steering.applied(told.steers, SafePoint.BEFORE_MODEL)

    monkeypatch.setattr(agents_base, "build", _lead_ends_with_a_plan([], bob_steers))
    async with row_scope(Task, task_id) as s:
        await execution.execute_step(s, task_id, lead_id)

    planned = [s for s in await rig_steps(task_id) if s.id != lead_id]
    assert planned and all(s.epoch == 2 for s in planned), "the plan carries the epoch the run reached"
    (row,) = await rig_steers(task_id)
    assert (row.state, row.safe_point) == ("applied", "before_model")
    monkeypatch.setattr(turn_replan, "_run", exploding)
    assert await rig_boundary(task_id) is False


async def test_review_focus_a_steer_while_planning_is_applied_by_the_plan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steer accepted before the turn was planned (no step running) is applied when the turn's Lead step is
    written, and the Lead reads it - never left accepted, never a re-plan."""
    task_id, bob_id = await project_task(CHURN)
    row = await rig_steer(bob_id, task_id)
    await draft(task_id)
    lead_id = await _lead_step(task_id)
    seen: list[str] = []
    monkeypatch.setattr(agents_base, "build", _lead_ends_with_a_plan(seen))
    async with row_scope(Task, task_id) as s:
        await execution.execute_step(s, task_id, lead_id)

    assert all(s.epoch == 2 for s in await rig_steps(task_id))
    assert STEER in seen[0], "the Lead read the steer"
    (settled,) = await rig_steers(task_id)
    assert (settled.state, settled.safe_point, settled.applied_step_id) == ("applied", "replan", None)
    (applied,) = await rig_events(task_id, SteerApplied)
    assert applied.command_id == row.command_id and applied.safe_point is SafePoint.REPLAN

    monkeypatch.setattr(turn_replan, "_run", exploding)
    assert await rig_boundary(task_id) is False, "the plan already took it"
