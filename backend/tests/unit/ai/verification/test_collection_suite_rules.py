# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The collection suite's shape and view rules, one problem per row: each mutation of a valid
collection names exactly the problem the report lists, word for word and in document order."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from prismoid.ai.verification.suites.collection import check_shape, check_views

VALID: dict[str, Any] = {
    "collection_version": 1,
    "title": "Plan",
    "fields": [
        {"key": "name", "label": "Name", "type": "text"},
        {"key": "stage", "label": "Stage", "type": "select", "options": ["a", "b"]},
        {"key": "due", "label": "Due", "type": "date"},
        {"key": "end", "label": "End", "type": "date"},
        {"key": "site", "label": "Site", "type": "url"},
    ],
    "rows": [],
    "views": [{"name": "All", "layout": "table"}],
}


def _with(**changes: Any) -> dict[str, Any]:
    doc = copy.deepcopy(VALID)
    doc.update(changes)
    return doc


def _field(**changes: Any) -> list[dict[str, Any]]:
    return [{"key": "x", "label": "X", "type": "text", **changes}]


def test_a_valid_shape_passes_and_counts() -> None:
    check, by_key = check_shape(VALID)
    assert (check["status"], check["detail"]) == ("pass", "5 field(s), 0 row(s), 1 view(s)")
    assert list(by_key) == ["name", "stage", "due", "end", "site"]


def test_a_non_object_is_no_document() -> None:
    check, by_key = check_shape([1])
    assert (check["status"], check["detail"], by_key) == ("fail", "the document is not a JSON object", {})


@pytest.mark.parametrize(
    ("doc", "detail"),
    [
        (_with(collection_version=2), "collection_version must be 1, got 2"),
        (_with(title="  "), "title must be a non-empty string ≤200 chars"),
        (_with(fields=[]), "fields must be a non-empty list (≤64)"),
        (_with(fields=["x"]), "a field is not an object"),
        (_with(fields=_field(key="Bad")), "field key 'Bad' is not lowercase snake_case"),
        (_with(fields=_field() + _field()), "field key 'x' is duplicated"),
        (_with(fields=_field(label="")), "field 'x': label must be a non-empty string ≤80 chars"),
        (_with(fields=_field(type="blob")), "field 'x': unknown type 'blob'"),
        (_with(fields=_field(options=["a"])), "field 'x' has options but is not a select"),
        (_with(fields=_field(type="select")), "select field 'x' needs options"),
        (_with(rows={}), "rows must be a list (≤5000)"),
        (_with(views=[]), "views must be a non-empty list (≤12)"),
        (
            _with(collection_version=0, fields=_field(key="Bad") + _field(label=""), views=[]),
            "collection_version must be 1, got 0; field key 'Bad' is not lowercase snake_case; "
            "field 'x': label must be a non-empty string ≤80 chars; views must be a non-empty list (≤12)",
        ),
    ],
)
def test_each_shape_problem_is_named(doc: dict[str, Any], detail: str) -> None:
    check, _by_key = check_shape(doc)
    assert (check["status"], check["detail"]) == ("fail", detail)


def test_a_badly_keyed_field_is_left_out_of_the_fields_by_key() -> None:
    _check, by_key = check_shape(_with(fields=_field(key="Bad") + _field(label="")))
    assert list(by_key) == ["x"]


def _views(*views: object) -> str:
    doc = _with(views=list(views))
    _shape, by_key = check_shape(doc)
    check = check_views(doc, by_key)
    return str(check["detail"]) if check["status"] == "fail" else "pass"


@pytest.mark.parametrize(
    ("views", "detail"),
    [
        (
            [
                {"name": "B", "layout": "board", "group_by": "stage", "column_width": 300},
                {"name": "G", "layout": "gantt", "start_field": "due", "end_field": "end"},
                {"name": "C", "layout": "calendar", "date_field": "due"},
                {"name": "P", "layout": "gallery", "cover_field": "site"},
                {"name": "T", "layout": "table", "columns": ["name"], "widths": {"name": 320.0}},
            ],
            "pass",
        ),
        (["x"], "a view is not an object"),
        ([{"layout": "table"}], "a view has no name"),
        ([{"name": "A", "layout": "table"}] * 2, "view name 'A' is duplicated"),
        ([{"name": "A", "layout": "pie", "group_by": "nope"}], "view 'A': unknown layout 'pie'"),
        ([{"name": "A", "layout": "table", "group_by": "nope"}], "view 'A': group_by 'nope' is not a field"),
        (
            [{"name": "A", "layout": "table", "sub_group_by": "due"}],
            "view 'A': sub_group_by 'due' must be one of: checkbox, person, select",
        ),
        (
            [{"name": "A", "layout": "table", "cover_field": "name"}],
            "view 'A': cover_field 'name' must be one of: url",
        ),
        (
            [{"name": "A", "layout": "table", "sort": [{"field": "nope"}], "filter": [{"field": "gone"}]}],
            "view 'A': sort field 'nope' is not a field; view 'A': filter field 'gone' is not a field",
        ),
        ([{"name": "A", "layout": "table", "columns": ["nope"]}], "view 'A': column 'nope' is not a field"),
        ([{"name": "A", "layout": "table", "widths": []}], "view 'A': widths must be an object"),
        (
            [{"name": "A", "layout": "table", "widths": {"nope": True}}],
            "view 'A': widths key 'nope' is not a field; view 'A': width for 'nope' must be 80–800 px",
        ),
        (
            [{"name": "A", "layout": "table", "column_width": 100}],
            "view 'A': column_width must be 200–600 px",
        ),
        (
            [{"name": "A", "layout": "board"}],
            "view 'A': a board groups by definition - group_by is required",
        ),
        ([{"name": "A", "layout": "calendar"}], "view 'A': a calendar needs date_field"),
        (
            [{"name": "A", "layout": "gantt", "start_field": "due"}],
            "view 'A': a gantt needs start_field and end_field",
        ),
    ],
)
def test_each_view_problem_is_named(views: list[object], detail: str) -> None:
    assert _views(*views) == detail
