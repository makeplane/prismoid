# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A turn's code-made plans: the `Lane` a recipe answers with, the dial lane, and the look a page gets.

A recipe answers a turn with no model call (ADR-092). `turn/route.py` and the fleet seat build each
`Lane`: the plan's shape plus the routed record `task.planned` wears. A format maker's page step
carries the QA gate whichever path planned it (`gate_visual_steps`). Nothing here reads a row or awaits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from prismoid.ai.agents.base import find, roster
from prismoid.ai.rules.plan import Plan
from prismoid.ai.skills.deliverables import kind_agent
from prismoid.operations.plan_shapes import one_step_plan
from prismoid.schemas import sizing
from prismoid.schemas.contract import Criteria
from prismoid.schemas.ids import StepId
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanEdge, StepSpec

# The kinds a format maker builds as pages a customer opens: such a step carries the QA gate
# (`StepSpec.qa`) so the rendered pages are looked at before delivery. The receipt (`verify`) still
# signs after - the gate adds a look, it removes nothing.
VISUAL_KINDS: Final = frozenset({ArtifactType.DOCUMENT, ArtifactType.DECK})


@dataclass(frozen=True, slots=True)
class Lane:
    """One recipe's answer: the plan's shape, plus the routed record stamped onto `task.planned` -
    the lane, the clause that chose it (`route_source`) and why."""

    steps: list[tuple[StepId, StepSpec]]
    edges: list[PlanEdge]
    criteria: Criteria
    name: str
    source: str
    why: str
    fallback: str = ""  # the plan was refused and its one-step floor runs: why


def lane_of(plan: Plan, name: str, source: str, why: str) -> Lane:
    """A policy plan wearing its routed record."""
    steps, edges, criteria = plan
    return Lane(steps=steps, edges=edges, criteria=criteria, name=name, source=source, why=why)


def looks(agent: str, kind: ArtifactType | None) -> bool:
    """Whether a step's rendered pages are judged before it is done: a format maker building a page kind."""
    definition = find(agent)
    return kind in VISUAL_KINDS and definition is not None and definition.makes_formats


def looked_at(spec: StepSpec) -> bool:
    """`looks` for a planned step, from its agent and its contract's kind."""
    output = spec.contract.output if spec.contract is not None else None
    return spec.agent is not None and output is not None and looks(str(spec.agent), output.type)


def dialed_size(
    scale: str | None, kind: ArtifactType | None, units: int | None
) -> tuple[sizing.SizeBucket, int | None] | None:
    """A size dial as a bucket and a count clamped to it (a bare count picks its bucket); None when unsized."""
    if sizing.normalize(scale) is None and units is None:
        return None
    return sizing.resolve(scale, kind, units)


def dial_lane(
    resolved: ArtifactType,
    *,
    message: str,
    inputs: Sequence[TaskInput],
    deliverable_format: str | None,
    scale: str | None,
    target_pages: int | None,
) -> Lane:
    """The dial IS the plan: a chosen deliverable becomes its one typed step, deterministically, with the
    acceptance clauses and the size band only when the user sized it. `resolved` is the kind they chose."""
    doc_format = deliverable_format if resolved is ArtifactType.DOCUMENT else None
    agent = kind_agent(resolved, roster(plannable=True), doc_format)
    bucket, units = dialed_size(scale, resolved, target_pages) or (None, None)
    plan = one_step_plan(
        message,
        inputs,
        agent=agent,
        produces=resolved,
        bucket=bucket,
        units=units,
        doc_format=doc_format,
        qa=looks(agent, resolved),  # the rendered page is judged before done
    )
    return lane_of(plan, "standard", "dial", f"the {resolved.value} dial is the plan")


def rendered(plan: Plan, doc_format: str) -> Plan:
    """`plan`'s one text step as working machinery, then the format maker's dial step building its document
    as a `doc_format` file word for word: a text-publishing bot's work reaches the dialed format."""
    (text_id, text_spec), *_ = plan[0]
    goal = (
        f"Publish the document in /task/inputs as a {doc_format} document, verbatim: keep every word, "
        "heading and list as written; add nothing, cut nothing, and do not rewrite it."
    )
    lane = dial_lane(
        ArtifactType.DOCUMENT,
        message=goal,
        inputs=(),
        deliverable_format=doc_format,
        scale=None,
        target_pages=None,
    )
    render_id, render_spec = lane.steps[0]
    steps = [
        (text_id, text_spec.model_copy(update={"artifact_role": "working"})),
        (render_id, render_spec.model_copy(update={"title": f"Build the .{doc_format} file"})),
    ]
    return steps, [PlanEdge(from_step=text_id, to_step=render_id, kind="produces_for")], plan[2]


def gate_visual_steps(steps: list[tuple[StepId, StepSpec]]) -> list[tuple[StepId, StepSpec]]:
    """Every format maker step building pages (a document, a deck) carries the QA gate, whichever path planned
    it; pure and idempotent, so a step that has it keeps it."""
    return [
        (sid, spec.model_copy(update={"qa": True}) if looked_at(spec) and not spec.qa else spec)
        for sid, spec in steps
    ]
