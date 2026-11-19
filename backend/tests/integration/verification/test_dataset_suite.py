# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dataset suite, against real files in a real frame (M1).

Same citizenship as the spreadsheet suite's test: the suite is exercised exactly where it runs in
production - copied into a fresh frame by the **real pipeline** and fed the job over stdin. The
unit tests (`tests/unit/ai/verification/test_dataset_suite.py`) cover its logic in microseconds; this file proves
the frame contract (absolute interpreter, stdin/stdout JSON, /work paths).

The doctrinal end-to-end assertion rides `verdict_of`: a clean CSV's checks are all structural
(`format:*`), so the verdict is UNVERIFIED - well-formed is not verified - while a corrupt one
FAILs. Publishing a `.csv` used to crash the step here (`SuiteNotBuilt`, uncaught).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.verification import pipeline
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import CheckStatus, Verdict

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]

CLEAN = b"metric,value_musd\nrevenue,41.6\nservices,9.2\nhardware,32.4\n"
RAGGED = b"metric,value_musd\nrevenue,41.6\nservices\n"


@pytest.fixture
async def frame() -> AsyncIterator[DockerFrame]:
    ensure_image()
    box = DockerFrame(task_id=f"test-dataset-suite-{uuid.uuid4().hex[:8]}")
    await box.create(allow_domains=[])
    try:
        yield box
    finally:
        await box.destroy()


async def _run(frame: DockerFrame, name: str, raw: bytes, *, sha: str | None = None) -> list[pipeline.Check]:
    await frame.write_bytes(f"/work/{name}", raw, mode=0o444, owner="system")
    return await pipeline.run_suite(
        frame,
        VerifierSuite.DATASET,
        artifact_path=f"/work/{name}",
        artifact_sha256=sha or hashlib.sha256(raw).hexdigest(),
        sources=[],
        allow_domains=[],
    )


async def test_a_clean_csv_parses_in_frame_and_ships_unverified(frame: DockerFrame) -> None:
    checks = await _run(frame, "rows.csv", CLEAN)
    status = {c.name: c.status for c in checks}
    assert status["bytes:sha256"] is CheckStatus.PASS
    assert status["format:parse"] is CheckStatus.PASS
    assert status["format:shape"] is CheckStatus.PASS
    assert pipeline.verdict_of(checks) is Verdict.UNVERIFIED, "well-formed is not verified"


async def test_a_ragged_csv_fails_in_frame(frame: DockerFrame) -> None:
    checks = await _run(frame, "bad.csv", RAGGED)
    status = {c.name: c.status for c in checks}
    assert status["format:parse"] is CheckStatus.FAIL
    assert pipeline.verdict_of(checks) is Verdict.FAIL


async def test_a_tampered_digest_is_caught(frame: DockerFrame) -> None:
    checks = await _run(frame, "rows2.csv", CLEAN, sha="0" * 64)
    assert {c.name: c.status for c in checks}["bytes:sha256"] is CheckStatus.FAIL
