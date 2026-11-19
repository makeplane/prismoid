# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The lockstep doctrine (ADR-015): `schemas/collection.Collection` is the ONE definition of a
valid collection; the in-frame suite is its stdlib mirror. These tests hold the two together
mutation by mutation - a rule added to the model without its mirror (or vice versa) fails here,
in microseconds, not in production as a verdict that means the wrong thing.

Direction matters both ways, as the document suite learned: a mirror that misses a violation
makes the product a liar; a mirror that flags a legal collection makes the verifier something
somebody switches off.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from prismoid.ai.verification.pipeline import verdict_of
from prismoid.ai.verification.suites.collection import run
from prismoid.schemas.collection import Collection
from prismoid.schemas.verification import Check, Verdict
from tests.support.samples import FIXTURE, MUTATIONS, mutate

SOURCE = "The line expansion costs $41.6M this quarter."


def suite_checks(doc: dict[str, Any], tmp_path: Path, *, source: str = SOURCE) -> list[dict[str, object]]:
    raw = json.dumps(doc).encode()
    file = tmp_path / "c.collection.json"
    file.write_bytes(raw)
    sources = []
    if source:
        src = tmp_path / "brief.md"
        src.write_text(source)
        sources = [str(src)]
    return run(
        {
            "artifact_path": str(file),
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "sources": sources,
            "allow_domains": [],
        }
    )


def suite_verdict(doc: dict[str, Any], tmp_path: Path) -> Verdict:
    return verdict_of([Check.model_validate(c) for c in suite_checks(doc, tmp_path)])


# ── the clean fixture passes BOTH definitions ─────────────────────────────────────────────────


def test_the_fixture_is_legal_in_both_definitions(tmp_path: Path) -> None:
    Collection.model_validate(FIXTURE)  # the model says yes
    assert suite_verdict(FIXTURE, tmp_path) is Verdict.PASS, (
        "the mirror must agree - and PASS, not unverified"
    )


def test_a_grounded_number_cell_passes_and_says_verbatim(tmp_path: Path) -> None:
    grounded = next(c for c in suite_checks(FIXTURE, tmp_path) if c["name"] == "numbers:grounded")
    assert grounded["status"] == "pass"
    assert "no derivation" in str(grounded["detail"]), "'grounded' must not over-claim"


# ── every mutation fails BOTH definitions (the lockstep) ──────────────────────────────────────


@pytest.mark.parametrize("doc", MUTATIONS.values(), ids=MUTATIONS.keys())
def test_the_model_and_the_mirror_refuse_the_same_mutations(doc: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Collection.model_validate(doc)
    assert suite_verdict(doc, tmp_path) is Verdict.FAIL, "the mirror let through what the model refuses"


# ── grounding is its own axis ─────────────────────────────────────────────────────────────────


def test_an_invented_number_fails_grounding_but_not_the_model(tmp_path: Path) -> None:
    """Schema-legal and still wrong: 99.9 appears in no source. The model accepts it (shape is
    fine); the suite fails it (grounding is the frame's job, not the schema's)."""
    doc = mutate("rows.0.cells", {"cost": 99.9})
    Collection.model_validate(doc)
    grounded = next(c for c in suite_checks(doc, tmp_path) if c["name"] == "numbers:grounded")
    assert grounded["status"] == "fail"
    assert "99.9" in str(grounded["detail"])


def test_no_sources_means_grounding_skips_never_passes(tmp_path: Path) -> None:
    checks = suite_checks(FIXTURE, tmp_path, source="")
    grounded = next(c for c in checks if c["name"] == "numbers:grounded")
    assert grounded["status"] == "skip"
    # And the collection can still PASS: the reference/type checks are substantive on their own.
    assert verdict_of([Check.model_validate(c) for c in checks]) is Verdict.PASS
