# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The collection normalizer (ADR-019) - imperfect JSON becomes a valid collection, honestly.

`schemas/collection.py` is the contract and stays strict. This module is the door's forgiveness:
a deterministic, conservative repair pass run at HARVEST on `.collection.json` bytes, so a
model's near-miss - a stray select value, a timestamp where a date belongs, a board grouped by a
field that doesn't exist - becomes a renderable collection with the slip RECORDED, instead of a
verify-fail ladder that ends in a dead thread. The user asked for a board; a board with one
noted imperfection beats no board every single time.

Rules of the pass:
* **Deterministic.** Same bytes in, same collection out - no model, no clock, no randomness.
* **Conservative.** Data is preserved where a faithful coercion exists (a stray select value
  becomes a new option; `"1,200"` becomes 1200; a timestamp becomes its date); a cell that
  cannot be read faithfully is DROPPED, never guessed.
* **Honest.** Every mutation appends one human-readable note; the notes ride the artifact's
  provenance (`ManifestEntry.repairs`) and the verifier still judges the stored bytes.
* **Valid by construction.** The pass ends with `Collection.model_validate`; if even the
  repaired candidate cannot pass the contract, the pass gives up (`content=None`) and the
  artifact ships as-published - the reader's honest "can't be read" card is the floor.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeGuard, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prismoid.schemas.collection import GROUPABLE, Collection, FieldType, ViewLayout

MAX_FIELDS = 64
MAX_ROWS = 5000
MAX_VIEWS = 12

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ROW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DATE_FIELD = frozenset({FieldType.DATE})


def _is_obj(value: object) -> TypeGuard[dict[str, object]]:
    """A JSON object; its keys are strings, as `json.loads` always makes them."""
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_list(value) and all(isinstance(v, str) for v in value)


class CollectionRepair(BaseModel):
    """The pass's result: a validated collection (as its `model_dump(mode="json")` dict) plus
    the honest ledger of what changed. `content=None` = unrepairable; store as-published."""

    model_config = ConfigDict(frozen=True)

    content: dict[str, object] | None
    repairs: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.content is not None


def repair_collection(raw: bytes) -> CollectionRepair:
    """bytes → a valid collection + the change ledger, or an honest give-up. Pure."""
    notes: list[str] = []

    data = _parse(raw, notes)
    if not _is_obj(data):
        return CollectionRepair(content=None, repairs=[*notes, "not a JSON object - stored as published"])

    candidate = _normalize(data, notes)
    if candidate is None:
        return CollectionRepair(content=None, repairs=notes)

    try:
        validated = Collection.model_validate(candidate)
    except ValidationError as bad:
        # The belt: the pass should always produce a valid collection; if a rule slipped
        # through, ship as-published rather than ship a guess.
        first = bad.errors()[0].get("msg", "invalid") if bad.errors() else "invalid"
        return CollectionRepair(
            content=None, repairs=[*notes, f"normalizer could not satisfy the contract ({first})"]
        )
    return CollectionRepair(content=validated.model_dump(mode="json"), repairs=notes)


# ── parsing ───────────────────────────────────────────────────────────────────────────────────


def _parse(raw: bytes, notes: list[str]) -> object | None:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # One salvage attempt: trailing garbage after the closing brace (a log line, a stray
    # backtick fence). Never bracket-guessing - a truncated object stays unreadable, honestly.
    tail = text.rfind("}")
    if tail != -1:
        try:
            parsed = json.loads(text[: tail + 1])
        except json.JSONDecodeError:
            return None
        notes.append("trailing non-JSON after the closing brace was dropped")
        return parsed
    return None


# ── the normalizer ────────────────────────────────────────────────────────────────────────────


