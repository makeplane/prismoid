# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One collection, many lenses (ADR-015). The `collection` artifact's published bytes.

A collection is fields + rows + named **views** - board, table, list, gallery, gantt, calendar -
the Notion/Airtable model and Plane's own layouts: views are *projections over one dataset*,
never separate formats. Adding a layout is a renderer and a schema revision, never a new
artifact kind.

**This model is the one definition.** The in-frame verifier suite
(`ai/verification/suites/collection.py`) is a stdlib mirror of these rules - it cannot import
pydantic inside a frame - and the lockstep test (`tests/unit/test_collection_suite.py`) pins the
two together mutation by mutation, so the mirror cannot drift. The web's zod façade is pinned the
same way, against the frozen `docs/api/collection.schema.json`.

`collection_version` is a gate, not a decoration: additive changes ride within 1; anything that
would re-shape existing bytes is a 2, and a viewer that only speaks 1 says so honestly.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"  # ISO-8601 date, 1900–2100
    SELECT = "select"  # one of `options`
    MULTI_SELECT = "multi_select"  # a subset of `options`
    CHECKBOX = "checkbox"
    URL = "url"
    PERSON = "person"


# The field types a board may group by: a column set must be enumerable and small. Grouping by a
# free-text field is a board with one column per typo.
GROUPABLE: frozenset[FieldType] = frozenset({FieldType.SELECT, FieldType.PERSON, FieldType.CHECKBOX})

CellValue = str | float | bool | list[str] | None


class CollectionField(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = Field(min_length=1, max_length=80)
    type: FieldType
    options: list[str] = Field(default_factory=list)  # select/multi_select only; else must be []
    unit: str = Field(default="", max_length=12)  # display hint ("$", "%"); cells stay bare numbers


class ViewLayout(StrEnum):
    TABLE = "table"
    BOARD = "board"
    LIST = "list"
    GALLERY = "gallery"
    GANTT = "gantt"
    CALENDAR = "calendar"


class SortSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    direction: Literal["asc", "desc"] = "asc"


class FilterSpec(BaseModel):
    """Carried and validated in v1; applied by future viewers. A published collection is settled
    facts - the agent publishes the rows it means, and a filter is a lens description."""

    model_config = ConfigDict(frozen=True)

    field: str
    op: Literal["eq", "ne", "contains", "gte", "lte", "empty", "not_empty"]
    value: str | float | bool | None = None


class CollectionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=80)
    layout: ViewLayout
    group_by: str | None = None  # BOARD: required, a GROUPABLE field
    sub_group_by: str | None = None  # BOARD only, a GROUPABLE field
    sort: list[SortSpec] = Field(default_factory=list[SortSpec])
    filter: list[FilterSpec] = Field(default_factory=list[FilterSpec])
    date_field: str | None = None  # CALENDAR: required, a date field
    start_field: str | None = None  # GANTT: required, a date field
    end_field: str | None = None  # GANTT: required, a date field
    cover_field: str | None = None  # GALLERY: optional, a url field
    columns: list[str] = Field(default_factory=list)  # visible fields, in order; [] = all fields
    widths: dict[str, int] = Field(default_factory=dict)  # px per field key; 80–800
    column_width: int | None = None  # BOARD: the one shared column width, px; 200–600


class CollectionRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    cells: dict[str, CellValue] = Field(default_factory=dict)  # keyed by field key; sparse is fine


class Collection(BaseModel):
    """The whole artifact. Frozen, versioned, and self-consistent by construction - the
    validator below is the contract the verifier re-checks in the frame."""

    model_config = ConfigDict(frozen=True)

    collection_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=200)
    fields: list[CollectionField] = Field(min_length=1, max_length=64)
    rows: list[CollectionRow] = Field(default_factory=list[CollectionRow], max_length=5000)
    views: list[CollectionView] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _consistent(self) -> Collection:
        by_key = {f.key: f for f in self.fields}
        if len(by_key) != len(self.fields):
            raise ValueError("field keys must be unique")

        for field in self.fields:
            if field.options and field.type not in (FieldType.SELECT, FieldType.MULTI_SELECT):
                raise ValueError(f"field {field.key!r} has options but is not a select")
            if not field.options and field.type in (FieldType.SELECT, FieldType.MULTI_SELECT):
                raise ValueError(f"select field {field.key!r} needs options")

        seen_rows: set[str] = set()
        for row in self.rows:
            if row.id in seen_rows:
                raise ValueError(f"row id {row.id!r} is duplicated")
            seen_rows.add(row.id)
            for key, value in row.cells.items():
                field = by_key.get(key)
                if field is None:
                    raise ValueError(f"row {row.id!r} has a cell for unknown field {key!r}")
                _check_cell(field, value, row.id)

        seen_views: set[str] = set()
        for view in self.views:
            if view.name in seen_views:
                raise ValueError(f"view name {view.name!r} is duplicated")
            seen_views.add(view.name)
            _check_view(view, by_key)
        return self


