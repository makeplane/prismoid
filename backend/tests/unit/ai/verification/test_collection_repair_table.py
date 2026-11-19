# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The collection normalizer, one input per arm: each field type's coercer, each view rule and each
field rule pinned to the exact value it keeps and the exact note it records, in order. The notes
ride the artifact's provenance, so their wording is part of the contract."""

from __future__ import annotations

import json
from typing import Any

import pytest

from prismoid.schemas.collection_repair import repair_collection

FIELDS: list[dict[str, Any]] = [
    {"key": "name", "type": "text"},
    {"key": "amount", "type": "number"},
    {"key": "done", "type": "checkbox"},
    {"key": "due", "type": "date"},
    {"key": "stage", "type": "select", "options": ["todo", "done"]},
    {"key": "tags", "type": "multi_select", "options": ["a"]},
    {"key": "site", "type": "url"},
    {"key": "end", "type": "date"},
]
NO_VIEWS = "no usable views - a default table view was added"


def _repair(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    r = repair_collection(json.dumps({"collection_version": 1, "title": "T", **doc}).encode())
    assert r.content is not None, r.repairs
    return r.content, r.repairs


CELLS: list[tuple[str, object, dict[str, object], list[str] | None, str | None]] = [
    ("name", "plain", {"name": "plain"}, None, None),
    ("name", True, {"name": "true"}, None, "boolean rendered as text"),
    ("name", 4, {"name": "4"}, None, "number rendered as text"),
    ("name", ["a", "b"], {"name": "a, b"}, None, "list joined into text"),
    ("name", {"k": 1}, {}, None, "unreadable dict dropped"),
    ("site", 2.5, {"site": "2.5"}, None, "number rendered as text"),
    ("amount", 7, {"amount": 7.0}, None, None),
    ("amount", True, {}, None, "boolean is not a number - dropped"),
    ("amount", "$1,200%", {"amount": 1200.0}, None, "'$1,200%' coerced to a number"),
    ("amount", "abc", {}, None, "'abc' is not a number - dropped"),
    ("amount", [1], {}, None, "unreadable list dropped"),
    ("amount", None, {"amount": None}, None, None),
    ("done", False, {"done": False}, None, None),
    ("done", " Yes ", {"done": True}, None, "' Yes ' coerced to a checkbox"),
    ("done", "maybe", {}, None, "'maybe' is not true/false - dropped"),
    ("due", 5, {}, None, "date must be an ISO string - dropped"),
    ("due", "2024-03-01", {"due": "2024-03-01"}, None, None),
    ("due", "2024-03-01T10:00:00", {"due": "2024-03-01"}, None, "timestamp '2024-03-01T10:00:00' trimmed to its date"),
    ("due", "soon", {}, None, "'soon' is not an ISO date - dropped"),
    ("due", "1800-01-01", {}, None, "'1800-01-01' is outside 1900–2100 - dropped"),
    ("stage", "todo", {"stage": "todo"}, ["todo", "done"], None),
    ("stage", "shipped", {"stage": "shipped"}, ["todo", "done", "shipped"], "stray value 'shipped' added to the field's options"),
    ("stage", 3, {}, ["todo", "done"], "select value must be a string - dropped"),
    ("tags", ["a"], {"tags": ["a"]}, ["a"], None),
    ("tags", "a", {"tags": ["a"]}, ["a"], "single value wrapped as a list"),
    ("tags", ["a", "b", "b"], {"tags": ["a", "b", "b"]}, ["a", "b"], "stray values ['b', 'b'] added to the field's options"),
    ("tags", "z", {"tags": ["z"]}, ["a", "z"], "stray values ['z'] added to the field's options"),
    ("tags", [1], {}, ["a"], "multi-select must be a list of strings - dropped"),
]  # fmt: skip


@pytest.mark.parametrize(("key", "value", "cells", "options", "note"), CELLS)
def test_each_field_type_coerces_a_cell_and_notes_it(
    key: str, value: object, cells: dict[str, object], options: list[str] | None, note: str | None
) -> None:
    content, repairs = _repair({"fields": FIELDS, "rows": [{"id": "r1", "cells": {key: value}}]})
    assert content["rows"][0]["cells"] == cells
    if options is not None:
        assert next(f["options"] for f in content["fields"] if f["key"] == key) == options
    assert repairs == ([f"row 'r1', field {key!r}: {note}"] if note else []) + [NO_VIEWS]


def test_a_cell_for_an_unknown_field_is_dropped_with_a_note() -> None:
    content, repairs = _repair({"fields": FIELDS, "rows": [{"id": "r1", "cells": {"ghost": 1}}]})
    assert content["rows"][0]["cells"] == {}
    assert repairs == ["row 'r1': cell for unknown field 'ghost' dropped", NO_VIEWS]


def _set(view: dict[str, Any]) -> dict[str, Any]:
    """The keys the normalizer set (the contract's defaults left out)."""
    return {k: v for k, v in view.items() if v not in (None, [], {})}


VIEWS: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = [
    ({"name": "B", "layout": "board"}, {"name": "B", "layout": "board", "group_by": "done"}, ["view 'B': board grouped by 'done'"]),
    (
        {"name": "B", "layout": "board", "group_by": "stage", "sub_group_by": "name"},
        {"name": "B", "layout": "board", "group_by": "stage"},
        ["view 'B': sub_group_by 'name' is not a usable field - dropped"],
    ),
    ({"name": "C", "layout": "calendar"}, {"name": "C", "layout": "calendar", "date_field": "due"}, ["view 'C': calendar dated by 'due'"]),
    (
        {"name": "C", "layout": "calendar", "date_field": "stage"},
        {"name": "C", "layout": "calendar", "date_field": "due"},
        ["view 'C': date_field 'stage' is not a usable field - dropped", "view 'C': calendar dated by 'due'"],
    ),
    (
        {"name": "G", "layout": "gantt"},
        {"name": "G", "layout": "gantt", "start_field": "due", "end_field": "end"},
        ["view 'G': gantt span filled as 'due' → 'end'"],
    ),
    (
        {"name": "G", "layout": "gantt", "start_field": "end"},
        {"name": "G", "layout": "gantt", "start_field": "end", "end_field": "due"},
        ["view 'G': gantt span filled as 'end' → 'due'"],
    ),
    (
        {"name": "G", "layout": "gantt", "start_field": "due", "end_field": "end", "cover_field": "site"},
        {"name": "G", "layout": "gantt", "start_field": "due", "end_field": "end", "cover_field": "site"},
        [],
    ),
    ({"name": "X", "layout": "weird"}, {"name": "X", "layout": "table"}, ["view 'X': unknown layout 'weird' → table"]),
    (
        {"name": "", "layout": "table", "cover_field": "name"},
        {"name": "View 1", "layout": "table"},
        ["view 'View 1': cover_field 'name' is not a usable field - dropped"],
    ),
    (
        {"name": "S", "layout": "table", "sort": [{"field": "amount", "direction": "desc"}, {"field": "name", "direction": "up"}, {"field": "ghost"}, 3]},
        {"name": "S", "layout": "table", "sort": [{"field": "amount", "direction": "desc"}, {"field": "name", "direction": "asc"}]},
        ["view 'S': sort rules referencing unknown fields were dropped"],
    ),
    ({"name": "S", "layout": "table", "sort": [{"field": "ghost"}]}, {"name": "S", "layout": "table"}, ["view 'S': sort rules referencing unknown fields were dropped"]),
    (
        {"name": "F", "layout": "table", "filter": [{"field": "amount", "op": "gte", "value": 3}, {"field": "name", "op": "bad", "value": "x"}, {"field": "name", "op": "eq", "value": [1]}]},
        {"name": "F", "layout": "table", "filter": [{"field": "amount", "op": "gte", "value": 3.0}]},
        ["view 'F': filters with unknown fields or ops were dropped"],
    ),
    (
        {"name": "K", "layout": "table", "columns": ["name", "ghost", 5]},
        {"name": "K", "layout": "table", "columns": ["name"]},
        ["view 'K': columns referencing unknown fields were dropped"],
    ),
    (
        {"name": "W", "layout": "table", "widths": {"name": 50, "amount": 300.7, "ghost": 100, "done": True, "due": "x"}},
        {"name": "W", "layout": "table", "widths": {"name": 80, "amount": 300}},
        ["view 'W': widths outside 80–800 or on unknown fields were fixed"],
    ),
    ({"name": "W", "layout": "table", "widths": {"name": 200}}, {"name": "W", "layout": "table", "widths": {"name": 200}}, []),
    (
        {"name": "Q", "layout": "board", "column_width": 900},
        {"name": "Q", "layout": "board", "group_by": "done", "column_width": 600},
        ["view 'Q': board grouped by 'done'", "view 'Q': column_width clamped to 600"],
    ),
    (
        {"name": "Q", "layout": "board", "column_width": 250.5},
        {"name": "Q", "layout": "board", "group_by": "done", "column_width": 250},
        ["view 'Q': board grouped by 'done'"],
    ),
    (
        {"name": "Q", "layout": "board", "column_width": True},
        {"name": "Q", "layout": "board", "group_by": "done"},
        ["view 'Q': board grouped by 'done'"],
    ),
]  # fmt: skip


@pytest.mark.parametrize(("raw", "view", "notes"), VIEWS)
def test_each_view_rule_repairs_and_notes_it(
    raw: dict[str, Any], view: dict[str, Any], notes: list[str]
) -> None:
    content, repairs = _repair({"fields": FIELDS, "views": [raw]})
    assert [_set(v) for v in content["views"]] == [view]
    assert repairs == notes


def test_a_repeated_view_name_is_numbered_and_noted_under_its_new_name() -> None:
    raw = {"name": "S", "layout": "table", "sort": [{"field": "ghost"}]}
    content, repairs = _repair({"fields": FIELDS, "views": [raw, raw]})
    assert [v["name"] for v in content["views"]] == ["S", "S (2)"]
    assert repairs == [
        "view 'S': sort rules referencing unknown fields were dropped",
        "view 'S (2)': sort rules referencing unknown fields were dropped",
    ]


@pytest.mark.parametrize(
    ("fields", "raw", "note"),
    [
        ([{"key": "name", "type": "text"}], {"layout": "board"}, "board without a groupable field → table"),
        ([{"key": "name", "type": "text"}], {"layout": "calendar"}, "calendar without a date field → table"),
        ([{"key": "name", "type": "text"}], {"layout": "gantt"}, "gantt without two date fields → table"),
        (
            [{"key": "due", "type": "date"}],
            {"layout": "gantt", "start_field": "due"},
            "gantt without two date fields → table",
        ),
    ],
)
def test_a_layout_with_no_fitting_field_demotes_to_a_table(
    fields: list[dict[str, Any]], raw: dict[str, Any], note: str
) -> None:
    content, repairs = _repair({"fields": fields, "views": [{"name": "V", **raw}]})
    assert [_set(v) for v in content["views"]] == [{"name": "V", "layout": "table"}]
    assert repairs == [f"view 'V': {note}"]


FIELD_CASES: list[tuple[list[object], list[dict[str, object]], list[str]]] = [
    (
        [{"key": "Due Date", "type": "date"}],
        [{"key": "due_date", "label": "due date", "type": "date", "options": [], "unit": ""}],
        ["field key 'Due Date' → 'due_date'"],
    ),
    (
        [{"key": "a", "type": "text"}, {"key": "a", "type": "number"}],
        [
            {"key": "a", "label": "a", "type": "text", "options": [], "unit": ""},
            {"key": "a_1", "label": "a 1", "type": "number", "options": [], "unit": ""},
        ],
        ["duplicate field key 'a' → 'a_1'"],
    ),
    (
        [{"key": "a", "type": "rainbow"}],
        [{"key": "a", "label": "a", "type": "text", "options": [], "unit": ""}],
        ["field 'a': unknown type 'rainbow' → text"],
    ),
    (
        [{"key": "a", "type": "text", "options": ["x", "x", " "]}],
        [{"key": "a", "label": "a", "type": "text", "options": [], "unit": ""}],
        ["field 'a': options on a text field were dropped"],
    ),
    (
        [
            {
                "key": "a",
                "type": "select",
                "options": ["x", "x", " "],
                "label": " Big ",
                "unit": "kilograms!!!!",
            }
        ],
        [{"key": "a", "label": "Big", "type": "select", "options": ["x"], "unit": "kilograms!!!"}],
        [],
    ),
    (
        [{"key": "1st", "type": "text"}, 7],
        [{"key": "f_1st", "label": "f 1st", "type": "text", "options": [], "unit": ""}],
        ["field key '1st' → 'f_1st'", "field #2 was not an object - dropped"],
    ),
    (
        [{"key": "", "type": "text"}],
        [{"key": "field_1", "label": "field 1", "type": "text", "options": [], "unit": ""}],
        ["field key '' → 'field_1'"],
    ),
    (
        [{"key": "a", "type": "select"}],
        [{"key": "a", "label": "a", "type": "text", "options": [], "unit": ""}],
        ["select field 'a' has no options → text"],
    ),
]


@pytest.mark.parametrize(("raw", "fields", "notes"), FIELD_CASES)
def test_each_field_rule_repairs_and_notes_it(
    raw: list[object], fields: list[dict[str, object]], notes: list[str]
) -> None:
    content, repairs = _repair({"fields": raw})
    assert content["fields"] == fields
    assert repairs == [*notes, NO_VIEWS]
