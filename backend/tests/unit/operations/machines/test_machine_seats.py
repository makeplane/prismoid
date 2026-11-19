# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Seats keep throwaway frames: decided by the plan's shape, and by dispatch."""

from typing import Any

import pytest

from prismoid.jobs import effects, queues
from prismoid.operations import execution
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepKind, StepSpec


def _spec(**kw: Any) -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        title="s",
        contract=Contract(
            goal="g", acceptance=["a"], inputs=[], output=OutputRef(type=ArtifactType.DOCUMENT, title="t")
        ),
        **kw,
    )


def test_fleet_seats_sections_and_scatter_seats_are_seats_by_shape() -> None:
    assert execution.is_seat(_spec(model_ref="anthropic:claude-x"))
    assert execution.is_seat(_spec(lane_role="section"))
    assert execution.is_seat(_spec(lane_role="gather"))
    assert not execution.is_seat(_spec())
    assert not execution.is_seat(_spec(lane_role="harmonize"))
    assert not execution.is_seat(_spec(lane_role="synthesis"))


async def test_a_concurrent_frontier_dispatches_its_steps_as_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[Any, ...]] = []

    class Handle:
        async def get_result(self, **kwargs: Any) -> str:
            return "ok"

    async def enqueue(fn: Any, *args: Any) -> Handle:
        sent.append(args)
        return Handle()

    # `dispatch_frontier` imports the queue from `jobs/queues.py` at call time: patch the object.
    monkeypatch.setattr(queues.step_queue, "enqueue_async", enqueue)
    await effects.dispatch_frontier(
        "00000000-0000-0000-0000-000000000001",
        ["00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"],
    )
    assert len(sent) == 2 and all(args[-1] is True for args in sent)
