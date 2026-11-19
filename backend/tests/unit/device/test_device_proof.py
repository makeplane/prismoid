# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`just device-proof` (ADR-078): a device test that cannot run fails under the proof, and only there.
A laptop without Chrome still skips, and a run on purpose can no longer read green for having run
nothing."""

from __future__ import annotations

import pytest

from tests.support import markers


@pytest.fixture
def proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMOID_DEVICE_PROOF", "1")


def test_a_test_that_cannot_run_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISMOID_DEVICE_PROOF", raising=False)
    mark = markers.requires(False, "no Chrome").mark
    assert mark.name == "skipif" and mark.args == (True,) and mark.kwargs["reason"] == "no Chrome"
    with pytest.raises(pytest.skip.Exception, match="no Docker"):
        markers.cannot_run("no Docker")


@pytest.mark.usefixtures("proof")
def test_under_the_proof_it_fails() -> None:
    mark = markers.requires(False, "no Chrome").mark
    assert mark.name == "must_run" and mark.args == ("no Chrome",)
    with pytest.raises(pytest.fail.Exception, match="no Docker"):
        markers.cannot_run("no Docker")


@pytest.mark.usefixtures("proof")
def test_a_test_that_can_run_is_left_alone() -> None:
    mark = markers.requires(True, "no Chrome").mark
    assert mark.name == "skipif" and mark.args == (False,)
