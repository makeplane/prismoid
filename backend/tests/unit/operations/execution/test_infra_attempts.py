# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a dead attempt is booked: a model attempt, or an infra attempt the weather took - capped on
its own, and a provider death backs off before the next attempt. Pure: flags in, a verdict out."""

from __future__ import annotations

import pytest

from prismoid.db.models.plan import PlanStep, paced
from prismoid.errors import ModelIncapable
from prismoid.operations.execution.infra import Death, backoff_s, booked, spent
from prismoid.operations.execution.record import hand_back
from prismoid.schemas.plan import StepState


@pytest.mark.parametrize(
    ("context", "death"),
    [
        ({}, Death(infra=False, counted=False, backoff=False)),  # the model could not comply
        ({"provider": True}, Death(infra=True, counted=True, backoff=True)),  # a 529, a dead chain
        ({"provider": True, "refused": True}, Death(infra=False, counted=False, backoff=False)),  # a 400/413
        ({"infra": True}, Death(infra=True, counted=True, backoff=False)),  # a sandbox, a wall clock
        (
            {"infra": True, "machine": True},
            Death(infra=True, counted=False, backoff=False),
        ),  # paced by wait_s
    ],
)
def test_each_death_is_booked_by_what_killed_it(context: dict[str, bool], death: Death) -> None:
    dead = ModelIncapable("dead")
    dead.context.update(context)
    assert booked(dead) == death


def test_the_cap_allows_its_retries_then_stops() -> None:
    assert not spent(3, 3), "three infra deaths, three infra retries"
    assert spent(4, 3)


def test_the_backoff_doubles_to_its_ceiling() -> None:
    waits = [backoff_s(n, base=30, ceiling=600) for n in range(1, 7)]
    assert waits == [30, 60, 120, 240, 480, 600]


def test_no_base_means_no_wait() -> None:
    assert backoff_s(2, base=0, ceiling=600) == 0


def test_a_provider_death_waits_seconds_not_minutes() -> None:
    """The gateway has already retried the request on its Retry-After by the time a step dies, so
    the step's own wait is short: an express answer must not sit out half a minute per blip."""
    from prismoid.conf import WorkerSettings

    worker = WorkerSettings()
    waits = [
        backoff_s(n, base=worker.provider_backoff_s, ceiling=worker.provider_backoff_max_s)
        for n in range(1, worker.max_infra_attempts + 1)
    ]
    assert waits[0] <= 5
    assert sum(waits) <= 60


def test_a_step_waiting_out_its_pace_is_in_flight_not_unstarted() -> None:
    """A provider death hands the workflow a pace and leaves the step `ready`: a replan must read it
    as in flight. The record the ladder writes (`hand_back`) is the one the row reads."""
    step = PlanStep(state=StepState.READY.value, resume_state=None)
    assert not step.waiting_out_a_pace(), "a fresh ready step is unstarted"

    hand_back(step, "pace:2026-11-06T13:00:05+00:00")
    assert step.waiting_out_a_pace()

    hand_back(step, "park:gate-1")
    assert not step.waiting_out_a_pace(), "parked on a gate, not paced"
    assert not paced(StepState.RUNNING.value, {"handbacks": ["pace:x"]}), "re-entered: running again"
