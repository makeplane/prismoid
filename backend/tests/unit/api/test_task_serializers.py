# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The task door + the size read-back: council obeys the
single-breath law at the serializer, and `size_marker` is the server-composed string every
surface prints - the CLI and the web chip must never re-derive a unit."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prismoid.api.serializers.tasks import TaskCreateIn, TaskSummaryOut


def _summary(**overrides: object) -> TaskSummaryOut:
    base: dict[str, object] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "goal": "g",
        "title": None,
        "pinned_at": None,
        "archived_at": None,
        "held_at": None,
        "status": "running",
        "mode": "standard",
        "project_id": None,
        "turn": 1,
        "created_at": "2026-08-13T00:00:00Z",
    }
    return TaskSummaryOut.model_validate({**base, **overrides})


def test_fleet_and_council_refuse_big_buckets_at_the_door() -> None:
    # "council" is the pre-rename fleet wire value - same law, same message (review).
    for mode in ("fleet", "council"):
        with pytest.raises(ValidationError, match="single-breath"):
            TaskCreateIn(goal="compare the answers", mode=mode, scale="book")
    TaskCreateIn(goal="compare the answers", mode="fleet", scale="brief")  # the allowed pair
    TaskCreateIn(goal="a long document", mode="standard", scale="book")  # standard unrestricted


def test_size_marker_is_server_composed_with_the_kinds_unit() -> None:
    assert (
        _summary(scale="extended", target_pages=6, deliverable="spreadsheet").size_marker
        == "extended · ~6 sheets"
    )
    assert (
        _summary(scale="extended", target_pages=25, deliverable="deck").size_marker == "extended · ~25 slides"
    )
    # No deliverable = document semantics; no units = the band's middle.
    assert _summary(scale="long").size_marker == "long · ~45 pages"
    assert _summary(scale="auto").size_marker is None  # unresolved: the chip hides
    assert _summary(scale="garbage").size_marker is None  # unknown stored value reads as auto
    assert _summary(scale="extended", deliverable="not-a-kind").size_marker is not None


def test_deliverable_source_rides_the_summary() -> None:
    # The path read-back: who chose the kind reaches every surface; the label
    # itself composes client-side (kit/artifact-icon is the one vocabulary home).
    assert _summary(deliverable="deck", deliverable_source="auto").deliverable_source == "auto"
    assert _summary().deliverable_source is None  # unresolved: the chip hides