def _check_cell(field: CollectionField, value: CellValue, row_id: str) -> None:
    """A cell either matches its field's type or is None (sparse). The mirror in the frame suite
    re-derives exactly this table - change one, change both, and the lockstep test will insist."""
    if value is None:
        return
    where = f"row {row_id!r}, field {field.key!r}"
    match field.type:
        case FieldType.TEXT | FieldType.URL | FieldType.PERSON:
            if not isinstance(value, str):
                raise ValueError(f"{where}: expected a string, got {type(value).__name__}")
        case FieldType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{where}: expected a number, got {type(value).__name__}")
        case FieldType.CHECKBOX:
            if not isinstance(value, bool):
                raise ValueError(f"{where}: expected true/false, got {type(value).__name__}")
        case FieldType.DATE:
            if not isinstance(value, str):
                raise ValueError(f"{where}: expected an ISO date string")
            _check_date(value, where)
        case FieldType.SELECT:
            if not isinstance(value, str) or value not in field.options:
                raise ValueError(f"{where}: {value!r} is not one of the field's options")
        case FieldType.MULTI_SELECT:
            if not isinstance(value, list) or any(v not in field.options for v in value):
                raise ValueError(f"{where}: every value must be one of the field's options")


def _check_date(value: str, where: str) -> None:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{where}: {value!r} is not an ISO-8601 date") from None
    if not 1900 <= parsed.year <= 2100:
        raise ValueError(f"{where}: {value!r} is outside 1900–2100 - almost certainly a typo")


def _check_view(view: CollectionView, by_key: dict[str, CollectionField]) -> None:
    name = view.name

    def field_of(ref: str | None, role: str, *, types: frozenset[FieldType] | None = None) -> None:
        if ref is None:
            return
        field = by_key.get(ref)
        if field is None:
            raise ValueError(f"view {name!r}: {role} {ref!r} is not a field")
        if types is not None and field.type not in types:
            legal = ", ".join(sorted(t.value for t in types))
            raise ValueError(f"view {name!r}: {role} {ref!r} must be one of: {legal}")

    field_of(view.group_by, "group_by", types=GROUPABLE)
    field_of(view.sub_group_by, "sub_group_by", types=GROUPABLE)
    field_of(view.date_field, "date_field", types=frozenset({FieldType.DATE}))
    field_of(view.start_field, "start_field", types=frozenset({FieldType.DATE}))
    field_of(view.end_field, "end_field", types=frozenset({FieldType.DATE}))
    field_of(view.cover_field, "cover_field", types=frozenset({FieldType.URL}))
    for spec in view.sort:
        field_of(spec.field, "sort field")
    for spec in view.filter:
        field_of(spec.field, "filter field")
    for column in view.columns:
        field_of(column, "column")
    for key, px in view.widths.items():
        if key not in by_key:
            raise ValueError(f"view {name!r}: widths key {key!r} is not a field")
        if not 80 <= px <= 800:
            raise ValueError(f"view {name!r}: width for {key!r} must be 80–800 px")
    if view.column_width is not None and not 200 <= view.column_width <= 600:
        raise ValueError(f"view {name!r}: column_width must be 200–600 px")

    match view.layout:
        case ViewLayout.BOARD:
            if view.group_by is None:
                raise ValueError(f"view {name!r}: a board groups by definition - group_by is required")
        case ViewLayout.CALENDAR:
            if view.date_field is None:
                raise ValueError(f"view {name!r}: a calendar needs date_field")
        case ViewLayout.GANTT:
            if view.start_field is None or view.end_field is None:
                raise ValueError(f"view {name!r}: a gantt needs start_field and end_field")
        case _:
            pass
