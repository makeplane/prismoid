# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The task state machines, asserted.

`ALLOWED` in `schemas/task_states.py` and `_ALLOWED` in `db/models/plan.py` are not copies of the
diagram - they are the diagram. So these tests read like the diagram, and if someone widens a transition to make a
bug go away, a test fails and asks them to change the diagram first.
"""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.models.plan import MAX_ATTEMPTS, PlanStep
from prismoid.db.models.task import TERMINAL, Task
from prismoid.errors import InvalidTransition
from prismoid.schemas.plan import StepState, TaskStatus


def a_task(status: TaskStatus) -> Task:
    t = Task(goal="g", creator_actor_id=uuid.uuid4())
    t.id = uuid.uuid4()
    t.status = status.value
    return t


def a_step(state: StepState, attempt: int = 0) -> PlanStep:
    s = PlanStep(task_id=uuid.uuid4())
    s.id = uuid.uuid4()
    s.state = state.value
    s.attempt = attempt
    return s


# ── the task machine ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (TaskStatus.CREATED, TaskStatus.CLARIFYING),
        (TaskStatus.CREATED, TaskStatus.PLANNING),
        (TaskStatus.CLARIFYING, TaskStatus.PLANNING),
        (TaskStatus.PLANNING, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.WAITING),  # parked on a gate, at ~zero cost
        (TaskStatus.WAITING, TaskStatus.RUNNING),  # resolved, resumed
        (TaskStatus.RUNNING, TaskStatus.PAUSED),  # dispatch stops at the frontier boundary
        (TaskStatus.PAUSED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.PLANNING),  # replan-as-edit re-enters planning
        (TaskStatus.RUNNING, TaskStatus.DELIVERED),
    ],
)
def test_the_diagram_permits(frm: TaskStatus, to: TaskStatus) -> None:
    t = a_task(frm)
    t.transition(to)
    assert t.state is to


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (TaskStatus.CREATED, TaskStatus.RUNNING),  # you cannot run what you have not planned
        (TaskStatus.CREATED, TaskStatus.DELIVERED),  # …or deliver it
        (TaskStatus.PLANNING, TaskStatus.WAITING),  # gates are hit while running, not while planning
        (TaskStatus.PAUSED, TaskStatus.DELIVERED),  # a paused task delivers nothing
        (TaskStatus.CLARIFYING, TaskStatus.RUNNING),  # clarify → plan → run, never clarify → run
    ],
)
def test_the_diagram_refuses(frm: TaskStatus, to: TaskStatus) -> None:
    t = a_task(frm)
    with pytest.raises(InvalidTransition):
        t.transition(to)
    assert t.state is frm  # and it did not half-move


@pytest.mark.parametrize("settled", sorted(TERMINAL))
def test_settled_reopens_to_planning_only(settled: TaskStatus) -> None:
    """Settled, not sealed: a follow-up continues the SAME task, so the one
    exit from every settled status is `planning` - turn N+1 - and everything else stays
    refused. `TERMINAL` still means "settled" for the archive/delete/steer guards."""
    for to in TaskStatus:
        t = a_task(settled)
        if to is TaskStatus.PLANNING:
            t.transition(to)
            assert t.state is TaskStatus.PLANNING
        else:
            with pytest.raises(InvalidTransition):
                t.transition(to)
            assert t.state is settled  # and it did not half-move


def test_cancel_is_reachable_from_every_live_state() -> None:
    """Cancel is the only mid-step interrupt - so it must always be available."""
    for status in TaskStatus:
        if status in TERMINAL:
            continue
        assert a_task(status).can(TaskStatus.CANCELLED), f"{status} cannot be cancelled"


# ── the step machine ──────────────────────────────────────────────────────────────────────────


def test_running_reaches_done_directly_and_through_the_fleet_lane() -> None:
    """ADR-019: `running → done` is the standard lane (published means done; the verification
    receipt is signed after delivery, on the same stream). `running → verifying → done` stays
    the fleet's in-line lane. What Rule 9 still forbids is an UNPUBLISHED artifact -
    publish remains the only door; the receipt just stopped blocking the answer."""
    s = a_step(StepState.RUNNING)
    s.transition(StepState.DONE)
    assert s.step_state is StepState.DONE

    f = a_step(StepState.RUNNING)
    f.transition(StepState.VERIFYING)
    f.transition(StepState.DONE)
    assert f.step_state is StepState.DONE


def test_a_retry_re_enters_at_ready() -> None:
    """A retry is a fresh attempt, not a resumed one - which is what makes the attempt counter
    mean something and the frame snapshot the unit of recovery."""
    s = a_step(StepState.VERIFYING)
    s.transition(StepState.RETRYING)
    s.transition(StepState.READY)
    assert s.step_state is StepState.READY


def test_attempts_are_exhausted_at_three() -> None:
    assert not a_step(StepState.RETRYING, attempt=MAX_ATTEMPTS - 1).attempts_exhausted()
    assert a_step(StepState.RETRYING, attempt=MAX_ATTEMPTS).attempts_exhausted()


def test_an_escalated_step_can_be_retried_after_a_human_helps() -> None:
    s = a_step(StepState.ESCALATED)
    s.transition(StepState.READY)
    assert s.step_state is StepState.READY


# ── the recovery walks (the triage is total; these are the edges it leans on) ─────────────────


def test_recovered_retrying_walks_to_running() -> None:
    s = a_step(StepState.RETRYING)
    s.transition(StepState.READY)
    s.transition(StepState.RUNNING)
    assert s.step_state is StepState.RUNNING


@pytest.mark.parametrize("to", [StepState.DONE, StepState.RETRYING, StepState.ESCALATED])
def test_verifying_resume_edges_are_legal(to: StepState) -> None:
    s = a_step(StepState.VERIFYING)
    s.transition(to)
    assert s.step_state is to


def test_pending_can_settle_skipped_by_primary_loss() -> None:
    s = a_step(StepState.PENDING)
    s.transition(StepState.SKIPPED)
    assert s.step_state is StepState.SKIPPED
