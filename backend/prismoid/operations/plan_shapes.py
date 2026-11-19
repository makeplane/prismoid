# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The plans code makes without a model: the one-step plan, the Lead's turn step and the fleet.

A leaf: pure builders over values, each returning a `rules.plan.Plan` whose contracts wear the same
clauses a validated step wears. The recipe turns and the floor land here, and so does the Lead's own
step before it runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from prismoid.ai.frames.layout import input_paths
from prismoid.ai.rules.plan import Plan, depth_clause, input_clause
from prismoid.schemas import sizing
from prismoid.schemas.contract import Contract, Criteria
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import PlanEdge, StepKind, StepSpec
from prismoid.schemas.sizing import SizeBucket


def one_step_plan(
    goal: str,
    inputs: Sequence[TaskInput] = (),
    *,
    agent: str = "writer",
    produces: ArtifactType = ArtifactType.DOCUMENT,
    bucket: SizeBucket | None = None,
    units: int | None = None,
    doc_format: str | None = None,
    qa: bool = False,
) -> Plan:
    """Do the thing, publish the thing: the floor every refused plan lands on. `bucket`/`units` size it,
    `doc_format` is the dialed format, and `qa` gates a binary deliverable on its rendered page."""
    paths = input_paths(i.filename for i in inputs)
    marker = f" - {sizing.size_marker(produces, bucket, units)}" if bucket is not None else ""
    spec = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        qa=qa,
        title=f"{goal[:100]}{marker}"[:120],
        contract=Contract(
            goal=goal,
            acceptance=[
                "the output answers the goal as stated",
                *input_clause(paths),
                *depth_clause(produces, bucket, units),
                *(
                    [f"the published file is a .{doc_format} - the user chose this format"]
                    if doc_format is not None
                    else []
                ),
            ],
            inputs=list(paths),
            output=OutputRef(type=produces, title=goal[:80]),
        ),
    )
    return [(StepId(uuid.uuid4()), spec)], [], Criteria(must=[goal], tone="", audience="")


def lead_plan(goal: str) -> Plan:
    """A turn's whole plan before the Lead runs: its one frameless step, the ask its whole brief (ADR-092)."""
    spec = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("lead"),
        lane_role="express",
        verify=False,
        title=goal[:120],
        contract=Contract(goal=goal, acceptance=[], output=None),
    )
    return [(StepId(uuid.uuid4()), spec)], [], Criteria(must=[goal], tone="", audience="")


def fleet_plan(goal: str, inputs: Sequence[TaskInput] = (), *, models: Sequence[str]) -> Plan:
    """Fleet mode: each model in `models` answers the goal on its own pinned writer step, and the writer's
    `fleet_table` part lays the answers side by side, each `produces_for` the table."""
    if not models:
        raise ValueError("a fleet needs at least one model - the caller filters the catalog")

    paths = input_paths(i.filename for i in inputs)
    answers = [(StepId(uuid.uuid4()), _answer(goal, paths, ref)) for ref in models]
    table_id = StepId(uuid.uuid4())
    table = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        lane_role="fleet_table",
        tolerates_missing_inputs=True,  # a lost seat costs the seat, never the fleet
        title=f"Compare {len(answers)} answers",
        contract=Contract(
            # Self-contained: the task's phrasing instructs the answerers, and a table graded against it
            # would be failed for a recommendation it is forbidden to give.
            goal=(
                "Compare the fleet's answers to one another in a table - one row per answer. "
                "Do NOT answer the underlying question yourself, recommend, synthesise, or pick "
                "a winner; lay the answers side by side and let the reader choose."
            ),
            acceptance=[
                "the document is a title, the table, then only 'Where they actually agree' and "
                "'Where they differ' - no preamble, no summary, no recommendation section",
                "a markdown table with one row per answer: the model, its position in one "
                "line, its strongest point, and any caveat it flags - cells are paraphrases, "
                "with no quotation marks inside the table",
                "every quoted phrase appears verbatim in the answer it is attributed to",
                "agreement is reported only where the answers actually agree; disagreement "
                "is stated plainly - no merged recommendation, no vote, no tie-break",
            ],
            output=OutputRef(type=ArtifactType.FLEET_REPORT, title=f"Fleet table - {goal[:60]}"),
        ),
    )
    edges = [PlanEdge(from_step=aid, to_step=table_id, kind="produces_for") for aid, _ in answers]
    return [*answers, (table_id, table)], edges, Criteria(must=[goal], tone="", audience="")


def _answer(goal: str, paths: Sequence[str], ref: str) -> StepSpec:
    """One fleet seat: a writer step pinned to the model `ref`, answering the goal as standard mode would."""
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        model_ref=ref,
        title=f"Answer - {ref}",
        contract=Contract(
            goal=goal,
            acceptance=[
                "the output answers the goal as stated",
                *input_clause(paths),
            ],
            inputs=list(paths),
            output=OutputRef(type=ArtifactType.DOCUMENT, title=f"{goal[:60]} - {ref}"),
        ),
    )