def _normalize(data: dict[str, object], notes: list[str]) -> dict[str, object] | None:
    if data.get("collection_version") != 1:
        notes.append(f"collection_version {data.get('collection_version')!r} → 1")
    title = str(data.get("title") or "").strip() or "Untitled collection"
    if title != data.get("title"):
        notes.append("title normalized")
    title = title[:200]

    raw_fields = data.get("fields")
    if not _is_list(raw_fields) or not raw_fields:
        notes.append("no usable fields - stored as published")
        return None

    fields, rename = _fields(raw_fields, notes)
    if not fields:
        notes.append("no usable fields - stored as published")
        return None
    by_key: dict[str, dict[str, object]] = {str(f["key"]): f for f in fields}

    raw_rows = data.get("rows")
    if raw_rows is None:
        raw_rows = list[object]()
    if not _is_list(raw_rows):
        notes.append("rows was not a list → []")
        raw_rows = list[object]()
    if len(raw_rows) > MAX_ROWS:
        notes.append(f"rows clamped to {MAX_ROWS} (was {len(raw_rows)})")
        raw_rows = raw_rows[:MAX_ROWS]
    rows = _rows(raw_rows, by_key, rename, notes)

    # A select that ended up with no options (published without any, and no row carried a
    # value to derive one from) cannot satisfy the contract - it reads as text, honestly.
    for field in fields:
        if field["type"] in (FieldType.SELECT.value, FieldType.MULTI_SELECT.value) and not field["options"]:
            notes.append(f"select field {field['key']!r} has no options → text")
            field["type"] = FieldType.TEXT.value

    raw_views = data.get("views")
    if not _is_list(raw_views):
        if raw_views is not None:
            notes.append("views was not a list → default table view")
        raw_views = list[object]()
    if len(raw_views) > MAX_VIEWS:
        notes.append(f"views clamped to {MAX_VIEWS} (was {len(raw_views)})")
        raw_views = raw_views[:MAX_VIEWS]
    views = _views(raw_views, by_key, rename, notes)
    if not views:
        notes.append("no usable views - a default table view was added")
        views = [{"name": "Table", "layout": "table"}]

    return {
        "collection_version": 1,
        "title": title,
        "fields": fields,
        "rows": rows,
        "views": views,
    }


