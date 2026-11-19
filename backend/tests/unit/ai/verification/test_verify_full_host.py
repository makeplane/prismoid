# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A full host and the receipt (ADR-076): the wait ends, and it ends in a verdict.

Regression: `frame-verify-…` hit `quota_exceeded`, the job swallowed it, and a delivered
task said Pending for ever with nothing left to change that.
"""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.ai.verification import pipeline
from prismoid.errors import HostFull
from prismoid.operations import verification
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import CheckStatus, Verdict


class _Frame:
    def __init__(self, world: dict[str, int]) -> None:
        self.world = world

    async def create(self, **_kw: Any) -> None:
        self.world["tries"] += 1
        if self.world["tries"] <= self.world["full_for"]:
            raise HostFull("quota_exceeded at create: every host is full")

    async def write_bytes(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def read_bytes(self, _path: str) -> bytes:
        raise FileNotFoundError

    async def destroy(self) -> None:
        self.world["destroyed"] += 1


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    state = {"tries": 0, "full_for": 0, "destroyed": 0}
    monkeypatch.setattr(verification, "new_frame", lambda **_kw: _Frame(state))
    monkeypatch.setattr(verification, "ensure_ready", lambda _profile: None)
    monkeypatch.setattr(verification, "_FRAME_RETRY_S", 0.01)

    async def suite(*_a: Any, **_kw: Any) -> list[pipeline.Check]:
        return [
            pipeline.Check(name="ran", status=pipeline.CheckStatus.PASS, kind=pipeline.CheckKind.SUBSTANTIVE)
        ]

    monkeypatch.setattr(pipeline, "run_suite", suite)
    return state


async def _run(**kw: Any) -> list[pipeline.Check]:
    ran = await verification.run_in_a_fresh_frame(
        suite=VerifierSuite.DOCUMENT,
        artifact_name="/artifacts/a.md",
        payload=b"x",
        sha256="0" * 64,
        sources={},
        allow_domains=[],
        **kw,
    )
    return ran.checks


async def test_a_host_that_frees_up_in_time_runs_the_suite(world: dict[str, int]) -> None:
    world["full_for"] = 2
    checks = await _run(frame_wait_s=5)
    assert [c.name for c in checks] == ["ran"] and world["tries"] == 3 and world["destroyed"] == 1


async def test_a_host_that_stays_full_signs_unverified_and_says_why(world: dict[str, int]) -> None:
    world["full_for"] = 10_000
    checks = await _run(frame_wait_s=0.03)
    assert len(checks) == 1 and checks[0].status is CheckStatus.SKIP
    assert "no computer was free" in checks[0].detail
    assert pipeline.verdict_of(checks) is Verdict.UNVERIFIED


async def test_the_in_line_fleet_still_raises_a_full_host(world: dict[str, int]) -> None:
    world["full_for"] = 1
    with pytest.raises(HostFull):
        await _run()
