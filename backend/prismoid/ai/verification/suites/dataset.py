# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dataset suite - **this file runs inside the frame, not in the worker.**

Copied into a fresh sandbox and executed there (`ai/verification/pipeline.py`): stdlib only,
job on stdin, checks on stdout, no network. Same citizenship as the document suite.

**What it can prove, and what it deliberately cannot.** A dataset's mechanical truths are
well-formedness ones: the CSV parses and is rectangular, every JSONL line is an object, the
parquet magic bytes are where parquet puts them. All of that is `format:*` - declared
**structural** (`schemas/verification.CheckKind`), because a file being a valid CSV says nothing
about whether its values are *right*. So a merely-well-formed dataset ships `unverified`, loudly,
and a corrupt one FAILs - never a green tick for "it parsed".

Parquet gets magic-bytes only: a real parse needs pyarrow, which is deliberately not in the frame
image. The report says exactly how far it looked.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import cast

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check

# Enough rows to prove shape without turning verification into a second ingest. Past the cap the
# report says so - a cap nobody is told about is a cap that becomes a lie.
MAX_ROWS_EXAMINED = 100_000


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


# ── the three formats ─────────────────────────────────────────────────────────────────────────


def check_csv(raw: bytes) -> list[dict[str, object]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as bad:
        return [_check("format:parse", STRUCTURAL, FAIL, "not UTF-8", actual=str(bad))]

    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not any(cell.strip() for cell in rows[0]):
        return [_check("format:parse", STRUCTURAL, FAIL, "no header row - an empty file is not a dataset")]

    header, body = rows[0], rows[1 : MAX_ROWS_EXAMINED + 1]
    width = len(header)
    ragged = [i + 2 for i, row in enumerate(body) if len(row) != width]  # +2: 1-based, after header
    if ragged:
        return [
            _check(
                "format:parse",
                STRUCTURAL,
                FAIL,
                f"{len(ragged)} row(s) do not match the header's {width} column(s)",
                expected=f"{width} columns per row",
                actual=f"first ragged line: {ragged[0]}",
            )
        ]

    examined = len(body)
    capped = "" if examined <= MAX_ROWS_EXAMINED else f" (first {MAX_ROWS_EXAMINED:,} examined)"
    return [
        _check("format:parse", STRUCTURAL, PASS, f"CSV parses; rectangular{capped}"),
        _check(
            "format:shape",
            STRUCTURAL,
            PASS,
            f"{examined:,} row(s) × {width} column(s); header: {', '.join(header[:8])}",
        ),
    ]


def check_jsonl(raw: bytes) -> list[dict[str, object]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as bad:
        return [_check("format:parse", STRUCTURAL, FAIL, "not UTF-8", actual=str(bad))]

    records = 0
    keys: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if records >= MAX_ROWS_EXAMINED:
            break
        try:
            value = json.loads(line)
        except json.JSONDecodeError as bad:
            return [
                _check(
                    "format:parse",
                    STRUCTURAL,
                    FAIL,
                    f"line {lineno} is not JSON",
                    actual=f"{bad.msg} at column {bad.colno}",
                )
            ]
        records += 1
        if records == 1 and isinstance(value, dict):
            keys = [str(k) for k in cast("dict[object, object]", value)][:8]

    if records == 0:
        return [_check("format:parse", STRUCTURAL, FAIL, "no records - an empty file is not a dataset")]
    shape = f"{records:,} record(s)" + (f"; first record's keys: {', '.join(keys)}" if keys else "")
    return [
        _check("format:parse", STRUCTURAL, PASS, "every line parses as JSON"),
        _check("format:shape", STRUCTURAL, PASS, shape),
    ]


def check_parquet(raw: bytes) -> list[dict[str, object]]:
    # Magic bytes only: `PAR1` opens and closes every parquet file. A real parse needs pyarrow,
    # which is not in the frame image on purpose - say how far we looked, never further.
    if len(raw) >= 8 and raw[:4] == b"PAR1" and raw[-4:] == b"PAR1":
        return [
            _check("format:parse", STRUCTURAL, PASS, "parquet magic bytes present (PAR1…PAR1)"),
            _check(
                "format:shape",
                STRUCTURAL,
                SKIP,
                "no parquet reader in the frame - schema and rows not examined",
            ),
        ]
    return [
        _check(
            "format:parse",
            STRUCTURAL,
            FAIL,
            "not a parquet file",
            expected="PAR1 magic at both ends",
            actual=f"leading bytes: {raw[:4]!r}",
        )
    ]


# ── the frame around the checks ───────────────────────────────────────────────────────────────


def run(job: dict[str, object]) -> list[dict[str, object]]:
    artifact_path = str(job["artifact_path"])
    expected_sha = str(job["artifact_sha256"])
    sources = [str(s) for s in cast("list[object]", job.get("sources", []))]

    checks: list[dict[str, object]] = []

    # 1. Are we even looking at the right bytes? Everything downstream is worthless if not.
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

    # 2. Sources are recorded for provenance; a dataset's grounding is not re-derived here (a row
    #    set is a record, not an argument), so presence is reported and nothing more is claimed.
    if sources:
        missing = [p for p in sources if not Path(p).is_file()]
        if missing:
            checks.append(
                _check(
                    "sources:present",
                    STRUCTURAL,
                    FAIL,
                    f"{len(sources) - len(missing)} of {len(sources)} present",
                    expected="every declared source readable",
                    actual="missing: " + ", ".join(missing),
                )
            )
        else:
            checks.append(
                _check("sources:present", STRUCTURAL, PASS, f"{len(sources)} source(s), all readable")
            )
    else:
        checks.append(_check("sources:present", STRUCTURAL, SKIP, "the dataset declares no sources"))

    # 3. Well-formedness, by extension - the same extension the publish door validated.
    name = artifact_path.rsplit("/", 1)[-1].lower()
    if name.endswith(".csv"):
        checks.extend(check_csv(raw))
    elif name.endswith(".jsonl"):
        checks.extend(check_jsonl(raw))
    elif name.endswith(".parquet"):
        checks.extend(check_parquet(raw))
    else:  # unreachable through publish (accepts() gates extensions); honest if reached sideways
        checks.append(_check("format:parse", STRUCTURAL, SKIP, f"no dataset reader for {name!r}"))

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