def _slug(raw: object, fallback: str, *, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(raw).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return fallback
    if not s[0].isalpha():
        s = f"f_{s}"
    return s[:limit]


def _fields(raw_fields: list[object], notes: list[str]) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Field defs → valid defs + a rename map (original key spelling → final key) so cells and
    view refs written against the original spelling still land."""
    fields: list[dict[str, object]] = []
    originals: list[tuple[str, str]] = []  # (as-published key, final key)
    seen: set[str] = set()

    for i, raw in enumerate(raw_fields[:MAX_FIELDS]):
        if not _is_obj(raw):
            notes.append(f"field #{i + 1} was not an object - dropped")
            continue
        field_raw: dict[str, object] = raw
        original_key = str(field_raw.get("key") or "")
        key = _field_key(original_key, i, seen, notes)
        originals.append((original_key, key))
        ftype = _field_type(field_raw, key, notes)
        label = str(field_raw.get("label") or "").strip() or key.replace("_", " ")
        fields.append(
            {
                "key": key,
                "label": label[:80],
                "type": ftype.value,
                "options": _field_options(field_raw, key, ftype, notes),
                "unit": str(field_raw.get("unit") or "")[:12],
            }
        )
    if len(raw_fields) > MAX_FIELDS:
        notes.append(f"fields clamped to {MAX_FIELDS} (was {len(raw_fields)})")
    # The rename map remaps cells/view refs written against the AS-PUBLISHED spelling - but
    # only when that spelling survives nowhere (a duplicated key's cells belong to the field
    # that kept the name, never to its renamed twin).
    final_keys = {f["key"] for f in fields}
    rename = {orig: new for orig, new in originals if orig and orig != new and orig not in final_keys}
    return fields, rename


def _field_key(original_key: str, i: int, seen: set[str], notes: list[str]) -> str:
    """A legal key, unique among the fields so far, each change noted."""
    key = original_key if _KEY_RE.match(original_key) else _slug(original_key, f"field_{i + 1}")
    if key != original_key:
        notes.append(f"field key {original_key!r} → {key!r}")
    while key in seen:
        key = f"{key[:37]}_{len(seen)}"
        notes.append(f"duplicate field key {original_key!r} → {key!r}")
    seen.add(key)
    return key


def _field_type(raw: dict[str, object], key: str, notes: list[str]) -> FieldType:
    try:
        return FieldType(str(raw.get("type")))
    except ValueError:
        notes.append(f"field {key!r}: unknown type {raw.get('type')!r} → text")
        return FieldType.TEXT


def _field_options(raw: dict[str, object], key: str, ftype: FieldType, notes: list[str]) -> list[str]:
    """The distinct non-blank options, kept only on a select."""
    options_raw = raw.get("options")
    options = [str(o) for o in options_raw if str(o).strip()] if _is_list(options_raw) else []
    options = list(dict.fromkeys(options))
    if options and ftype not in (FieldType.SELECT, FieldType.MULTI_SELECT):
        notes.append(f"field {key!r}: options on a {ftype.value} field were dropped")
        options = []
    return options


def _rows(
    raw_rows: list[object],
    by_key: dict[str, dict[str, object]],
    rename: dict[str, str],
    notes: list[str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_rows):
        if not _is_obj(raw):
            notes.append(f"row #{i + 1} was not an object - dropped")
            continue
        original_id = str(raw.get("id") or "")
        row_id = original_id if _ROW_ID_RE.match(original_id) else ""
        if not row_id:
            slug = re.sub(r"[^a-z0-9_-]+", "-", original_id.strip().lower()).strip("-_")
            row_id = slug[:64] if slug and slug[0].isalnum() else f"row-{i + 1}"
            if original_id:
                notes.append(f"row id {original_id!r} → {row_id!r}")
        base = row_id
        n = 2
        while row_id in seen:
            row_id = f"{base[:60]}-{n}"
            n += 1
        if row_id != base:
            notes.append(f"duplicate row id {base!r} → {row_id!r}")
        seen.add(row_id)

        cells_raw = raw.get("cells")
        cells: dict[str, object] = {}
        if _is_obj(cells_raw):
            for raw_key, value in cells_raw.items():
                key = rename.get(str(raw_key), str(raw_key))
                field = by_key.get(key)
                if field is None:
                    notes.append(f"row {row_id!r}: cell for unknown field {raw_key!r} dropped")
                    continue
                kept, note = _cell(field, value)
                if note:
                    notes.append(f"row {row_id!r}, field {key!r}: {note}")
                if kept is not _DROP:
                    cells[key] = kept
        rows.append({"id": row_id, "cells": cells})
    return rows


_DROP = object()


# ── cells: one small coercer per field type ───────────────────────────────────────────────────

# A coercer takes a non-null cell and its field def and returns (kept value | _DROP, note | None).
# A select's coercer may grow the field's options with the stray value it keeps.
_Coercer = Callable[[object, dict[str, object]], tuple[object, str | None]]


def _cell(field: dict[str, object], value: object) -> tuple[object, str | None]:
    """One cell → (kept value | _DROP, note | None). Faithful coercions only."""
    if value is None:
        return None, None
    return _COERCERS[FieldType(str(field["type"]))](value, field)


def _options(field: dict[str, object]) -> list[str]:
    return list(cast("list[str]", field.get("options") or []))  # _fields stores options as a list[str]


def _as_text(value: object, _field: dict[str, object]) -> tuple[object, str | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, bool):
        return ("true" if value else "false"), "boolean rendered as text"
    if isinstance(value, int | float):
        return str(value), "number rendered as text"
    if _is_str_list(value):
        return ", ".join(value), "list joined into text"
    return _DROP, f"unreadable {type(value).__name__} dropped"


def _as_number(value: object, _field: dict[str, object]) -> tuple[object, str | None]:
    if isinstance(value, bool):
        return _DROP, "boolean is not a number - dropped"
    if isinstance(value, int | float):
        return value, None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").lstrip("$€£").rstrip("%").strip()
        try:
            return float(cleaned), f"{value!r} coerced to a number"
        except ValueError:
            return _DROP, f"{value!r} is not a number - dropped"
    return _DROP, f"unreadable {type(value).__name__} dropped"


def _as_checkbox(value: object, _field: dict[str, object]) -> tuple[object, str | None]:
    if isinstance(value, bool):
        return value, None
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
        return value.strip().lower() in ("true", "yes"), f"{value!r} coerced to a checkbox"
    return _DROP, f"{value!r} is not true/false - dropped"


def _as_date(value: object, _field: dict[str, object]) -> tuple[object, str | None]:
    if not isinstance(value, str):
        return _DROP, "date must be an ISO string - dropped"
    head = value.strip().split("T")[0].split(" ")[0]
    try:
        parsed = dt.date.fromisoformat(head)
    except ValueError:
        return _DROP, f"{value!r} is not an ISO date - dropped"
    if not 1900 <= parsed.year <= 2100:
        return _DROP, f"{value!r} is outside 1900–2100 - dropped"
    if head != value:
        return head, f"timestamp {value!r} trimmed to its date"
    return head, None


def _as_select(value: object, field: dict[str, object]) -> tuple[object, str | None]:
    if not isinstance(value, str):
        return _DROP, "select value must be a string - dropped"
    options = _options(field)
    if value not in options:
        options.append(value)
        field["options"] = options
        return value, f"stray value {value!r} added to the field's options"
    return value, None


def _as_multi_select(value: object, field: dict[str, object]) -> tuple[object, str | None]:
    values = [value] if isinstance(value, str) else value
    if not _is_str_list(values):
        return _DROP, "multi-select must be a list of strings - dropped"
    options = _options(field)
    stray = [v for v in values if v not in options]
    if stray:
        field["options"] = [*options, *dict.fromkeys(stray)]
        return values, f"stray values {stray!r} added to the field's options"
    return (values, "single value wrapped as a list") if isinstance(value, str) else (values, None)


_COERCERS: dict[FieldType, _Coercer] = {
    FieldType.TEXT: _as_text,
    FieldType.URL: _as_text,
    FieldType.PERSON: _as_text,
    FieldType.NUMBER: _as_number,
    FieldType.CHECKBOX: _as_checkbox,
    FieldType.DATE: _as_date,
    FieldType.SELECT: _as_select,
    FieldType.MULTI_SELECT: _as_multi_select,
}


# ── views: anchors, layout fallbacks, then one small rule per view setting ────────────────────


@dataclass(frozen=True, slots=True)
class _Refs:
    """The final fields a view may name (with the rename map for refs written against the
    as-published spelling) and the ledger every view rule notes its changes on."""

    by_key: dict[str, dict[str, object]]
    rename: dict[str, str]
    notes: list[str]

    def resolve(self, ref: object, *, types: frozenset[FieldType] | None = None) -> str | None:
        if not isinstance(ref, str) or not ref:
            return None
        key = self.rename.get(ref, ref)
        field = self.by_key.get(key)
        if field is None:
            return None
        if types is not None and FieldType(str(field["type"])) not in types:
            return None
        return key

    def first_of(self, types: frozenset[FieldType], *, exclude: tuple[str, ...] = ()) -> str | None:
        for key, field in self.by_key.items():
            if key not in exclude and FieldType(str(field["type"])) in types:
                return key
        return None


# Each anchor a view may name, and the field types it accepts, in the order they are judged.
_ANCHORS: tuple[tuple[str, frozenset[FieldType]], ...] = (
    ("group_by", GROUPABLE),
    ("sub_group_by", GROUPABLE),
    ("date_field", _DATE_FIELD),
    ("start_field", _DATE_FIELD),
    ("end_field", _DATE_FIELD),
    ("cover_field", frozenset({FieldType.URL})),
)

# A layout that needs one anchor: the anchor, its types, and the wording of the fill and the demotion.
_ONE_ANCHOR: dict[ViewLayout, tuple[str, frozenset[FieldType], str, str]] = {
    ViewLayout.BOARD: ("group_by", GROUPABLE, "board grouped by", "board without a groupable field"),
    ViewLayout.CALENDAR: ("date_field", _DATE_FIELD, "calendar dated by", "calendar without a date field"),
}


def _views(
    raw_views: list[object],
    by_key: dict[str, dict[str, object]],
    rename: dict[str, str],
    notes: list[str],
) -> list[dict[str, object]]:
    refs = _Refs(by_key, rename, notes)
    views: list[dict[str, object]] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_views):
        if not _is_obj(raw):
            notes.append(f"view #{i + 1} was not an object - dropped")
            continue
        views.append(_view(raw, _view_name(raw, i, seen), refs))
    return views


def _view_name(raw: dict[str, object], i: int, seen: set[str]) -> str:
    """The view's name, numbered when an earlier view already took it."""
    base = (str(raw.get("name") or "").strip() or f"View {i + 1}")[:80]
    name = base
    n = 2
    while name in seen:
        name = f"{base[:76]} ({n})"
        n += 1
    seen.add(name)
    return name


def _view(raw: dict[str, object], name: str, refs: _Refs) -> dict[str, object]:
    try:
        layout = ViewLayout(str(raw.get("layout")))
    except ValueError:
        refs.notes.append(f"view {name!r}: unknown layout {raw.get('layout')!r} → table")
        layout = ViewLayout.TABLE
    anchors = {role: _anchor(raw, role, types, name, refs) for role, types in _ANCHORS}
    # A layout that lost its required anchor gets the first fitting field, else demotes to
    # table - a table always renders; a refused artifact never does.
    if layout in _ONE_ANCHOR:
        layout = _fill_one(layout, anchors, name, refs)
    elif layout is ViewLayout.GANTT:
        layout = _fill_gantt(anchors, name, refs)
    view: dict[str, object] = {"name": name, "layout": layout.value}
    view.update({role: key for role, key in anchors.items() if key})
    for setting, rule in _SETTINGS:
        kept = rule(raw.get(setting), name, refs)
        if kept:
            view[setting] = kept
    return view


def _anchor(
    raw: dict[str, object], role: str, types: frozenset[FieldType], name: str, refs: _Refs
) -> str | None:
    ref = raw.get(role)
    key = refs.resolve(ref, types=types)
    if ref and key is None:
        refs.notes.append(f"view {name!r}: {role} {ref!r} is not a usable field - dropped")
    return key


def _fill_one(layout: ViewLayout, anchors: dict[str, str | None], name: str, refs: _Refs) -> ViewLayout:
    role, types, filled, missing = _ONE_ANCHOR[layout]
    if anchors[role] is not None:
        return layout
    anchors[role] = refs.first_of(types)
    if anchors[role] is None:
        refs.notes.append(f"view {name!r}: {missing} → table")
        return ViewLayout.TABLE
    refs.notes.append(f"view {name!r}: {filled} {anchors[role]!r}")
    return layout


def _fill_gantt(anchors: dict[str, str | None], name: str, refs: _Refs) -> ViewLayout:
    start, end = anchors["start_field"], anchors["end_field"]
    if start is not None and end is not None:
        return ViewLayout.GANTT
    start = start or refs.first_of(_DATE_FIELD)
    end = end or refs.first_of(_DATE_FIELD, exclude=(start,) if start else ())
    if start is None or end is None:
        refs.notes.append(f"view {name!r}: gantt without two date fields → table")
        anchors["start_field"] = anchors["end_field"] = None
        return ViewLayout.TABLE
    refs.notes.append(f"view {name!r}: gantt span filled as {start!r} → {end!r}")
    anchors["start_field"], anchors["end_field"] = start, end
    return ViewLayout.GANTT


# A view setting's rule takes the raw value, the view's name and the refs, and returns what the
# view keeps (falsy: the setting is left out), noting what it dropped or clamped.
_Setting = Callable[[object, str, _Refs], object]

_LEGAL_OPS = ("eq", "ne", "contains", "gte", "lte", "empty", "not_empty")


def _sort(sort_raw: object, name: str, refs: _Refs) -> object:
    if not _is_list(sort_raw):
        return None
    sort = [
        {
            "field": key,
            "direction": s.get("direction") if s.get("direction") in ("asc", "desc") else "asc",
        }
        for s in sort_raw
        if _is_obj(s) and (key := refs.resolve(s.get("field"))) is not None
    ]
    if len(sort) != len(sort_raw):
        refs.notes.append(f"view {name!r}: sort rules referencing unknown fields were dropped")
    return sort


def _filter(filter_raw: object, name: str, refs: _Refs) -> object:
    if not _is_list(filter_raw):
        return None
    filters = [
        {"field": key, "op": f.get("op"), "value": f.get("value")}
        for f in filter_raw
        if _is_obj(f)
        and (key := refs.resolve(f.get("field"))) is not None
        and f.get("op") in _LEGAL_OPS
        and isinstance(f.get("value"), str | int | float | bool | type(None))
    ]
    if len(filters) != len(filter_raw):
        refs.notes.append(f"view {name!r}: filters with unknown fields or ops were dropped")
    return filters


def _columns(columns_raw: object, name: str, refs: _Refs) -> object:
    if not _is_list(columns_raw):
        return None
    columns = [key for c in columns_raw if (key := refs.resolve(c)) is not None]
    if len(columns) != len(columns_raw):
        refs.notes.append(f"view {name!r}: columns referencing unknown fields were dropped")
    return columns


def _widths(widths_raw: object, name: str, refs: _Refs) -> object:
    if not _is_obj(widths_raw):
        return None
    widths: dict[str, int] = {}
    clamped = False
    for raw_key, px in widths_raw.items():
        key = refs.resolve(raw_key)
        if key is None or isinstance(px, bool) or not isinstance(px, int | float):
            clamped = True
            continue
        bounded = max(80, min(800, int(px)))
        clamped = clamped or bounded != int(px)
        widths[key] = bounded
    if clamped:
        refs.notes.append(f"view {name!r}: widths outside 80–800 or on unknown fields were fixed")
    return widths


def _column_width(cw: object, name: str, refs: _Refs) -> object:
    if not isinstance(cw, int | float) or isinstance(cw, bool):
        return None
    bounded = max(200, min(600, int(cw)))
    if bounded != int(cw):
        refs.notes.append(f"view {name!r}: column_width clamped to {bounded}")
    return bounded


# The settings in the order they are judged, so the notes keep their order.
_SETTINGS: tuple[tuple[str, _Setting], ...] = (
    ("sort", _sort),
    ("filter", _filter),
    ("columns", _columns),
    ("widths", _widths),
    ("column_width", _column_width),
)
