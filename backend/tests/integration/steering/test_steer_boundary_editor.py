# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A pause held at the frontier and an editor's replan (ADR-064, ADR-092): the hold is on the record, and
an editor's replan lands their words as their accepted steer, then the Lead's replan seat takes it - cut
while it drafts it keeps the plan, a frontier that moved meanwhile is a 409 with nothing replaced, a kept or
failed draft leaves the steer to the steps, and a settled task or a step mid-wave is a 409 before both.
The rig is `tests/support/rigs/steer_boundary_rig.py`."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import Conflict
from prismoid.jobs import effects
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from prismoid.operations.turn import replan as turn_replan
from prismoid.schemas.events import PauseApplied, PlanEdited
from prismoid.schemas.lead import KeepPlan
from prismoid.schemas.plan import PlanEditKind, StepState
from prismoid.schemas.steering import SafePoint
from tests.support.rigs.steer_boundary_rig import (
    LeadCall,
    exploding,
    failing_lead,
    lead_replans,
    researched,
    rig_events,
    rig_steers,
    rig_steps,
)

pytestmark = pytest.mark.integration


async def test_a_pause_held_at_the_frontier_is_on_the_record(silent_dispatcher: None) -> None:
    task_id, bob_id = await researched()
    async with row_scope(Task, task_id) as s:
        bob = await s.get(Actor, bob_id)
        assert bob is not None
        await steering.pause(s, bob, task_id)

    assert await effects.pause_held(str(task_id)) is True

    (fact,) = await rig_events(task_id, PauseApplied)
    assert fact.step_id is None and fact.safe_point is SafePoint.BOUNDARY


async def test_no_pause_means_no_hold_and_no_fact(silent_dispatcher: None) -> None:
    task_id, _ = await researched()

    assert await effects.pause_held(str(task_id)) is False
    assert await rig_events(task_id, PauseApplied) == []


async def _replan_as_creator(task_id: uuid.UUID, reason: str = "start the memo over") -> int:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        ada = await s.get(Actor, task.creator_actor_id)
        assert ada is not None
        return await task_ops.replan(s, ada, task_id, reason=reason)


async def _replans_on_record(task_id: uuid.UUID) -> list[PlanEdited]:
    async with row_scope(Task, task_id) as s:
        events = await TaskEvent.replay(s, task_id, since=0)
    return [e for e in events if isinstance(e, PlanEdited) and e.edit.kind is PlanEditKind.REPLAN]


async def test_an_editors_replan_cut_while_the_lead_drafts_keeps_the_plan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor's replan drafts FIRST, as the boundary's does: the Lead's call runs with nothing
    committed but the reads, so a request cut - or an API that dies - while it thinks costs the draft,
    never the plan."""
    task_id, _ = await researched()
    before = [(s.id, s.state) for s in await rig_steps(task_id)]

    async def run(session: Any, *args: Any) -> Any:
        await session.commit()  # the real run releases its transaction before the model call
        raise asyncio.CancelledError  # the request is cut while the Lead thinks

    monkeypatch.setattr(turn_replan, "_run", run)
    with pytest.raises(asyncio.CancelledError):
        await _replan_as_creator(task_id)

    assert [(s.id, s.state) for s in await rig_steps(task_id)] == before
    assert await _replans_on_record(task_id) == [], "no replan is on the record until it replaces"


async def test_an_editors_replan_tells_the_lead_its_words_and_keeps_a_moved_frontier(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit is not on the record while it drafts, so the Lead reads the editor's words as their steer.
    A step that started while it drafted means the frontier is no longer the one it drafted for: a 409, the
    plan kept and nothing replaced. Drafted for an unmoved frontier, the revision replaces it, on the
    record as the editor's edit."""
    task_id, _ = await researched()
    write = next(s.id for s in await rig_steps(task_id) if s.title == "write")

    async def wave_starts_write() -> None:
        async with row_scope(Task, task_id) as s:
            step = await s.get(PlanStep, write)
            assert step is not None
            step.transition(StepState.RUNNING)
            await s.commit()

    calls: list[LeadCall] = []
    lead_replans(monkeypatch, calls, during=wave_starts_write, titles=("Ada's outline", "Ada's memo"))
    with pytest.raises(Conflict, match="moved while it was re-drafted"):
        await _replan_as_creator(task_id)
    ((told,),) = [c.taken for c in calls]
    assert (told.author, told.text, told.by_creator) == ("Ada", "start the memo over", True)
    assert [s.title for s in await rig_steps(task_id)] == ["research", "write"]
    assert await _replans_on_record(task_id) == []

    async with row_scope(Task, task_id) as s:  # `write` hands back: the frontier is a boundary again
        step = await s.get(PlanStep, write)
        assert step is not None
        step.state = StepState.READY.value
        await s.commit()
    assert await _replan_as_creator(task_id) == 3
    assert [s.title for s in await rig_steps(task_id)] == ["research", "Ada's outline", "Ada's memo"]
    (recorded,) = await _replans_on_record(task_id)
    assert recorded.edit.reason == "Re-planned for Ada's steers", "the 409'd replan's steer stood"
    assert [r.state for r in await rig_steers(task_id)] == ["applied", "applied"]
    assert recorded.actor is not None and recorded.actor.name == "Ada", "the edit is the editor's"


async def test_an_editors_replan_mid_wave_or_after_the_turn_is_a_409_before_any_model(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = await researched()
    monkeypatch.setattr(turn_replan, "_run", exploding)
    write = next(s.id for s in await rig_steps(task_id) if s.title == "write")
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, write)
        assert step is not None
        step.transition(StepState.RUNNING)
        await s.commit()
    with pytest.raises(Conflict, match="running"):
        await _replan_as_creator(task_id)

    settled, _ = await researched(write_done=True)
    with pytest.raises(Conflict, match="nothing in this plan"):
        await _replan_as_creator(settled)
    assert await _replans_on_record(settled) == []
    assert await rig_steers(task_id) == [] and await rig_steers(settled) == [], "a 409 lands no steer"


@pytest.mark.parametrize("lead", ["keeps", "fails"])
async def test_an_editors_replan_the_lead_does_not_revise_still_reaches_the_steps(
    lead: str, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor's words are their accepted steer: a kept plan's step not started has taken it, and after a
    failed Lead it waits, accepted, for that step to take as it starts."""
    task_id, _ = await researched()

    async def keeps(session: Any, *args: Any) -> KeepPlan:
        await session.commit()  # the real run releases its transaction before the model call
        return KeepPlan(reason="the memo already starts over")

    if lead == "keeps":
        monkeypatch.setattr(turn_replan, "_run", keeps)
    else:
        failing_lead(monkeypatch)
    assert await _replan_as_creator(task_id) == 2

    (steer,) = await rig_steers(task_id)
    write = next(s for s in await rig_steps(task_id) if s.title == "write")
    assert steer.text == "start the memo over"
    if lead == "keeps":
        assert steer.state == "applied" and steer.epoch <= write.epoch, "the kept step took it"
    else:
        assert steer.state == "accepted" and steer.epoch > write.epoch, "the step takes it as it starts"
