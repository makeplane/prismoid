# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The code suite's receipt: a project's own tests, run, read from their JUnit report, cut at the cap.

The suite runs as a frame runs it (a separate interpreter, a job in, checks out), with its time cap
lowered so a hung test costs a second; a suite that cannot cut a hung run fails here by the watchdog.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from prismoid.ai.verification import pipeline
from prismoid.schemas.model_class import Family
from prismoid.schemas.verification import (
    TESTS_RUN,
    Check,
    CheckStatus,
    CodeReceipt,
    JudgeOpinion,
    JUnitRead,
    Verdict,
)

SUITE = Path(pipeline.__file__).parent / "suites" / "code.py"
HUNG_CAP_S = 2  # the cap a hung run is cut at, so a hang costs two seconds
# The cap a run that finishes is given: it starts pytest in a fresh interpreter, which on a loaded
# runner under coverage takes most of two seconds on its own.
CAP_S = 30
WATCHDOG_S = 60  # a suite that hangs past its own cap fails the test here
PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider --junitxml=test-reports/pytest.xml'
ADD = "def add(a, b):\n    return a + b\n"
TESTS = (
    "from add import add\n\n"
    "def test_adds():\n    assert add(2, 3) == 5\n\n"
    "def test_adds_negatives():\n    assert add(-2, -3) == -5\n"
)
BROKEN = TESTS + "\ndef test_breaks():\n    assert add(1, 1) == 3, 'add is wrong'\n"
_RUNNER = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("code_suite", sys.argv[1])
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)
suite.TEST_TIMEOUT_S = int(sys.argv[2])
json.dump(suite.run(json.load(sys.stdin)), sys.stdout)
"""


def _artifact(tmp_path: Path, files: dict[str, str]) -> dict[str, object]:
    """A `code` artifact as publish tars it (`project/...`), and the suite's job for it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(f"project/{name}")
            member.size, member.mode = len(data), 0o755
            tar.addfile(member, io.BytesIO(data))
    path = tmp_path / "project.tar"
    path.write_bytes(buffer.getvalue())
    return {"artifact_path": str(path), "artifact_sha256": hashlib.sha256(buffer.getvalue()).hexdigest()}


def _suite(tmp_path: Path, files: dict[str, str], *, cap_s: int = CAP_S) -> dict[str, Check]:
    """The suite's checks by name, run in its own interpreter under the watchdog."""
    job = json.dumps(_artifact(tmp_path, files))
    done = subprocess.run(
        [sys.executable, "-c", _RUNNER, str(SUITE), str(cap_s)],
        input=job,
        capture_output=True,
        text=True,
        timeout=WATCHDOG_S,
        check=True,
    )
    return {c.name: c for c in (Check.model_validate(raw) for raw in json.loads(done.stdout))}


def _receipt(checks: dict[str, Check]) -> CodeReceipt:
    actual = checks[TESTS_RUN].actual
    assert actual is not None
    return CodeReceipt.model_validate_json(actual)


def _judge_pass() -> JudgeOpinion:
    return JudgeOpinion(model="judge", family=Family("openai"), verdict=Verdict.PASS, rationale="reads right")


def test_passing_tests_sign_a_pass_with_their_counts(tmp_path: Path) -> None:
    checks = _suite(tmp_path, {"add.py": ADD, "test_add.py": TESTS, "test.sh": PYTEST})
    receipt = _receipt(checks)
    assert checks[TESTS_RUN].status is CheckStatus.PASS
    assert (receipt.report, receipt.exit_code) == (JUnitRead.JUNIT, 0)
    assert (receipt.passed, receipt.failed, receipt.errors) == (2, 0, 0)
    assert pipeline.verdict_of(list(checks.values())) is Verdict.PASS


def test_a_failing_test_fails_the_receipt_by_its_name(tmp_path: Path) -> None:
    checks = _suite(tmp_path, {"add.py": ADD, "test_add.py": BROKEN, "test.sh": PYTEST})
    receipt = _receipt(checks)
    assert checks[TESTS_RUN].status is CheckStatus.FAIL
    assert (receipt.passed, receipt.failed) == (2, 1)
    assert [f.name for f in receipt.failures] == ["test_add::test_breaks"]
    assert checks["test:test_add::test_breaks"].status is CheckStatus.FAIL
    assert "add is wrong" in checks["test:test_add::test_breaks"].detail
    assert pipeline.verdict_of(list(checks.values())) is Verdict.FAIL


def test_a_failure_the_script_swallowed_still_fails(tmp_path: Path) -> None:
    checks = _suite(tmp_path, {"add.py": ADD, "test_add.py": BROKEN, "test.sh": f"{PYTEST} || true\n"})
    assert _receipt(checks).exit_code == 0
    assert checks[TESTS_RUN].status is CheckStatus.FAIL


def test_no_tests_is_untested_and_no_judge_lifts_it(tmp_path: Path) -> None:
    checks = _suite(tmp_path, {"add.py": ADD})
    assert checks[TESTS_RUN].status is CheckStatus.SKIP
    assert "untested" in checks[TESTS_RUN].detail
    listed = list(checks.values())
    assert pipeline.verdict_of(listed) is Verdict.UNVERIFIED
    assert pipeline.final_verdict(listed, _judge_pass(), judge_read_content=True) is Verdict.UNVERIFIED


def test_a_run_that_collected_no_test_is_untested(tmp_path: Path) -> None:
    checks = _suite(tmp_path, {"add.py": ADD, "test.sh": f"{PYTEST} || exit 0\n"})
    assert _receipt(checks).passed == 0
    assert checks[TESTS_RUN].status is CheckStatus.SKIP


def test_a_report_the_artifact_shipped_is_never_counted(tmp_path: Path) -> None:
    shipped = '<testsuite><testcase classname="t" name="fake"/></testsuite>'
    checks = _suite(tmp_path, {"add.py": ADD, "test-reports/old.xml": shipped, "test.sh": "exit 0\n"})
    receipt = _receipt(checks)
    assert receipt.report is JUnitRead.NONE
    assert receipt.passed is None


def test_a_hung_test_is_cut_at_the_cap(tmp_path: Path) -> None:
    hung = (
        "echo started\nsleep 600\necho never\n"  # sleep, a child, holds the output pipe after test.sh is cut
    )
    began = time.monotonic()
    checks = _suite(tmp_path, {"add.py": ADD, "test.sh": hung}, cap_s=HUNG_CAP_S)
    receipt = _receipt(checks)
    assert time.monotonic() - began < HUNG_CAP_S + 10
    assert checks[TESTS_RUN].status is CheckStatus.FAIL
    assert receipt.exit_code is None
    assert "started" in receipt.output


def test_a_chatty_run_keeps_only_a_tail(tmp_path: Path) -> None:
    chatty = "head -c 5000000 /dev/zero | tr '\\0' x\necho end\n"
    receipt = _receipt(_suite(tmp_path, {"add.py": ADD, "test.sh": chatty}))
    assert len(receipt.output) <= 4000
    assert receipt.output.endswith("end")
