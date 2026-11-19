# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA gate reaches file deliverables (ADR-034): a one-step maker plan for pages carries `qa=True`
with its receipt intact, a writer's prose step does not, and the planner-path stamp is pure +
idempotent."""

from __future__ import annotations

import uuid

from prismoid.operations.plan_shapes import one_step_plan
from prismoid.operations.turn.recipes import gate_visual_steps
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepSpec


def test_one_step_plan_carries_qa_when_asked_and_keeps_verify() -> None:
    steps, _, _ = one_step_plan("a memo", agent="maker", produces=ArtifactType.DOCUMENT, qa=True)
    spec = steps[0][1]
    assert spec.qa is True and spec.verify is True
    steps, _, _ = one_step_plan("a memo", agent="writer")
    assert steps[0][1].qa is False


def test_gate_visual_steps_stamps_only_a_makers_pages_and_is_idempotent() -> None:
    """Pages a customer opens (a maker's document or deck) are looked at; a workbook, prose, research
    and a stored step naming a retired maker are judged by what they are, not by their name."""

    def step(agent: str, kind: ArtifactType | None, qa: bool = False) -> tuple[StepId, StepSpec]:
        output = OutputRef(type=kind, title="t") if kind is not None else None
        contract = Contract(goal="g", acceptance=[], output=output)
        return StepId(uuid.uuid4()), StepSpec(agent=AgentName(agent), contract=contract, qa=qa)

    steps = [
        step("researcher", None),
        step("maker", ArtifactType.DOCUMENT),
        step("maker", ArtifactType.DECK),
        step("maker", ArtifactType.SPREADSHEET),
        step("writer", ArtifactType.DOCUMENT),
        step("maker", ArtifactType.DOCUMENT, qa=True),
    ]
    once = gate_visual_steps(steps)
    assert [s.qa for _, s in once] == [False, True, True, False, False, True]
    assert [s.qa for _, s in gate_visual_steps(once)] == [False, True, True, False, False, True]
