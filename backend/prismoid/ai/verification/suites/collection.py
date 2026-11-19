# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The collection suite - **this file runs inside the frame, not in the worker.**

Copied into a fresh sandbox and executed there (`ai/verification/pipeline.py`): stdlib only,
job on stdin, checks on stdout, no network.

**It is a mirror, not a second opinion.** `schemas/collection.py` is the one definition of a
valid collection; this file re-derives those rules with the standard library because pydantic
does not exist in a frame. The lockstep test (`tests/unit/ai/verification/test_collection_suite.py`) holds the
two together mutation by mutation - change the model, and this mirror must change in the same
commit or the suite fails.

What is substantive here, unlike the dataset suite: `schema:shape`, `refs:views`, `cells:typed`
and `rows:unique` all examine what the collection *claims* (this select value is legal, this
view's fields exist), so a collection that passes them has been genuinely examined - it can earn
a PASS. `numbers:grounded` then checks every number cell **verbatim** against the sources: a
collection is a record, not an analysis, so no derivation search - and the report says exactly
that, so nobody mistakes "verbatim" for "derived".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import TypeGuard, cast

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check

FIELD_TYPES = {"text", "number", "date", "select", "multi_select", "checkbox", "url", "person"}
LAYOUTS = {"table", "board", "list", "gallery", "gantt", "calendar"}
GROUPABLE = {"select", "person", "checkbox"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ROW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Decimal literals in source prose, commas stripped: enough for verbatim matching of cell values.
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

MAX_LISTED = 5  # errors reported per check before "…and N more" - a wall of 400 is not a report


def _check(
    name: str,
    kind: str,
    status: str,
    detail: str = "",
    *,
    expected: str | None = None,
    actual: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "status": status,
        "detail": detail,
        "expected": expected,
        "actual": actual,
    }


def _fail_list(name: str, kind: str, problems: list[str]) -> dict[str, object]:
    listed = "; ".join(problems[:MAX_LISTED])
    more = f" …and {len(problems) - MAX_LISTED} more" if len(problems) > MAX_LISTED else ""
    return _check(name, kind, FAIL, listed + more)


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    """A JSON object - its keys are strings by construction (the job is `json.loads` output)."""
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# ── the mirror, rule by rule ──────────────────────────────────────────────────────────────────


def check_shape(doc: object) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Envelope + field declarations. Returns the check and `fields by key` for the later rules."""
    if not _is_object(doc):
        return _check("schema:shape", SUBSTANTIVE, FAIL, "the document is not a JSON object"), {}
    problems = _envelope_problems(doc)
    by_key = _declared_fields(doc.get("fields"), problems)
    row_count, view_count = _rows_and_views(doc, problems)

    if problems:
        return _fail_list("schema:shape", SUBSTANTIVE, problems), by_key
    return _check(
        "schema:shape", SUBSTANTIVE, PASS, f"{len(by_key)} field(s), {row_count} row(s), {view_count} view(s)"
    ), by_key


def _envelope_problems(doc: dict[str, object]) -> list[str]:
    problems: list[str] = []
    if doc.get("collection_version") != 1:
        problems.append(f"collection_version must be 1, got {doc.get('collection_version')!r}")
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        problems.append("title must be a non-empty string ≤200 chars")
    return problems


def _declared_fields(fields: object, problems: list[str]) -> dict[str, dict[str, object]]:
    """Every well-keyed field by its key, appending what is wrong with the declarations to
    `problems` in document order."""
    by_key: dict[str, dict[str, object]] = {}
    items = _list_or_empty(fields)
    if not items or len(items) > 64:
        problems.append("fields must be a non-empty list (≤64)")
        items = []
    for f in items:
        if not _is_object(f):
            problems.append("a field is not an object")
            continue
        key = f.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            problems.append(f"field key {key!r} is not lowercase snake_case")
            continue
        if key in by_key:
            problems.append(f"field key {key!r} is duplicated")
        problems.extend(_field_problems(key, f))
        by_key[key] = f
    return by_key


def _field_problems(key: str, f: dict[str, object]) -> list[str]:
    problems: list[str] = []
    label, ftype = f.get("label"), f.get("type")
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        problems.append(f"field {key!r}: label must be a non-empty string ≤80 chars")
    if ftype not in FIELD_TYPES:
        problems.append(f"field {key!r}: unknown type {ftype!r}")
    options = f.get("options", [])
    if options and ftype not in ("select", "multi_select"):
        problems.append(f"field {key!r} has options but is not a select")
    if not options and ftype in ("select", "multi_select"):
        problems.append(f"select field {key!r} needs options")
    return problems


def _rows_and_views(doc: dict[str, object], problems: list[str]) -> tuple[int, int]:
    rows, views = doc.get("rows", []), doc.get("views")
    row_count = len(rows) if _is_list(rows) else 0
    view_count = len(views) if _is_list(views) else 0
    if not _is_list(rows) or row_count > 5000:
        problems.append("rows must be a list (≤5000)")
    if not _is_list(views) or not views or view_count > 12:
        problems.append("views must be a non-empty list (≤12)")
    return row_count, view_count


def check_views(doc: dict[str, object], by_key: dict[str, dict[str, object]]) -> dict[str, object]:
    problems: list[str] = []
    seen: set[str] = set()
    for v in _list_or_empty(doc.get("views")):
        problems.extend(_view_problems(v, seen, by_key))

    if problems:
        return _fail_list("refs:views", SUBSTANTIVE, problems)
    return _check("refs:views", SUBSTANTIVE, PASS, "every view references real, type-appropriate fields")


# The single-field references a view may carry, each with the field types it accepts.
_VIEW_REFS: tuple[tuple[str, set[str]], ...] = (
    ("group_by", GROUPABLE),
    ("sub_group_by", GROUPABLE),
    ("date_field", {"date"}),
    ("start_field", {"date"}),
    ("end_field", {"date"}),
    ("cover_field", {"url"}),
)


def _view_problems(v: object, seen: set[str], by_key: dict[str, dict[str, object]]) -> list[str]:
    """What is wrong with one view, in the order the report lists it; records its name in `seen`."""
    if not _is_object(v):
        return ["a view is not an object"]
    problems: list[str] = []
    name, layout = v.get("name"), v.get("layout")
    if not isinstance(name, str) or not name.strip():
        problems.append("a view has no name")
        name = "?"
    if name in seen:
        problems.append(f"view name {name!r} is duplicated")
    seen.add(str(name))
    if layout not in LAYOUTS:
        problems.append(f"view {name!r}: unknown layout {layout!r}")
        return problems
    problems.extend(_view_ref_problems(v, str(name), by_key))
    problems.extend(_view_field_list_problems(v, str(name), by_key))
    problems.extend(_view_width_problems(v, str(name), by_key))
    problems.extend(_view_layout_problems(v, str(name), layout))
    return problems


def _view_ref_problems(v: dict[str, object], name: str, by_key: dict[str, dict[str, object]]) -> list[str]:
    problems: list[str] = []
    for role, types in _VIEW_REFS:
        value = v.get(role)
        if value is None:
            continue
        field = by_key.get(str(value))
        if field is None:
            problems.append(f"view {name!r}: {role} {value!r} is not a field")
        elif field.get("type") not in types:
            problems.append(f"view {name!r}: {role} {value!r} must be one of: {', '.join(sorted(types))}")
    return problems


def _view_field_list_problems(
    v: dict[str, object], name: str, by_key: dict[str, dict[str, object]]
) -> list[str]:
    """Sort and filter specs, then columns: every field they name must exist."""
    problems: list[str] = []
    for role in ("sort", "filter"):
        problems += [
            f"view {name!r}: {role} field {spec.get('field')!r} is not a field"
            for spec in _list_or_empty(v.get(role))
            if _is_object(spec) and str(spec.get("field")) not in by_key
        ]
    problems += [
        f"view {name!r}: column {column!r} is not a field"
        for column in _list_or_empty(v.get("columns"))
        if str(column) not in by_key
    ]
    return problems


def _list_or_empty(value: object) -> list[object]:
    return value if _is_list(value) else []


def _view_width_problems(v: dict[str, object], name: str, by_key: dict[str, dict[str, object]]) -> list[str]:
    problems: list[str] = []
    widths = v.get("widths", {})
    if not _is_object(widths):
        problems.append(f"view {name!r}: widths must be an object")
    else:
        for wkey, px in widths.items():
            if str(wkey) not in by_key:
                problems.append(f"view {name!r}: widths key {wkey!r} is not a field")
            if not _integral_px(px, 80, 800):
                problems.append(f"view {name!r}: width for {wkey!r} must be 80–800 px")
    if v.get("column_width") is not None and not _integral_px(v.get("column_width"), 200, 600):
        problems.append(f"view {name!r}: column_width must be 200–600 px")
    return problems


def _view_layout_problems(v: dict[str, object], name: str, layout: object) -> list[str]:
    """The fields a layout cannot draw without."""
    problems: list[str] = []
    if layout == "board" and v.get("group_by") is None:
        problems.append(f"view {name!r}: a board groups by definition - group_by is required")
    if layout == "calendar" and v.get("date_field") is None:
        problems.append(f"view {name!r}: a calendar needs date_field")
    if layout == "gantt" and (v.get("start_field") is None or v.get("end_field") is None):
        problems.append(f"view {name!r}: a gantt needs start_field and end_field")
    return problems


def _integral_px(value: object, lo: int, hi: int) -> bool:
    """Pydantic-lax parity for the width fields: integral floats coerce (320.0 → 320), bools
    never do (True would otherwise be 1 and range-fail with a misleading message)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return float(value).is_integer() and lo <= value <= hi


def check_cells(doc: dict[str, object], by_key: dict[str, dict[str, object]]) -> dict[str, object]:
    problems: list[str] = []
    for row in _list_or_empty(doc.get("rows")):
        if not _is_object(row):
            problems.append("a row is not an object")
            continue
        rid = row.get("id", "?")
        cells = row.get("cells", {})
        if not _is_object(cells):
            problems.append(f"row {rid!r}: cells is not an object")
            continue
        for key, value in cells.items():
            field = by_key.get(str(key))
            if field is None:
                problems.append(f"row {rid!r} has a cell for unknown field {key!r}")
                continue
            if value is None:
                continue
            problem = _cell_problem(field, value)
            if problem:
                problems.append(f"row {rid!r}, field {key!r}: {problem}")
    if problems:
        return _fail_list("cells:typed", SUBSTANTIVE, problems)
    return _check("cells:typed", SUBSTANTIVE, PASS, "every cell matches its field's type")


def _cell_problem(field: dict[str, object], value: object) -> str | None:
    """The type table, mirrored from `schemas/collection._check_cell` - one rule, two homes,
    pinned together by the lockstep test."""
    ftype = field.get("type")
    options = _list_or_empty(field.get("options"))
    if ftype in ("text", "url", "person"):
        return None if isinstance(value, str) else f"expected a string, got {type(value).__name__}"
    if ftype == "number":
        ok = not isinstance(value, bool) and isinstance(value, int | float)
        return None if ok else f"expected a number, got {type(value).__name__}"
    if ftype == "checkbox":
        return None if isinstance(value, bool) else f"expected true/false, got {type(value).__name__}"
    if ftype == "date":
        if not isinstance(value, str):
            return "expected an ISO date string"
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError:
            return f"{value!r} is not an ISO-8601 date"
        return None if 1900 <= parsed.year <= 2100 else f"{value!r} is outside 1900–2100"
    if ftype == "select":
        ok = isinstance(value, str) and value in options
        return None if ok else f"{value!r} is not one of the field's options"
    if ftype == "multi_select":
        ok = _is_list(value) and all(v in options for v in value)
        return None if ok else "every value must be one of the field's options"
    return None  # unknown type is schema:shape's failure, not this cell's


def check_row_ids(doc: dict[str, object]) -> dict[str, object]:
    problems: list[str] = []
    seen: set[str] = set()
    for row in _list_or_empty(doc.get("rows")):
        rid = row.get("id") if _is_object(row) else None
        if not isinstance(rid, str) or not ROW_ID_RE.match(rid):
            problems.append(f"row id {rid!r} is not lowercase [a-z0-9_-]")
            continue
        if rid in seen:
            problems.append(f"row id {rid!r} is duplicated")
        seen.add(rid)
    if problems:
        return _fail_list("rows:unique", SUBSTANTIVE, problems)
    return _check("rows:unique", SUBSTANTIVE, PASS, f"{len(seen)} unique row id(s)")


def check_numbers(doc: dict[str, object], source_text: str) -> dict[str, object]:
    """Every number CELL, verbatim in the sources. No derivation search - a collection is a
    record, not an argument - and the detail says so, so 'grounded' is never over-read."""
    fields = _list_or_empty(doc.get("fields"))
    number_keys = {str(f.get("key")) for f in fields if _is_object(f) and f.get("type") == "number"}
    if not number_keys:
        return _check("numbers:grounded", SUBSTANTIVE, SKIP, "no number fields - nothing to ground")
    if not source_text:
        return _check(
            "numbers:grounded", SUBSTANTIVE, SKIP, "no readable sources - nothing to ground against"
        )

    source_numbers = {m.group().replace(",", "") for m in NUMBER_RE.finditer(source_text)}
    missing: list[str] = []
    for row in _list_or_empty(doc.get("rows")):
        record: dict[str, object] = row if _is_object(row) else {}
        # A non-object `cells` is cells:typed's failure; it raises here (-> suite:ran), as it always has.
        cells = cast("dict[str, object]", record.get("cells", {}))
        for key, value in cells.items():
            if str(key) not in number_keys or value is None or isinstance(value, bool):
                continue
            if not isinstance(value, int | float):
                continue  # cells:typed already failed it; do not double-report
            text = f"{value:g}"
            if text not in source_numbers and str(value) not in source_numbers:
                missing.append(f"{record.get('id', '?')}.{key}={text}")
    if missing:
        return _fail_list(
            "numbers:grounded", SUBSTANTIVE, [f"{m} appears in no source (verbatim match)" for m in missing]
        )
    return _check(
        "numbers:grounded",
        SUBSTANTIVE,
        PASS,
        "every number cell appears verbatim in a source (no derivation)",
    )


# ── the frame around the checks ───────────────────────────────────────────────────────────────


def run(job: dict[str, object]) -> list[dict[str, object]]:
    artifact_path = str(job["artifact_path"])
    expected_sha = str(job["artifact_sha256"])
    sources = [str(s) for s in cast("list[object]", job.get("sources", []))]

    checks: list[dict[str, object]] = []

    raw = Path(artifact_path).read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha == expected_sha:
        checks.append(_check("bytes:sha256", STRUCTURAL, PASS, f"{actual_sha[:16]}… matches the manifest"))
    else:
        checks.append(
            _check(
                "bytes:sha256",
                STRUCTURAL,
                FAIL,
                "these are not the bytes that were published",
                expected=expected_sha,
                actual=actual_sha,
            )
        )

    present = [p for p in sources if Path(p).is_file()]
    if not sources:
        checks.append(_check("sources:present", STRUCTURAL, SKIP, "the collection declares no sources"))
    elif len(present) < len(sources):
        checks.append(
            _check(
                "sources:present",
                STRUCTURAL,
                FAIL,
                f"{len(present)} of {len(sources)} present",
                expected="every declared source readable",
                actual="missing: " + ", ".join(sorted(set(sources) - set(present))),
            )
        )
    else:
        checks.append(_check("sources:present", STRUCTURAL, PASS, f"{len(present)} source(s), all readable"))

    try:
        doc: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as bad:
        checks.append(
            _check("schema:shape", SUBSTANTIVE, FAIL, "the file is not JSON", actual=str(bad)[:200])
        )
        return checks

    shape, by_key = check_shape(doc)
    checks.append(shape)
    if shape["status"] == FAIL or not _is_object(doc):
        return checks  # reference/cell checks over a broken envelope would double-report noise

    checks.append(check_views(doc, by_key))
    checks.append(check_cells(doc, by_key))
    checks.append(check_row_ids(doc))
    source_text = "\n".join(Path(p).read_text(errors="replace") for p in present)
    checks.append(check_numbers(doc, source_text))
    return checks


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        checks = run(job)
    except Exception as exc:
        checks = [_check("suite:ran", STRUCTURAL, FAIL, f"{type(exc).__name__}: {exc}")]
    json.dump({"checks": checks}, sys.stdout)


if __name__ == "__main__":
    main()
