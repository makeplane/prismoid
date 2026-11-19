# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The workflow's effects, in journal order: a replayed `run_task` must call the same effects in the
same order, so each path through the body is pinned as the exact sequence it calls. Every effect is
stubbed - no database, no DBOS runtime."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from prismoid.errors import CouldNotDo, StepFailed
from prismoid.jobs import durable, effects
from prismoid.jobs import run_task as run_task_job

A, B = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"


class Script:
    """Stubbed effects that record each call by name and answer from `answers` (a value, or a list
    consumed one call at a time)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **answers: Any) -> None:
        self.calls: list[str] = []
        self.answers: dict[str, Any] = {
            "turn_is_express": False,
            "intake_task": None,
            "fanout_enabled": False,
            "frontier": [[A], []],
            "pause_held": False,
            "paused": False,
            "is_cancelled": False,
            "steer_boundary": False,
            "check_budget": None,
            "deliver_task": {"verdict": "delivered", "replaced": []},
            "turn_escalated": False,
            "apply_clarification": "planning",
            "gate_resolution": None,
            "dispatch_frontier": ["a said", "b said"],
            "resume_from_budget": "running",
            **answers,
        }
        names = [
            "mark_dequeued", "turn_is_express", "mark_planning", "intake_task", "gate_resolution",
            "apply_clarification", "plan_task", "mark_running", "fanout_enabled", "frontier",
            "pause_held", "paused", "is_cancelled", "steer_boundary", "check_budget",
            "resume_from_budget", "mark_cancelled", "dispatch_frontier", "deliver_task", "fail_task",
            "conversation_report", "title_task", "turn_escalated", "remember_task",
        ]  # fmt: skip
        for name in names:
            monkeypatch.setattr(effects, name, self._stub(name))
        monkeypatch.setattr(effects, "execute_step", self._stub("execute_step"))
        monkeypatch.setattr(durable, "wait", self._stub("wait"))
        monkeypatch.setattr(durable, "start", self._start)
        monkeypatch.setattr(durable, "join", self._stub("join"))

    def _answer(self, name: str) -> Any:
        value = self.answers.get(name)
        if isinstance(value, list) and value and name != "dispatch_frontier":
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def _stub(self, name: str) -> Callable[..., Any]:
        async def stub(*args: object, **kwargs: object) -> Any:
            self.calls.append(name)
            answer = self._answer(name)
            if isinstance(answer, Exception):
                raise answer
            return f"{args[-1]} said" if name == "join" else answer

        return stub

    async def _start(self, queue: object, key: str, fn: object, task_id: str, step_id: str) -> str:
        self.calls.append(f"start {key}")
        return step_id


async def _run(turn: int = 1) -> str:
    body = inspect.unwrap(run_task_job.run_task)
    return await body("t-1", turn)


HEAD = ["mark_dequeued", "turn_is_express"]
RUN = ["plan_task", "mark_running", "fanout_enabled"]
BOUNDARY = ["pause_held", "is_cancelled", "steer_boundary", "check_budget"]
DELIVER = ["frontier", "pause_held", "is_cancelled", "deliver_task", "conversation_report"]


async def test_a_first_turn_with_nothing_to_ask_runs_one_serial_step(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch)
    assert await _run() == "delivered"
    assert s.calls == [
        *HEAD, "intake_task", "mark_planning", *RUN,
        "frontier", *BOUNDARY, f"start step-{A}", "join",
        *DELIVER, "remember_task", "title_task",
    ]  # fmt: skip


async def test_an_express_first_turn_skips_intake_and_is_titled_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Script(monkeypatch, turn_is_express=True, frontier=[[]])
    assert await _run() == "delivered"
    assert s.calls == [
        *HEAD, "mark_planning", *RUN, *DELIVER, "title_task", "turn_escalated", "remember_task",
    ]  # fmt: skip


async def test_a_clarify_gate_that_times_out_reads_the_row_then_may_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Script(monkeypatch, intake_task="g-1", apply_clarification="cancelled")
    assert await _run() == "cancelled"
    assert s.calls == [
        *HEAD,
        "intake_task",
        "wait",
        "gate_resolution",
        "apply_clarification",
        "conversation_report",
    ]


async def test_a_turn_whose_person_is_gone_ends_at_the_dequeue_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Script(monkeypatch, mark_dequeued=True)
    assert await _run(turn=2) == "cancelled"
    assert s.calls == ["mark_dequeued", "conversation_report"]


HOLD = ["is_cancelled", "wait"]  # a turn's budget park, before its answer


async def test_a_journal_that_recorded_none_at_the_dequeue_mark_takes_the_old_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every journal written before the turn-start hold recorded None here: the same effects follow,
    in the same order, as before it existed."""
    s = Script(monkeypatch, mark_dequeued=None)
    assert await _run() == "delivered"
    assert s.calls == [
        *HEAD, "intake_task", "mark_planning", *RUN,
        "frontier", *BOUNDARY, f"start step-{A}", "join",
        *DELIVER, "remember_task", "title_task",
    ]  # fmt: skip


