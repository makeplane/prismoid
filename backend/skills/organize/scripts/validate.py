#!/usr/bin/env python3
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Check a collection file BEFORE publishing it - run me: python3 validate.py /work/x.collection.json

Stdlib only (this runs inside the frame). The rules mirror the product's collection schema -
the same ones the verifier re-checks after publish - so a clean pass here means the schema half
of verification will pass. Grounding (numbers verbatim in your sources) is still on you.
Exit 0 = clean; exit 1 prints every problem found.
"""

from __future__ import annotations

import datetime
import json
import re
import sys

FIELD_TYPES = {"text", "number", "date", "select", "multi_select", "checkbox", "url", "person"}
LAYOUTS = {"table", "board", "list", "gallery", "gantt", "calendar"}
GROUPABLE = {"select", "person", "checkbox"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ROW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def check(doc: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["the document is not a JSON object"]
    if doc.get("collection_version") != 1:
        problems.append(f"collection_version must be 1, got {doc.get('collection_version')!r}")
    if not isinstance(doc.get("title"), str) or not str(doc.get("title")).strip():
        problems.append("title must be a non-empty string")

    fields = doc.get("fields") if isinstance(doc.get("fields"), list) else []
    by_key: dict[str, dict] = {}
    if not fields:
        problems.append("fields must be a non-empty list")
    for f in fields:
        if not isinstance(f, dict):
            problems.append("a field is not an object")
            continue
        key = f.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            problems.append(f"field key {key!r} is not lowercase snake_case")
            continue
        if key in by_key:
            problems.append(f"field key {key!r} is duplicated")
        if f.get("type") not in FIELD_TYPES:
            problems.append(f"field {key!r}: unknown type {f.get('type')!r}")
        options = f.get("options") or []
        if options and f.get("type") not in ("select", "multi_select"):
            problems.append(f"field {key!r} has options but is not a select")
        if not options and f.get("type") in ("select", "multi_select"):
            problems.append(f"select field {key!r} needs options")
        by_key[key] = f

    seen_rows: set[str] = set()
    for row in doc.get("rows") if isinstance(doc.get("rows"), list) else []:
        if not isinstance(row, dict):
            problems.append("a row is not an object")
            continue
        rid = row.get("id")
        if not isinstance(rid, str) or not ROW_ID_RE.match(rid):
            problems.append(f"row id {rid!r} is not lowercase [a-z0-9_-]")
        elif rid in seen_rows:
            problems.append(f"row id {rid!r} is duplicated")
        else:
            seen_rows.add(rid)
        for key, value in (row.get("cells") or {}).items():
            field = by_key.get(str(key))
            if field is None:
                problems.append(f"row {rid!r} has a cell for unknown field {key!r}")
                continue
            problem = cell_problem(field, value)
            if problem:
                problems.append(f"row {rid!r}, field {key!r}: {problem}")

    seen_views: set[str] = set()
    views = doc.get("views") if isinstance(doc.get("views"), list) else []
    if not views:
        problems.append("views must be a non-empty list")
    for v in views:
        if not isinstance(v, dict):
            problems.append("a view is not an object")
            continue
        name = str(v.get("name") or "?")
        if name in seen_views:
            problems.append(f"view name {name!r} is duplicated")
        seen_views.add(name)
        layout = v.get("layout")
        if layout not in LAYOUTS:
            problems.append(f"view {name!r}: unknown layout {layout!r}")
            continue
        refs = {
            "group_by": GROUPABLE,
            "sub_group_by": GROUPABLE,
            "date_field": {"date"},
            "start_field": {"date"},
            "end_field": {"date"},
            "cover_field": {"url"},
        }
        for role, types in refs.items():
            ref = v.get(role)
            if ref is None:
                continue
            field = by_key.get(str(ref))
            if field is None:
                problems.append(f"view {name!r}: {role} {ref!r} is not a field")
            elif field.get("type") not in types:
                problems.append(f"view {name!r}: {role} {ref!r} must be one of: {', '.join(sorted(types))}")
        for column in v.get("columns") or []:
            if str(column) not in by_key:
                problems.append(f"view {name!r}: column {column!r} is not a field")
        widths = v.get("widths", {})
        if not isinstance(widths, dict):
            problems.append(f"view {name!r}: widths must be an object")
        else:
            for wkey, px in widths.items():
                if str(wkey) not in by_key:
                    problems.append(f"view {name!r}: widths key {wkey!r} is not a field")
                if not integral_px(px, 80, 800):
                    problems.append(f"view {name!r}: width for {wkey!r} must be 80-800 px")
        if v.get("column_width") is not None and not integral_px(v.get("column_width"), 200, 600):
            problems.append(f"view {name!r}: column_width must be 200-600 px")
        if layout == "board" and v.get("group_by") is None:
            problems.append(f"view {name!r}: a board groups by definition - group_by is required")
        if layout == "calendar" and v.get("date_field") is None:
            problems.append(f"view {name!r}: a calendar needs date_field")
        if layout == "gantt" and (v.get("start_field") is None or v.get("end_field") is None):
            problems.append(f"view {name!r}: a gantt needs start_field and end_field")
    return problems


def integral_px(value: object, lo: int, hi: int) -> bool:
    """Width fields accept whole numbers only: integral floats count (320.0), bools never do."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return float(value).is_integer() and lo <= value <= hi


def cell_problem(field: dict, value: object) -> str | None:
    if value is None:
        return None
    ftype = field.get("type")
    options = field.get("options") or []
    if ftype in ("text", "url", "person"):
        return None if isinstance(value, str) else "expected a string"
    if ftype == "number":
        ok = not isinstance(value, bool) and isinstance(value, (int, float))
        return None if ok else "expected a number"
    if ftype == "checkbox":
        return None if isinstance(value, bool) else "expected true/false"
    if ftype == "date":
        if not isinstance(value, str):
            return "expected an ISO date string"
        try:
            parsed = datetime.date.fromisoformat(value)
        except ValueError:
            return f"{value!r} is not an ISO-8601 date"
        return None if 1900 <= parsed.year <= 2100 else f"{value!r} is outside 1900-2100"
    if ftype == "select":
        return None if isinstance(value, str) and value in options else "not one of the field's options"
    if ftype == "multi_select":
        ok = isinstance(value, list) and all(x in options for x in value)
        return None if ok else "every value must be one of the field's options"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 validate.py <file.collection.json>")
        return 2
    try:
        from pathlib import Path

        doc = json.loads(Path(sys.argv[1]).read_bytes().decode("utf-8"))
    except Exception as bad:
        print(f"not JSON: {bad}")
        return 1
    problems = check(doc)
    if problems:
        for p in problems:
            print(f"✗ {p}")
        return 1
    rows = len(doc.get("rows", []))
    views = ", ".join(v.get("layout", "?") for v in doc.get("views", []))
    print(f"✓ valid collection - {rows} row(s); views: {views}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
