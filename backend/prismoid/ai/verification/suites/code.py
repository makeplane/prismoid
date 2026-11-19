# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The code suite: rebuild the project in a fresh frame and run its own tests; the run is the receipt.

Runs inside the frame (like every suite), reading a job on stdin and writing checks to stdout. A `code`
artifact is a directory, stored as a tar (`docker_driver.publish`); this suite untars it into a scratch
dir and runs its `test.sh`, the contract the `code` skill teaches (exit 0 iff the code is correct), so
the verifier never guesses a build command.

The receipt is typed (`schemas/verification.CodeReceipt`, as JSON in the `tests:run` check's `actual`): the
command, its exit code, and the counts and failing tests read from the JUnit XML the runner writes into
`test-reports/` (pytest `--junitxml`, node `--test-reporter=junit`: the format every CI reads), never
from the run's text. The exit code is the authority; a report adds counts and names, and a failing test
it names fails the run even when `test.sh` swallowed the exit. Reports the artifact shipped are deleted
before the run, so only this run's count. Each failing test is its own `test:<name>` check.

No `test.sh`, or a run whose report holds no passing test, is untested (a SKIP): never a pass, and no
judge lifts it (`pipeline.final_verdict`). The run is cut at `TEST_TIMEOUT_S` with its whole process
group and its output kept to a tail, so a hung or chatty test costs a bounded wait.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import IO

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check
TESTS = "tests:run"  # `pipeline.TESTS_RUN` names it: a judge never lifts it skipped
COMMAND = "bash test.sh"
TEST_TIMEOUT_S = (
    90  # under the pipeline's 120 s exec, so a hung test is this suite's receipt, not a crashed suite
)
REPORTS = "test-reports"  # where the run's JUnit XML lands, relative to the project root
OUTPUT_TAIL = 64 * 1024  # bytes of the run's output kept while it runs
SHOWN_OUTPUT = 4000  # characters of that tail the receipt carries
REPORT_BYTES = 4 * 1024 * 1024  # one report file, at most
MAX_REPORTS = 50
MAX_FAILURES = 20  # failing tests named, each its own check
MESSAGE_CHARS = 500
_OUTCOMES = ("failure", "error", "skipped")  # a JUnit testcase's child elements, in the order they decide


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


def run(job: dict[str, object]) -> list[dict[str, object]]:
    artifact_path = str(job["artifact_path"])
    expected_sha = str(job["artifact_sha256"])
    raw = Path(artifact_path).read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        return [
            _check(
                "bytes:sha256",
                STRUCTURAL,
                FAIL,
                "published bytes do not match their digest",
                expected=expected_sha,
                actual=actual_sha,
            )
        ]
    checks = [_check("bytes:sha256", STRUCTURAL, PASS, "matches the manifest digest")]

    workdir = tempfile.mkdtemp(prefix="verify-code-")
    try:
        with tarfile.open(artifact_path) as tar:
            tar.extractall(workdir, filter="data")  # no absolute paths, `..`, or device members
    except tarfile.TarError as exc:
        return [*checks, _check("code:present", STRUCTURAL, FAIL, f"artifact is not a readable tar: {exc}")]

    root = _project_root(Path(workdir))
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        return [*checks, _check("code:present", STRUCTURAL, FAIL, "the artifact is empty: no files to build")]
    checks.append(_check("code:present", STRUCTURAL, PASS, f"{len(files)} file(s)"))
    if not (root / "test.sh").is_file():
        return [*checks, _check(TESTS, SUBSTANTIVE, SKIP, "untested: the artifact ships no test.sh")]
    _fresh_reports(root)
    exit_code, output = _run_tests(root)
    return [*checks, *_receipt(root, exit_code, output)]


def _run_tests(root: Path) -> tuple[int | None, str]:
    """`test.sh`'s exit code (None: cut at `TEST_TIMEOUT_S`) and its output's tail."""
    proc = subprocess.Popen(
        ["/bin/bash", "test.sh"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # its own process group, so the cut reaches every child it started
    )
    assert proc.stdout is not None
    tail = _Tail(proc.stdout)
    try:
        exit_code: int | None = proc.wait(timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        exit_code = None
    # Leftover children of a finished run too (they hold the pipe); fail-soft: a group already gone is stopped.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    return exit_code, tail.text()


class _Tail:
    """The last `OUTPUT_TAIL` bytes of a stream, read on a thread so a chatty run fills neither memory nor the pipe."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._data = bytearray()
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: IO[bytes]) -> None:
        fd = stream.fileno()
        for chunk in iter(lambda: os.read(fd, 65536), b""):  # what the pipe holds now, never a full block
            self._data += chunk
            del self._data[:-OUTPUT_TAIL]

    def text(self) -> str:
        """The tail read so far, once the reader ends or a grace second passes (a child that escaped the group)."""
        self._thread.join(timeout=1)
        return bytes(self._data).decode("utf-8", errors="replace").strip()[-SHOWN_OUTPUT:]


def _fresh_reports(root: Path) -> None:
    """An empty `test-reports/`: the reports the artifact shipped are deleted, so only this run's count."""
    path = root / REPORTS
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.mkdir()


def _cases(root: Path) -> list[ElementTree.Element] | None:
    """Every testcase in this run's JUnit reports; None when it wrote none. Raises when one is unreadable."""
    folder = root / REPORTS
    if folder.is_symlink() or not folder.is_dir():
        return None
    files = sorted(p for p in folder.glob("*.xml") if p.is_file() and not p.is_symlink())[:MAX_REPORTS]
    if not files:
        return None
    cases: list[ElementTree.Element] = []
    for path in files:
        if path.stat().st_size > REPORT_BYTES:
            raise ValueError(f"{path.name} is over {REPORT_BYTES} bytes")
        cases.extend(ElementTree.parse(path).getroot().iter("testcase"))
    return cases


def _outcome(case: ElementTree.Element) -> tuple[str, str]:
    """A testcase's outcome (`failure`, `error`, `skipped` or `passed`) and the message its element carries."""
    for tag in _OUTCOMES:
        found = case.find(tag)
        if found is not None:
            return tag, (found.get("message") or found.text or "").strip()[:MESSAGE_CHARS]
    return "passed", ""


def _name(case: ElementTree.Element) -> str:
    classname, name = case.get("classname", ""), case.get("name", "")
    return f"{classname}::{name}" if classname else name


def _receipt(root: Path, exit_code: int | None, output: str) -> list[dict[str, object]]:
    """The `tests:run` check carrying the typed receipt, then one `test:<name>` check per failing test."""
    receipt: dict[str, object] = {"command": COMMAND, "exit_code": exit_code, "output": output}
    try:
        cases = _cases(root)
    except (ElementTree.ParseError, ValueError, OSError) as exc:  # fail-soft: the exit code still decides
        cases, receipt["report"] = None, "unreadable"
        receipt["output"] = f"{output}\n(test-reports unreadable: {exc})".strip()[-SHOWN_OUTPUT:]
    failing: list[tuple[str, str]] = []
    if cases is not None:
        counts = {"passed": 0, "failure": 0, "error": 0, "skipped": 0}
        for case in cases:
            outcome, message = _outcome(case)
            counts[outcome] += 1
            if outcome in ("failure", "error"):
                failing.append((_name(case), message))
        receipt |= {
            "report": "junit",
            "passed": counts["passed"],
            "failed": counts["failure"],
            "errors": counts["error"],
            "skipped": counts["skipped"],
        }
    receipt.setdefault("report", "none")
    receipt["failures"] = [{"name": n, "message": m} for n, m in failing[:MAX_FAILURES]]
    status, detail = _judged(receipt)
    head = _check(
        TESTS, SUBSTANTIVE, status, detail, expected="exit 0, no failing test", actual=json.dumps(receipt)
    )
    return [head, *(_check(f"test:{n}", SUBSTANTIVE, FAIL, m or "failed") for n, m in failing[:MAX_FAILURES])]


def _judged(receipt: dict[str, object]) -> tuple[str, str]:
    """The run's status and the one line a person reads, from the receipt's typed facts."""
    exit_code, counted = receipt["exit_code"], receipt["report"] == "junit"
    counts = (
        f"{receipt['passed']} passed, {receipt['failed']} failed, {receipt['errors']} errors, "
        f"{receipt['skipped']} skipped"
        if counted
        else f"no JUnit report in {REPORTS}/, so no counts"
    )
    if exit_code is None:
        return FAIL, f"{COMMAND} did not finish in {TEST_TIMEOUT_S}s and was stopped ({counts})"
    if exit_code != 0:
        return FAIL, f"{COMMAND} exited {exit_code} ({counts})"
    if counted and (receipt["failed"] or receipt["errors"]):
        return FAIL, f"{COMMAND} exited 0 but its report names failing tests ({counts})"
    if counted and not receipt["passed"]:
        return SKIP, f"untested: the run's report holds no passing test ({counts})"
    return PASS, f"{COMMAND} exited 0 ({counts})"


def _project_root(workdir: Path) -> Path:
    """The tar holds `<name>/...`, so the real project is the single top-level directory. If the tar
    was flat (no wrapping dir), the workdir itself is the root."""
    children = list(workdir.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return workdir


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        checks = run(job)
    except Exception as exc:  # a suite that crashed did not pass - say which, loudly
        checks = [_check("suite:ran", STRUCTURAL, FAIL, f"{type(exc).__name__}: {exc}")]
    json.dump({"checks": checks}, sys.stdout)


if __name__ == "__main__":
    main()
