# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fleet plan template (ADR-010) - a deterministic shape, so tested as one.

Fleet mode is the model-picker inverted: one pinned `writer` step per eligible model - each
producing a plain document exactly as standard mode would - and the writer's table, which depends on
every answer and lays them side by side without forcing agreement. The shape is guaranteed by code, so
the guarantees are pinned here: pins present, answers independent, the table gated by all of them.
"""

from __future__ import annotations

import pytest

from prismoid.operations.plan_shapes import fleet_plan
from prismoid.schemas.plan import StepKind
from tests.support.paths import BACKEND

MODELS = ["anthropic:x", "google:y", "local:z"]


def test_fleet_is_one_pinned_answer_per_model_and_a_table() -> None:
    steps, _edges, criteria = fleet_plan("should we raise prices 10%?", models=MODELS)

    agents = [spec.agent for _, spec in steps]
    assert agents == ["writer"] * (len(MODELS) + 1), agents
    assert [spec.lane_role for _, spec in steps] == [None] * len(MODELS) + ["fleet_table"], "the table's part"
    assert [spec.model_ref for _, spec in steps[:-1]] == MODELS, "each answer pins its model"
    assert steps[-1][1].model_ref is None, "the table routes by class - it is not a seat"
    assert all(spec.kind is StepKind.STEP for _, spec in steps)
    assert criteria.must == ["should we raise prices 10%?"]


def test_answers_publish_documents_like_standard_mode() -> None:
    steps, _, _ = fleet_plan("expand to Europe?", models=MODELS)
    for _, spec in steps[:-1]:
        assert spec.contract is not None and spec.contract.output is not None
        assert spec.contract.output.type.value == "document", "an answer is a plain document"
    table = steps[-1][1]
    assert table.contract is not None and table.contract.output is not None
    assert table.contract.output.type.value == "fleet_report"
    assert table.tolerates_missing_inputs, "a lost seat must cost the seat, never the fleet"
    assert not any(spec.tolerates_missing_inputs for _, spec in steps[:-1])


def test_the_table_gathers_every_answer() -> None:
    steps, edges, _ = fleet_plan("expand to Europe?", models=MODELS)
    table_id = steps[-1][0]
    answer_ids = {sid for sid, _ in steps[:-1]}

    # One `produces_for` edge per answer → table: it both gates the table (waits for all) and
    # mounts each answer into its frame. One edge per pair - the schema forbids two.
    produces = {e.from_step for e in edges if e.to_step == table_id and e.kind == "produces_for"}
    assert produces == answer_ids, "each answer must gate and feed the table"
    assert len(edges) == len(answer_ids), "exactly one edge per answer - no dependency said twice"


def test_answers_do_not_depend_on_each_other() -> None:
    # Independence is the point - answers with edges between them are not independent, and the
    # frontier could not dispatch them as one parallel wave.
    steps, edges, _ = fleet_plan("hire or contract?", models=MODELS)
    answer_ids = {sid for sid, _ in steps[:-1]}
    between = [e for e in edges if e.from_step in answer_ids and e.to_step in answer_ids]
    assert between == [], "answers run in parallel, gated by nothing"


def test_an_empty_fleet_is_refused() -> None:
    # The caller filters the catalog; an empty seat list is a caller bug, said plainly.
    with pytest.raises(ValueError):
        fleet_plan("anything", models=[])


def test_no_answer_is_judged_on_how_it_was_handed_over() -> None:
    """The publish order is the brief's; acceptance holds only what a reader checks."""
    steps, _, _ = fleet_plan("expand to Europe?", models=MODELS)
    for _, spec in steps:
        assert spec.contract is not None
        assert not any("/work" in line or "type='" in line for line in spec.contract.acceptance)


def test_the_table_is_graded_on_the_sections_its_skill_draws() -> None:
    """Found live: the skill adds agree and differ sections after the table, and a contract naming only the
    table had the verifier reject them as synthesis on every attempt."""
    skeleton = (BACKEND / "skills" / "fleet-table" / "SKILL.md").read_text()
    sections = [line.strip()[3:] for line in skeleton.splitlines() if line.startswith("    ## ")]
    assert sections, "the skill's skeleton names its sections"
    steps, _, _ = fleet_plan("expand to Europe?", models=MODELS)
    contract = steps[-1][1].contract
    assert contract is not None
    said = " ".join(contract.acceptance).lower()
    assert all(section.lower() in said for section in sections), sections
