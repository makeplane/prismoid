# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The spreadsheet suite - **this file runs inside the frame, not in the worker**.

Like the document suite, it is copied into a sandbox and executed there, reading a job on stdin and
writing checks to stdout, both JSON. Unlike the document suite it is *not* stdlib-only: it opens the
`.xlsx` the model wrote with `openpyxl`, which is pinned in the frame image. It has no network and no
credentials - only the published bytes and the sources they claim to derive from.

**What a spreadsheet lies with, and how this catches it.** A fabricated figure in prose is a number
that appears nowhere in a source. A fabricated figure in a model is subtler: a *total* that does not
add up. It hides three ways, and this suite has a check for each -

  1. **A hardcoded total.** A cell that should be `=SUM(B2:B9)` is typed as `41600000`. Now the total
     is whatever the author wanted, and nothing recomputes it. → `hardcode:totals`.
  2. **A short-changed range.** The total *is* a formula, `=SUM(B2:B8)`, but the column runs to B9 -
     the last row is quietly excluded. The formula looks honest and sums to a wrong number.
     → `cross-foot:totals`.
  3. **A broken formula.** `=SUM(B2:B9)/0`, a `#REF!`, a sheet that does not exist. → `cells:evaluable`.

The suite never evaluates a formula itself: LibreOffice recalculates the whole workbook headless (every
function it implements, across sheets, cached values ignored) and any error value is a FAIL. When
LibreOffice cannot run, `suite:recalc` says so - the verifier's gap, never the document's - and no
formula is judged.

