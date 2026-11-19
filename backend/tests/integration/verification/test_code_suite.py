# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The code suite, against real projects in a real frame of the image a code verify boots.

A `code` artifact is a directory, tarred at publish and rebuilt-and-tested by the suite. This drives
the whole path in a real frame: build projects, publish them (which tars the directory), and run the
suite via the real pipeline. Passing tests sign a PASS with the counts their JUnit report holds, a
failing test fails the receipt by its name (what the retry ladder hands the maker), and a project with
no tests is untested, never a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.skills.deliverables import verify_profile
from prismoid.ai.verification import pipeline
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import TESTS_RUN, Check, CheckStatus, CodeReceipt, JUnitRead

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-b")]

_ADD = "exports.add = (a, b) => a + b;\n"
_BROKEN = "exports.add = (a, b) => a - b;\n"  # the planted bug
_TESTS = (
    "const test = require('node:test');\nconst assert = require('node:assert');\n"
    "const { add } = require('./add.js');\n"
    "test('adds', () => assert.equal(add(2, 3), 5));\n"
    "test('adds negatives', () => assert.equal(add(-2, -3), -5));\n"
)
_TEST_SH = (
    "mkdir -p test-reports\n"
    "node --test --test-reporter=junit --test-reporter-destination=test-reports/node.xml "
    "--test-reporter=spec --test-reporter-destination=stdout add.test.js\n"
)
_PY_ADD = "def add(a, b):\n    return a + b\n"
_PY_BROKEN = "def add(a, b):\n    return a - b\n"
_PY_TESTS = (
    "from calc import add\n\n\ndef test_adds():\n    assert add(2, 3) == 5\n\n\n"
    "def test_adds_negatives():\n    assert add(-2, -3) == -5\n\n\ndef test_adds_zero():\n    assert add(0, 0) == 0\n"
)
_PY_TEST_SH = "mkdir -p test-reports\npytest --junitxml=test-reports/pytest.xml\n"


@pytest.fixture
async def frame() -> AsyncIterator[DockerFrame]:
    profile = verify_profile(VerifierSuite.CODE).value
    ensure_image(profile)
    f = DockerFrame(task_id=f"test-code-suite-{uuid.uuid4().hex[:8]}", profile=profile)
    await f.create(allow_domains=[])
    try:
        yield f
    finally:
        await f.destroy()


async def _publish(frame: DockerFrame, name: str, files: dict[str, str]) -> str:
    await frame.exec(f"mkdir -p /work/{name}")  # write_bytes tar-copies into an existing dir
    for filename, text in files.items():
        await frame.write_file(f"/work/{name}/{filename}", text)
    await frame.publish(f"/work/{name}", "code", name)
    manifest = await frame.manifest()
    entry = next(e for e in manifest["entries"] if e["path"] == f"/artifacts/{name}")
    return str(entry["sha256"])


async def _run(frame: DockerFrame, name: str, files: dict[str, str]) -> dict[str, Check]:
    sha = await _publish(frame, name, files)
    checks = await pipeline.run_suite(
        frame,
        VerifierSuite.CODE,
        artifact_path=f"/artifacts/{name}",
        artifact_sha256=sha,
        sources=[],
        allow_domains=[],
    )
    return {c.name: c for c in checks}


def _receipt(checks: dict[str, Check]) -> CodeReceipt:
    actual = checks[TESTS_RUN].actual
    assert actual is not None
    return CodeReceipt.model_validate_json(actual)


async def test_a_project_that_passes_its_tests_verifies_with_counts(frame: DockerFrame) -> None:
    checks = await _run(frame, "good", {"add.js": _ADD, "add.test.js": _TESTS, "test.sh": _TEST_SH})
    receipt = _receipt(checks)
    assert checks["bytes:sha256"].status is CheckStatus.PASS
    assert checks[TESTS_RUN].status is CheckStatus.PASS, checks[TESTS_RUN].detail
    assert (receipt.report, receipt.exit_code, receipt.passed, receipt.failed) == (JUnitRead.JUNIT, 0, 2, 0)


async def test_a_broken_build_fails_by_the_test_it_breaks(frame: DockerFrame) -> None:
    checks = await _run(frame, "broken", {"add.js": _BROKEN, "add.test.js": _TESTS, "test.sh": _TEST_SH})
    receipt = _receipt(checks)
    assert checks[TESTS_RUN].status is CheckStatus.FAIL
    assert receipt.failed == 2
    assert {f.name for f in receipt.failures} == {"test::adds", "test::adds negatives"}
    assert checks["test:test::adds"].status is CheckStatus.FAIL


async def test_code_with_no_test_is_untested_not_a_pass(frame: DockerFrame) -> None:
    checks = await _run(frame, "notest", {"add.js": _ADD})
    assert checks[TESTS_RUN].status is CheckStatus.SKIP
    assert pipeline.verdict_of(list(checks.values())).value == "unverified"


async def test_a_python_project_is_counted_from_its_pytest_report(frame: DockerFrame) -> None:
    files = {"calc.py": _PY_ADD, "test_calc.py": _PY_TESTS, "test.sh": _PY_TEST_SH}
    good = await _run(frame, "pygood", files)
    assert good[TESTS_RUN].status is CheckStatus.PASS, good[TESTS_RUN].detail
    assert (_receipt(good).report, _receipt(good).passed, _receipt(good).failed) == (JUnitRead.JUNIT, 3, 0)

    broken = await _run(frame, "pybroken", {**files, "calc.py": _PY_BROKEN})
    receipt = _receipt(broken)
    assert broken[TESTS_RUN].status is CheckStatus.FAIL
    assert (receipt.passed, receipt.failed) == (1, 2)
    assert {f.name for f in receipt.failures} == {"test_calc::test_adds", "test_calc::test_adds_negatives"}
