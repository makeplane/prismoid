# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A user's dial is its own plan, from values alone: one step on the kind's agent, with the QA gate where the
rendered page is judged. No I/O - the turn's facts are arguments."""

from __future__ import annotations

import pytest

from prismoid.ai.agents.base import roster
from prismoid.ai.skills import deliverables
from prismoid.operations.turn import recipes
from prismoid.schemas.outputs import ArtifactType


def _dial(
    resolved: ArtifactType,
    *,
    deliverable_format: str | None = None,
    message: str = "a memo on the roadmap",
) -> recipes.Lane:
    return recipes.dial_lane(
        resolved,
        message=message,
        inputs=(),
        deliverable_format=deliverable_format,
        scale=None,
        target_pages=None,
    )


# ── the dial ──────────────────────────────────────────────────────────────────────────────────


def test_dial_lands_a_document_on_the_writer_and_says_so() -> None:
    lane = _dial(ArtifactType.DOCUMENT)
    assert (lane.name, lane.source, lane.why) == ("standard", "dial", "the document dial is the plan")
    spec = lane.steps[0][1]
    assert str(spec.agent) == "writer" and spec.qa is False


def test_a_formatted_document_goes_to_the_maker_with_the_qa_gate() -> None:
    lane = _dial(ArtifactType.DOCUMENT, deliverable_format="pdf")
    spec = lane.steps[0][1]
    assert str(spec.agent) == "maker" and spec.qa is True


def test_a_format_on_a_non_document_kind_is_ignored() -> None:
    lane = _dial(ArtifactType.DECK, deliverable_format="pdf")
    spec = lane.steps[0][1]
    assert str(spec.agent) == "maker" and spec.qa is True


# A proposed card's Start (2026-11-10): the spreadsheet dial on an ask for current prices and sources.
SOURCED = "A spreadsheet of current on-road prices for three SUVs, and cite sources"


@pytest.mark.parametrize(
    ("kind", "fmt", "agent"),
    [
        (ArtifactType.SPREADSHEET, None, "maker"),
        (ArtifactType.DOCUMENT, "pdf", "maker"),
        (ArtifactType.DOCUMENT, None, "writer"),
    ],
)
def test_a_dial_on_an_ask_for_web_facts_stays_the_plan(
    kind: ArtifactType, fmt: str | None, agent: str
) -> None:
    """Every agent a dial seats reads the web itself, so its one step finds the sources it cites."""
    lane = _dial(kind, deliverable_format=fmt, message=SOURCED)
    assert str(lane.steps[0][1].agent) == agent


def test_a_dial_on_the_users_own_material_stays_the_plan() -> None:
    lane = _dial(ArtifactType.SPREADSHEET, message="Totals by region and month from the attached CSV")
    spec = lane.steps[0][1]
    assert str(spec.agent) == "maker" and spec.qa is False, "a workbook is not pages"
    assert lane.why == "the spreadsheet dial is the plan"


def test_a_prose_document_lands_on_the_writer_and_every_formatted_file_on_the_maker() -> None:
    """The format skills' manifests are the kind table: every kind a format skill builds is the maker's,
    a document in one of their formats included; a document with no file format is the writer's."""
    plannable = roster(plannable=True)
    assert deliverables.kind_agent(ArtifactType.DOCUMENT, plannable) == "writer"
    assert deliverables.kind_agent(ArtifactType.DOCUMENT, plannable, "md") == "writer"
    for fmt in ("docx", "pdf", "html"):
        assert deliverables.kind_agent(ArtifactType.DOCUMENT, plannable, fmt) == "maker"
    for kind in (ArtifactType.SPREADSHEET, ArtifactType.DECK, ArtifactType.CODE, ArtifactType.COLLECTION):
        assert deliverables.kind_agent(kind, plannable) == "maker"


def test_a_page_count_without_a_bucket_sizes_the_dialed_step() -> None:
    # A page count with scale auto must still size the dialed step.
    lane = recipes.dial_lane(
        ArtifactType.DOCUMENT,
        message="a guide",
        inputs=(),
        deliverable_format=None,
        scale="auto",
        target_pages=20,
    )
    spec = lane.steps[0][1]
    assert spec.contract is not None
    assert "~20 pages" in spec.title
    assert any("20" in clause for clause in spec.contract.acceptance[1:])


# ── a text step built into a dialed file format ───────────────────────────────────────────────


def test_a_text_step_hands_its_document_to_the_format_maker() -> None:
    """A text-publishing step's markdown is machinery; the dialed format is the maker's verbatim build, after it."""
    text = _dial(ArtifactType.DOCUMENT)
    steps, edges, criteria = recipes.rendered((text.steps, text.edges, text.criteria), "docx")
    (text_id, text_spec), (render_id, render_spec) = steps
    assert text_spec.artifact_role == "working" and text_spec.agent == text.steps[0][1].agent
    assert str(render_spec.agent) == "maker" and render_spec.qa is True
    assert render_spec.artifact_role == "deliverable" and render_spec.contract is not None
    assert "the published file is a .docx - the user chose this format" in render_spec.contract.acceptance
    assert [(e.from_step, e.to_step) for e in edges] == [(text_id, render_id)]
    assert criteria == text.criteria
