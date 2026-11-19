# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Planted defects through the real verification pipeline: each is caught, or is a known miss on record.

`evals/fixtures/defects/build.py` builds every deliverable twice in a frame, clean and with exactly one
planted defect. Both copies go through `run_in_a_fresh_frame` (the suite for their artifact type, in a
fresh frame with only the published and source bytes) and `final_verdict`, with no judge: the judge needs
a second model family, which a local one-family catalog does not have. On the claims path a document's
claims are scripted as the claim reader would list them (`READS`) and grounded against its sources.

Caught means the verdict is FAIL. A clean twin must never FAIL, or a catch would prove nothing. A known
miss is asserted NOT caught, so this file goes red the day a verification change starts catching it:
delete its row then. A defect that stops being caught goes red too.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from prismoid.ai.frames import ensure_ready, new_frame
from prismoid.ai.verification import grounding, pipeline
from prismoid.ai.verification.grounding import Source
from prismoid.operations import verification
from prismoid.schemas.claims import GROUNDING, Claim, Grounding
from prismoid.schemas.outputs import ArtifactType, VerifierSuite, spec_for
from prismoid.schemas.verification import Verdict
from tests.support.paths import BACKEND

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-b")]

BUILD = BACKEND / "evals" / "fixtures" / "defects" / "build.py"
REGIONS = {"North": "12.4", "South": "18.1", "East": "9.5", "West": "1.6"}  # the fixtures' brief, as written
OUT = "/work/defects"

# The defects today's deterministic suites let through, and why. Each row asserts the miss.
KNOWN_MISSES: dict[str, str] = {
    "missing_section": "no suite reads the sections a contract asks for; only the judge reads the criteria",
    "tables_out_of_order": "the reader keeps each table under its heading, but whether a heading names its "
    "table is meaning, and only the judge reads meaning",
    "empty_slide": "the deck suite fails only a deck with no slides; a blank slide among others passes",
    "truncated_text": "no deterministic check reads whether a document ends mid-sentence",
    "stale_date": "date cells are checked for type, never against the sources",
}


@dataclass(frozen=True)
class Verdicts:
    """One defect's two verdicts: the clean twin's and the planted copy's."""

    clean: Verdict
    planted: Verdict


def _claim(cid: str, figure: str, quote: str = "", formula: str | None = None) -> Claim:
    """A claim as the claim reader lists it: the figure, and the brief's span stating it."""
    found = figure if quote and figure in quote else None
    return Claim(
        id=cid, statement=figure, figure=figure, quote=quote or None, quoted_figure=found, formula=formula
    )


REVENUE, MARGIN_CLAIM = "Revenue: $41.6M in Q3 2026", "Gross margin: 46.5%"
REGION_CLAIMS = [_claim(f"c{i}", f"${v}M", f"{n} ${v}M") for i, (n, v) in enumerate(REGIONS.items(), 1)]
# What a claim reader lists for each copy of a document case, scripted: the claims path grounds these.
READS: dict[tuple[str, str], list[Claim]] = {
    ("wrong_figure", "clean"): [_claim("c1", "$41.6M", REVENUE), _claim("c2", "46.5%", MARGIN_CLAIM)],
    ("wrong_figure", "planted"): [
        _claim("c1", "$46.1M", REVENUE).model_copy(update={"quoted_figure": "$41.6M"}),
        _claim("c2", "46.5%", MARGIN_CLAIM),
    ],
    ("wrong_total", "clean"): [*REGION_CLAIMS, _claim("c5", "$41.6M", formula="c1 + c2 + c3 + c4")],
    ("wrong_total", "planted"): [*REGION_CLAIMS, _claim("c5", "$42.6M", formula="c1 + c2 + c3 + c4")],
}


async def _verify(
    defect: str, kind: ArtifactType, name: str, payload: bytes, sources: dict[str, bytes]
) -> Verdict:
    """The verdict the pipeline signs for these bytes when no judge can run, its claims read by script."""
    suite = spec_for(kind).verifier_suite
    ran = await verification.run_in_a_fresh_frame(
        suite=suite,
        artifact_name=name,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        sources=sources,
        allow_domains=[],
    )
    checks = list(ran.checks)
    if suite is VerifierSuite.DOCUMENT and GROUNDING is Grounding.CLAIMS and ran.extracted:
        given = [Source(n, d.decode()) for n, d in sources.items()]
        read = READS.get((defect, name.split(".", 1)[0]), [])
        checks.append(grounding.check(grounding.ground(read, given, document=ran.extracted)))
    return pipeline.final_verdict(checks, None, judge_read_content=False)


@pytest_asyncio.fixture
async def verdicts() -> AsyncIterator[dict[str, Verdicts]]:
    """Build the fixtures in one frame, then verify every clean and planted copy."""
    ensure_ready()
    frame = new_frame(task_id=f"test-planted-defects-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    files: dict[str, bytes] = {}
    try:
        await frame.write_bytes("/work/build.py", BUILD.read_bytes())
        result = await frame.exec(f"/usr/local/bin/python3 /work/build.py {OUT}")
        assert result.ok, f"fixture build failed: {result.stderr}"
        manifest: list[dict[str, Any]] = json.loads(result.stdout.strip().splitlines()[-1])
        for case in manifest:
            for name in [f"clean{case['ext']}", f"planted{case['ext']}", *case["sources"]]:
                path = f"{case['defect']}/{name}"
                files[path] = await frame.read_bytes(f"{OUT}/{path}")
    finally:
        await frame.destroy()

    out: dict[str, Verdicts] = {}
    for case in manifest:
        defect, kind, ext = str(case["defect"]), ArtifactType(case["type"]), str(case["ext"])
        sources = {name: files[f"{defect}/{name}"] for name in case["sources"]}
        clean = await _verify(defect, kind, f"clean{ext}", files[f"{defect}/clean{ext}"], sources)
        planted = await _verify(defect, kind, f"planted{ext}", files[f"{defect}/planted{ext}"], sources)
        out[defect] = Verdicts(clean=clean, planted=planted)
    yield out


async def test_each_planted_defect_is_caught_or_a_known_miss(verdicts: dict[str, Verdicts]) -> None:
    false_fails = sorted(d for d, v in verdicts.items() if v.clean is Verdict.FAIL)
    missed = {d for d, v in verdicts.items() if v.planted is not Verdict.FAIL}
    assert not false_fails, f"clean twins failed verification, so their catches prove nothing: {false_fails}"
    assert set(KNOWN_MISSES) <= set(verdicts), "a known miss names a defect the fixtures do not build"
    assert missed - set(KNOWN_MISSES) == set(), "missed, and not on the known-miss table"
    assert set(KNOWN_MISSES) - missed == set(), "now caught: delete its known-miss row"