Deterministic checks catch what is *mechanically* wrong - an arithmetic that does not hold. They
cannot catch a model whose every formula foots but whose *assumptions* are nonsense; that is the
cross-family judge's half. This suite is necessary, not sufficient, and says so.
"""

from __future__ import annotations

# `openpyxl` is pinned in the **frame image only** - it is never installed in the worker venv,
# because this file is copied into a sandbox and run there, never imported here. pyright has its
# stubs but not the source, so its `reportMissingModuleSource` is expected and suppressed for this
# one frame-only file, exactly as `document.py` stays stdlib-only to avoid the question entirely.
# pyright: reportMissingModuleSource=false
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.cell.cell import ERROR_CODES

# The one reader rides beside this file, loaded by exact path: the device runs suites with `-I`. Registered
# before it runs because its dataclasses look their own module up.
_SPEC = importlib.util.spec_from_file_location("extract", Path(__file__).with_name("extract.py"))
assert _SPEC is not None and _SPEC.loader is not None
_extract = sys.modules["extract"] = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_extract)
extract, write_extract = _extract.extract, _extract.write_extract

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check

_CELL = re.compile(r"^\$?([A-Z]+)\$?(\d+)$")
_RANGE = re.compile(r"^\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)$")
_SUM = re.compile(r"^=SUM\(([^)]*)\)$", re.IGNORECASE)
_TOTAL_WORD = re.compile(r"\b(?:grand[\s-]*total|subtotal|total)\b", re.IGNORECASE)

# Inside the pipeline's suite deadline, so a hung LibreOffice is our gap and never a crash.
RECALC_TIMEOUT = 60
# A fresh profile that recalculates every OOXML formula on load: a cached value is the author's claim, not a proof.
# Single-quoted attributes: the namespace URIs are identifiers, and backend/tools/audit.sh's URL check greps `"http`.
_RECALC_XCU = (
    "<?xml version='1.0' encoding='UTF-8'?>\n"
    "<oor:items xmlns:oor='http://openoffice.org/2001/registry'>\n"
    " <item oor:path='/org.openoffice.Office.Calc/Formula/Load'>"
    "<prop oor:name='OOXMLRecalcMode' oor:op='fuse'><value>0</value></prop></item>\n"
    "</oor:items>\n"
)


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


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _num_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _range_cells(spec: str) -> list[str] | None:
    """`B2:B9` → [B2, B3, …, B9] (a single row or column). Returns None if not a simple 1-D range."""
    m = _RANGE.match(spec.strip())
    if not m:
        return None
    c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    if c1 == c2:  # a column
        return [f"{c1}{r}" for r in range(min(r1, r2), max(r1, r2) + 1)]
    if r1 == r2:  # a row
        a, b = sorted((_col_to_num(c1), _col_to_num(c2)))
        return [f"{_num_to_col(c)}{r1}" for c in range(a, b + 1)]
    return None  # a 2-D block - not something we cross-foot


class RecalcUnavailable(Exception):
    """LibreOffice could not compute the workbook: the verifier's gap, never the document's."""


def recalc(path: str) -> Any:
    """The workbook as LibreOffice computes it, each formula's value cached for `data_only`; bounded by the timeout."""
    with tempfile.TemporaryDirectory() as scratch:
        profile = Path(scratch, "profile")
        (profile / "user").mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text(_RECALC_XCU)
        out = Path(scratch, "out")
        command = ["soffice", "--headless", f"-env:UserInstallation={profile.as_uri()}"]
        try:
            done = subprocess.run(
                [*command, "--convert-to", "xlsx", "--outdir", str(out), path],
                capture_output=True,
                timeout=RECALC_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as failed:
            raise RecalcUnavailable(f"{type(failed).__name__}: {failed}") from failed
        computed = out / f"{Path(path).stem}.xlsx"
        if not computed.is_file():
            raise RecalcUnavailable(done.stderr.decode(errors="replace")[-300:] or f"exit {done.returncode}")
        return openpyxl.load_workbook(computed, data_only=True)


def _numbers(ws: Any) -> dict[str, float]:
    """Every number on a sheet, by coordinate."""
    return {c.coordinate: n for row in ws.iter_rows() for c in row if (n := _as_number(c.value)) is not None}


def _evaluable_check(broke: list[str], formula_cells: int) -> dict[str, object]:
    """Every formula computes. A #REF!, a #NAME?, a #DIV/0! is a broken model."""
    if broke:
        return _check(
            "cells:evaluable",
            SUBSTANTIVE,
            FAIL,
            f"{len(broke)} formula(s) compute to an error",
            expected="every formula computes to a value",
            actual="; ".join(broke[:6]),
        )
    if formula_cells:
        return _check(
            "cells:evaluable", SUBSTANTIVE, PASS, f"{formula_cells} formula(s) compute without an error"
        )
    return _check("cells:evaluable", SUBSTANTIVE, SKIP, "no formulas - a spreadsheet of only literals")


def run(job: dict[str, object]) -> tuple[list[dict[str, object]], Any]:
    """The checks, and the workbook as LibreOffice computed it (None when it could not) for the sidecars."""
    artifact_path = str(job["artifact_path"])
    expected_sha = str(job["artifact_sha256"])
    checks: list[dict[str, object]] = []

    raw = Path(artifact_path).read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        failed = _check(
            "bytes:sha256",
            STRUCTURAL,
            FAIL,
            "published bytes do not match their digest",
            expected=expected_sha,
            actual=actual_sha,
        )
        return [failed], None
    checks.append(_check("bytes:sha256", STRUCTURAL, PASS, "matches the manifest digest"))

    try:
        computed = recalc(artifact_path)
    except RecalcUnavailable as gap:
        computed = None
        checks.append(
            _check(
                "suite:recalc",
                STRUCTURAL,
                SKIP,
                f"LibreOffice could not recalculate; no formula judged: {gap}",
            )
        )

    wb = openpyxl.load_workbook(artifact_path, data_only=False)
    formula_cells = 0
    broke: list[str] = []
    footed = 0
    misfoot: list[dict[str, object]] = []
    hardcoded: list[str] = []

    for ws in wb.worksheets:
        sheet = None if computed is None else computed[ws.title]
        values = _numbers(ws if sheet is None else sheet)
        for row in ws.iter_rows():
            for cell in row:
                if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                    continue
                formula_cells += 1
                result = None if sheet is None else sheet[cell.coordinate].value
                if result in ERROR_CODES:
                    broke.append(f"{ws.title}!{cell.coordinate}={cell.value} → {result}")
                m = _SUM.match(cell.value)
                if m:
                    problem = _cross_foot(ws, cell.coordinate, m.group(1), values)
                    if problem is None:
                        footed += 1
                    else:
                        misfoot.append({"cell": f"{ws.title}!{cell.coordinate}", **problem})
        hardcoded += [f"{ws.title}!{c}" for c in _hardcoded_totals(ws)]

    if computed is not None:
        checks.append(_evaluable_check(broke, formula_cells))

    # 2. Cross-foot: a SUM total must cover its whole column/row. A range that stops short is the
    #    classic planted bad formula - the total looks honest and is quietly wrong.
    if misfoot:
        detail = "; ".join(
            f"{m['cell']} sums {m['covered']} of {m['expected']} rows (off by {m['delta']})"
            for m in misfoot[:6]
        )
        checks.append(
            _check(
                "cross-foot:totals",
                SUBSTANTIVE,
                FAIL,
                f"{len(misfoot)} total(s) do not foot",
                expected="each SUM covers its full column",
                actual=detail,
            )
        )
    elif footed:
        checks.append(_check("cross-foot:totals", SUBSTANTIVE, PASS, f"{footed} total(s), all foot"))
    else:
        checks.append(_check("cross-foot:totals", SUBSTANTIVE, SKIP, "no SUM totals to cross-foot"))

    # 3. Hardcoded totals: a literal at the foot of a numeric column, where a formula belongs. A
    #    hardcoded total is where a fabricated figure hides - nothing recomputes it.
    if hardcoded:
        checks.append(
            _check(
                "hardcode:totals",
                SUBSTANTIVE,
                FAIL,
                f"{len(hardcoded)} total(s) are hardcoded, not formulas",
                expected="a column total is a =SUM formula",
                actual=", ".join(hardcoded[:8]),
            )
        )
    else:
        checks.append(_check("hardcode:totals", SUBSTANTIVE, PASS, "no hardcoded column totals"))

    return checks, computed


def _column_of(ws: Any, coord: str) -> list[str]:
    """The contiguous run of numeric cells directly above `coord` in its column - the range a total
    at `coord` ought to cover."""
    m = _CELL.match(coord)
    assert m is not None
    col, row = m.group(1), int(m.group(2))
    run: list[str] = []
    r = row - 1
    while r >= 1:
        cell = ws[f"{col}{r}"]
        if _as_number(cell.value) is None and not (
            isinstance(cell.value, str) and cell.value.startswith("=")
        ):
            break
        run.append(f"{col}{r}")
        r -= 1
    return list(reversed(run))


def _cross_foot(ws: Any, coord: str, sum_arg: str, values: dict[str, float]) -> dict[str, object] | None:
    """Whether this column SUM covers its whole column: None when it foots, else the shortfall; row
    totals and 2-D blocks are left to `cells:evaluable`."""
    cells = _range_cells(sum_arg)
    if cells is None:
        return None
    m = _CELL.match(cells[0])
    if m is None or not all((mc := _CELL.match(c)) and mc.group(1) == m.group(1) for c in cells):
        return None  # a row range, not a column - skip
    full = _column_of(ws, coord)
    if not full:
        return None
    covered = set(cells)
    missing = [c for c in full if c not in covered]
    if not missing:
        return None
    delta = sum(values.get(c, 0.0) for c in missing)
    return {"covered": len(cells), "expected": len(full), "delta": round(delta, 4)}


def _hardcoded_totals(ws: Any) -> list[str]:
    """Numeric literals in a row labelled a total, where a `=SUM` belongs; keyed on the label, not the
    arithmetic, so a data cell that happens to foot is never flagged."""
    flagged: list[str] = []
    for row in ws.iter_rows():
        labels = [c.value for c in row if isinstance(c.value, str) and not c.value.startswith("=")]
        if not any(_TOTAL_WORD.search(str(t)) for t in labels):
            continue
        for cell in row:
            # a plain number (not a formula, not a label) in a total-labelled row → a hardcoded total
            if _as_number(cell.value) is not None:
                flagged.append(cell.coordinate)
    return flagged


# /work is the frame's writable dir (a device frame's is a real folder); /verify is root 0555
PREVIEW_PATH = f"{os.environ.get('PRISMOID_WORK_DIR', '/work')}/preview.json"
PREVIEW_ROWS = 50
PREVIEW_COLS = 12
PREVIEW_MAX_BYTES = 64_000


def write_preview(artifact_path: str, computed: Any) -> None:
    """The in-app sidecar: sheet heads with the values just verified (formula text when nothing was computed).
    Best-effort by contract: presentation, not attestation, and it never rides inside the signed report."""
    wb = openpyxl.load_workbook(artifact_path, data_only=False) if computed is None else computed
    sheets: list[dict[str, Any]] = []
    for ws in wb.worksheets:
        rows = [
            [v if isinstance(v := c.value, (int, float, str)) or v is None else str(v) for c in row]
            for row in ws.iter_rows(min_row=1, max_row=PREVIEW_ROWS, max_col=PREVIEW_COLS)
        ]
        while rows and not any(c not in (None, "") for c in rows[-1]):
            rows.pop()  # trailing all-empty rows say nothing
        sheets.append(
            {
                "name": ws.title,
                "dims": f"{ws.max_row}×{ws.max_column}",
                "rows": rows,
                "truncated": bool(ws.max_row > PREVIEW_ROWS or ws.max_column > PREVIEW_COLS),
            }
        )
    payload = json.dumps({"kind": "sheet", "sheets": sheets})
    if len(payload) > PREVIEW_MAX_BYTES:
        # Shrink honestly rather than ship a monster: halve the rows until it fits.
        for sheet in sheets:
            sheet["rows"] = sheet["rows"][: PREVIEW_ROWS // 2]
            sheet["truncated"] = True
        payload = json.dumps({"kind": "sheet", "sheets": sheets})
    if len(payload) <= PREVIEW_MAX_BYTES:
        Path(PREVIEW_PATH).write_text(payload)


def main() -> None:
    job = json.loads(sys.stdin.read())
    path = str(job["artifact_path"])
    computed: Any = None
    try:
        checks, computed = run(job)
    except Exception as exc:  # a suite that crashed did not pass - say which, loudly
        checks = [_check("suite:ran", STRUCTURAL, FAIL, f"{type(exc).__name__}: {exc}")]
    with contextlib.suppress(Exception):  # best-effort presentation, never a verdict
        write_preview(path, computed)

    def values(sheet: Any) -> dict[str, float]:
        return _numbers(computed[sheet.title])

    with contextlib.suppress(Exception):  # the judge's text is a convenience, never a verdict
        write_extract(job, extract(path, values=None if computed is None else values).markdown)
    json.dump({"checks": checks}, sys.stdout)


if __name__ == "__main__":
    main()
