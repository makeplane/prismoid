# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dataset suite's logic - stdlib-only, so it unit-tests in microseconds (the document suite's
exception, same justification).

The doctrinal assertion here is the verdict shape: **well-formed is not verified.** Every
`format:*` check is structural, so a dataset whose checks all pass still comes out UNVERIFIED from
`verdict_of` - it parsed, nothing vouched for its contents - while a corrupt one FAILs. Before this
suite existed, publishing a `.csv` crashed the step outright (`SuiteNotBuilt`, uncaught).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from prismoid.ai.verification.pipeline import verdict_of
from prismoid.ai.verification.suites.dataset import run
from prismoid.schemas.verification import Check, Verdict


def job_for(tmp_path: Path, name: str, raw: bytes, *, sha: str | None = None) -> dict[str, object]:
    file = tmp_path / name
    file.write_bytes(raw)
    return {
        "artifact_path": str(file),
        "artifact_sha256": sha or hashlib.sha256(raw).hexdigest(),
        "sources": [],
        "allow_domains": [],
    }


def by_name(checks: list[dict[str, object]]) -> dict[str, str]:
    return {str(c["name"]): str(c["status"]) for c in checks}


def verdict(checks: list[dict[str, object]]) -> Verdict:
    return verdict_of([Check.model_validate(c) for c in checks])


# ── csv ───────────────────────────────────────────────────────────────────────────────────────


def test_a_clean_csv_parses_and_ships_unverified(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.csv", b"name,stage\nAcme,Beta\nOrbit,GA\n"))
    statuses = by_name(checks)
    assert statuses["format:parse"] == "pass"
    assert statuses["format:shape"] == "pass"
    assert verdict(checks) is Verdict.UNVERIFIED, "well-formed is not verified - format:* is structural"


def test_a_ragged_csv_fails(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.csv", b"a,b\n1,2\n3\n"))
    assert by_name(checks)["format:parse"] == "fail"
    assert verdict(checks) is Verdict.FAIL


def test_an_empty_csv_fails(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.csv", b""))
    assert by_name(checks)["format:parse"] == "fail"


def test_a_non_utf8_csv_fails(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.csv", b"a,b\n\xff\xfe,2\n"))
    assert by_name(checks)["format:parse"] == "fail"


# ── jsonl ─────────────────────────────────────────────────────────────────────────────────────


def test_a_clean_jsonl_parses(tmp_path: Path) -> None:
    raw = b'{"id": "r1", "n": 1}\n{"id": "r2", "n": 2}\n'
    checks = run(job_for(tmp_path, "rows.jsonl", raw))
    statuses = by_name(checks)
    assert statuses["format:parse"] == "pass"
    assert statuses["format:shape"] == "pass"
    assert verdict(checks) is Verdict.UNVERIFIED


def test_a_bad_jsonl_line_fails_and_names_the_line(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.jsonl", b'{"ok": true}\nnot json\n'))
    parse = next(c for c in checks if c["name"] == "format:parse")
    assert parse["status"] == "fail"
    assert "line 2" in str(parse["detail"])


# ── parquet ───────────────────────────────────────────────────────────────────────────────────


def test_parquet_magic_passes_and_shape_says_how_far_it_looked(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.parquet", b"PAR1" + b"\x00" * 16 + b"PAR1"))
    statuses = by_name(checks)
    assert statuses["format:parse"] == "pass"
    assert statuses["format:shape"] == "skip"  # no pyarrow in the frame - said, not hidden


def test_not_parquet_fails(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.parquet", b"definitely,csv\n1,2\n"))
    assert by_name(checks)["format:parse"] == "fail"


# ── the bytes and the frame around them ───────────────────────────────────────────────────────


def test_wrong_bytes_fail_regardless_of_format(tmp_path: Path) -> None:
    checks = run(job_for(tmp_path, "rows.csv", b"a,b\n1,2\n", sha="0" * 64))
    assert by_name(checks)["bytes:sha256"] == "fail"
    assert verdict(checks) is Verdict.FAIL


def test_the_suite_emits_json_shaped_checks(tmp_path: Path) -> None:
    for c in run(job_for(tmp_path, "rows.csv", b"a\n1\n")):
        json.dumps(c)  # every check must survive the stdout boundary