async def test_a_turn_without_allowance_parks_before_intake_and_resumes_into_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dequeue mark hands back the "allowance spent" gate it opened (a value no older journal
    recorded): the turn holds on it with the budget park's own effects, then intake runs."""
    s = Script(monkeypatch, mark_dequeued="g-1", wait={"action": "accept"})
    assert await _run() == "delivered"
    assert s.calls == [
        "mark_dequeued", *HOLD, "resume_from_budget", "is_cancelled",
        "turn_is_express", "intake_task", "mark_planning", *RUN,
        "frontier", *BOUNDARY, f"start step-{A}", "join",
        *DELIVER, "remember_task", "title_task",
    ]  # fmt: skip


async def test_a_turn_held_for_allowance_and_cancelled_never_reaches_intake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Script(monkeypatch, mark_dequeued="g-1", wait={"action": "cancel"})
    assert await _run(turn=2) == "cancelled"
    assert s.calls == ["mark_dequeued", *HOLD, "mark_cancelled", "conversation_report"]


async def test_a_later_turn_runs_a_parallel_wave_and_remembers(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch, fanout_enabled=True, frontier=[[A, B], []])
    assert await _run(turn=2) == "delivered"
    assert s.calls == [*HEAD, *RUN, "frontier", *BOUNDARY, "dispatch_frontier", *DELIVER, "remember_task"]


async def test_a_steer_boundary_reads_the_frontier_again(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch, steer_boundary=[True, False], frontier=[[A], [A], []])
    assert await _run(turn=2) == "delivered"
    first = ["frontier", "pause_held", "is_cancelled", "steer_boundary"]
    assert s.calls == [
        *HEAD,
        *RUN,
        *first,
        "frontier",
        *BOUNDARY,
        f"start step-{A}",
        "join",
        *DELIVER,
        "remember_task",
    ]


async def test_a_cancel_at_the_wave_boundary_reports_home(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch, is_cancelled=True)
    assert await _run(turn=2) == "cancelled"
    assert s.calls == [*HEAD, *RUN, "frontier", "pause_held", "is_cancelled", "conversation_report"]


async def test_a_budget_gate_cancelled_by_its_owner_ends_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch, check_budget="g-2", wait={"action": "cancel"})
    assert await _run(turn=2) == "cancelled"
    hold = ["is_cancelled", "wait", "mark_cancelled"]
    assert s.calls == [*HEAD, *RUN, "frontier", *BOUNDARY, *hold, "conversation_report"]


async def test_an_accepted_budget_gate_dispatches_the_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch, check_budget=["g-2", None], wait={"action": "accept"})
    assert await _run(turn=2) == "delivered"
    hold = ["is_cancelled", "wait", "resume_from_budget", "is_cancelled"]
    assert s.calls == [
        *HEAD,
        *RUN,
        "frontier",
        *BOUNDARY,
        *hold,
        f"start step-{A}",
        "join",
        *DELIVER,
        "remember_task",
    ]


async def test_a_failing_step_fails_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script(monkeypatch, steer_boundary=RuntimeError("boom"))
    assert await _run(turn=2) == "failed"
    assert s.calls == [
        *HEAD,
        *RUN,
        "frontier",
        "pause_held",
        "is_cancelled",
        "steer_boundary",
        "fail_task",
        "conversation_report",
    ]


async def _failure(monkeypatch: pytest.MonkeyPatch, raised: Exception) -> tuple[str, str | None]:
    """The reason and the typed ending the task failed with."""
    Script(monkeypatch, join=raised)
    said: list[tuple[str, str | None]] = []

    async def fail_task(task_id: str, reason: str, turn: int, ended: str | None = None) -> None:
        said.append((reason, ended))

    monkeypatch.setattr(effects, "fail_task", fail_task)
    assert await _run(turn=2) == "failed"
    return said[0]


async def test_a_step_that_could_not_do_the_work_fails_the_task_in_its_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    why = "operator could not do this: the site's bot protection blocked the page"
    assert await _failure(monkeypatch, CouldNotDo(why)) == (why, "could_not")


async def test_a_step_that_produced_nothing_fails_with_no_ending(monkeypatch: pytest.MonkeyPatch) -> None:
    why = "writer produced no document in 3 attempts"
    assert await _failure(monkeypatch, StepFailed(why)) == (why, None)


async def test_an_unexpected_error_still_names_its_type(monkeypatch: pytest.MonkeyPatch) -> None:
    said = await _failure(monkeypatch, TimeoutError("pool exhausted"))
    assert said == ("TimeoutError: pool exhausted", None)


async def test_a_reopened_delivery_runs_the_frontier_again(monkeypatch: pytest.MonkeyPatch) -> None:
    reopened = {"verdict": "reopened", "replaced": [A]}
    delivered = {"verdict": "delivered", "replaced": []}
    s = Script(monkeypatch, deliver_task=[reopened, delivered], frontier=[[A], [], [], []])
    assert await _run(turn=2) == "delivered"
    again = ["frontier", "pause_held", "is_cancelled", "deliver_task"]
    assert s.calls == [
        *HEAD,
        *RUN,
        "frontier",
        *BOUNDARY,
        f"start step-{A}",
        "join",
        *again,
        *DELIVER,
        "remember_task",
    ]
