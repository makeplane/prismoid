# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deck suite, against real `.pptx` in a real frame.

A deck is where a fabricated figure hides - a slide says a number and nobody opens the model. The
suite opens the `.pptx` and checks every substantial figure against the sources. This builds two decks
in a frame with `python-pptx`: one whose numbers come from the model, one with a planted figure that
does not. The grounded deck must PASS; the fabricated one must FAIL, naming the figure - that is the
whole reason the suite exists.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.verification import pipeline
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import CheckStatus

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-c")]

_SOURCE = "Q3 model - revenue was $60,000 across three product lines (10,000, 20,000, 30,000).\n"

# Builds two decks in /work and prints their {name: sha}. Both title a slide with a revenue figure;
# only "grounded" uses the model's number ($60,000). "fabricated" says $99,000, which is in no source.
_BUILD_TEMPLATE = r"""
from pptx import Presentation
import hashlib, json
out = {}
def deck(name, revenue):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = f"Q3 revenue was {revenue}"
    path = f"/work/{name}.pptx"; prs.save(path)
    out[name] = hashlib.sha256(open(path,"rb").read()).hexdigest()
deck("grounded", "$60,000")
deck("fabricated", "$99,000")
open("/work/model.md","w").write(%r)
print(json.dumps(out))
"""
# %-substitution, not str.format(): the embedded script is full of literal `{}` braces.
_BUILD = _BUILD_TEMPLATE % _SOURCE


@pytest.fixture
async def built() -> AsyncIterator[tuple[DockerFrame, dict[str, str]]]:
    ensure_image()
    frame = DockerFrame(task_id=f"test-deck-suite-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        await frame.write_file("/work/build.py", _BUILD)
        result = await frame.exec("/usr/local/bin/python3 /work/build.py")
        assert result.ok, f"fixture build failed: {result.stderr}"
        import json

        yield frame, json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        await frame.destroy()


async def _run(frame: DockerFrame, name: str, sha: str) -> dict[str, CheckStatus]:
    checks = await pipeline.run_suite(
        frame,
        VerifierSuite.DECK,
        artifact_path=f"/work/{name}.pptx",
        artifact_sha256=sha,
        sources=["/work/model.md"],
        allow_domains=[],
    )
    return {c.name: c.status for c in checks}


async def test_a_grounded_deck_passes(built: tuple[DockerFrame, dict[str, str]]) -> None:
    frame, shas = built
    status = await _run(frame, "grounded", shas["grounded"])
    assert status["bytes:sha256"] is CheckStatus.PASS
    assert status["format:slides"] is CheckStatus.PASS
    assert status["numbers:grounded"] is CheckStatus.PASS, "a figure from the model must ground"


async def test_a_fabricated_figure_fails(built: tuple[DockerFrame, dict[str, str]]) -> None:
    frame, shas = built
    status = await _run(frame, "fabricated", shas["fabricated"])
    assert status["numbers:grounded"] is CheckStatus.FAIL, "$99,000 is in no source - must be caught"


async def test_no_sources_skips_grounding_out_loud(built: tuple[DockerFrame, dict[str, str]]) -> None:
    # No source means the grounding check proved nothing - it must SKIP, not vacuously pass.
    frame, shas = built
    checks = await pipeline.run_suite(
        frame,
        VerifierSuite.DECK,
        artifact_path="/work/grounded.pptx",
        artifact_sha256=shas["grounded"],
        sources=[],
        allow_domains=[],
    )
    status = {c.name: c.status for c in checks}
    assert status["numbers:grounded"] is CheckStatus.SKIP
    # And "it has slides" is STRUCTURE (`format:` prefix) - with grounding skipped, a
    # sourceless deck has no substantive pass left, so its deterministic verdict is UNVERIFIED
    # (the judge may still promote on criteria), never a PASS earned by mere existence.
    from prismoid.ai.verification.pipeline import verdict_of
    from prismoid.schemas.verification import Verdict

    assert status["format:slides"] is CheckStatus.PASS
    assert verdict_of(checks) is Verdict.UNVERIFIED
