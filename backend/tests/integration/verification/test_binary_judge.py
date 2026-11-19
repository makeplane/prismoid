# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The judge reads a binary deliverable's words, or its PASS signs nothing.

A deck, a Word document or a workbook with no sources has only structural checks, so the receipt
rests on the judge. The suite already opens the file in the frame; it leaves the text it read, and
the judge is shown that. A judge shown a placeholder instead read nothing, and a PASS from it must
not turn the receipt green.

The files are built and checked in a real frame (python-docx, python-pptx and openpyxl live in the
frame image only); only the provider round trip is stubbed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import BinaryContent

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.gateway import providers
from prismoid.ai.verification import pipeline
from prismoid.db.session import session_scope
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import verification
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.usage import UsageRecord
from prismoid.schemas.verification import JudgeVerdict, SignedVerification, Verdict
from tests.support.artifacts import deliverable, verified
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]

_BUILD = r"""
import docx, openpyxl
from pptx import Presentation

essay = docx.Document()
essay.add_paragraph("Orbital tea ceremonies")
essay.add_paragraph("Steep for four minutes.")
essay.save("/work/essay.docx")

plan = Presentation()
plan.slides.add_slide(plan.slide_layouts[5]).shapes.title.text = "Lunar harvest plan"
plan.save("/work/plan.pptx")

book = openpyxl.Workbook()
sheet = book.active
sheet.title = "Camp"
sheet.append(["item", "cost"])
sheet.append(["tent", 40])
sheet.append(["stove", 60])
sheet["A4"], sheet["B4"] = "Subtotal", "=SUM(B2:B3)"
book.save("/work/camp.xlsx")
"""

# (type, published path, frame file, a line the judge must see, the suite's verdict alone)
FILES = [
    ("document", "/artifacts/essay.docx", "essay.docx", "Steep for four minutes.", Verdict.UNVERIFIED),
    ("deck", "/artifacts/plan.pptx", "plan.pptx", "Lunar harvest plan", Verdict.UNVERIFIED),
    ("spreadsheet", "/artifacts/camp.xlsx", "camp.xlsx", "=SUM(B2:B3) → 100", Verdict.PASS),
]
PLACEHOLDER = "you cannot read its raw bytes"


@pytest.fixture
async def office() -> AsyncIterator[dict[str, bytes]]:
    """A sourceless .docx, .pptx and .xlsx, built in a frame by the libraries that open them."""
    ensure_image()
    frame = DockerFrame(task_id=f"test-binary-judge-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        await frame.write_file("/work/build.py", _BUILD)
        built = await frame.exec("/usr/local/bin/python3 /work/build.py")
        assert built.ok, f"fixture build failed: {built.stderr}"
        yield {name: await frame.read_bytes(f"/work/{name}") for _, _, name, _, _ in FILES}
    finally:
        await frame.destroy()


@pytest.fixture
async def judged(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The prompts a judge of another family is asked, with a PASS for every one."""
    return await _a_judge(monkeypatch, ["judge"])


@pytest.fixture
async def sighted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The same, from a judge that can see images."""
    return await _a_judge(monkeypatch, ["judge", "vision"])


async def _a_judge(monkeypatch: pytest.MonkeyPatch, classes: list[str]) -> list[Any]:
    """Only the provider round trip is stubbed: the judge is built through the gateway on a model that
    did not write the work."""
    asked: list[Any] = []

    async def run(runner: AgentRunner, prompt: Any) -> Any:
        agent: Any = runner.agent
        deps: Any = runner.deps
        spec = agent.model.wrapped.spec
        asked.append(prompt)
        await deps.emit_usage(
            UsageRecord(
                task_id=deps.task_id,
                provider=spec.provider,
                model=spec.model_id,
                family=spec.family,
                units={},
                cost_usd=Decimal(0),
                price_version=1,
                at=dt.datetime.now(dt.UTC),
            )
        )
        return SimpleNamespace(output=JudgeVerdict(verdict=Verdict.PASS, rationale="reads well"))

    model_id = f"judge-{uuid.uuid4().hex[:8]}"

    async def order_for(session: Any) -> dict[str, list[str]]:
        return {ModelClass.JUDGE.value: [f"google:{model_id}"]}

    every_row = model_ops.routed_catalog

    async def only_this_judge(session: Any) -> Any:
        rows, order = await every_row(session)
        return [row for row in rows if row.model_id == model_id], order

    monkeypatch.setattr(providers, "api_key_for", lambda provider: "a-key")  # constructible, never called
    monkeypatch.setattr(AgentRunner, "run", run)
    monkeypatch.setattr(routing_ops, "order_for", order_for)
    # The only seat, whatever else the catalog holds.
    monkeypatch.setattr(model_ops, "routed_catalog", only_this_judge)
    async with session_scope() as s:
        await model_ops.add_model(
            s,
            provider="google",
            model_id=model_id,
            family="google",
            protocol="google",
            classes=classes,
            enabled=True,
        )
    return asked


async def _verify(kind: str, path: str, body: bytes) -> SignedVerification:
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="write it up")
        artifact = deliverable(task.id, body, kind=kind, path=path, title="The work")
        s.add(artifact)
        await s.commit()
    return await verified(artifact.id, workspace.id)


@pytest.mark.parametrize(("kind", "path", "name", "line", "alone"), FILES, ids=[kind for kind, *_ in FILES])
async def test_a_sourceless_binary_reaches_the_judge_as_its_text(
    silent_dispatcher: None,
    office: dict[str, bytes],
    judged: list[Any],
    kind: str,
    path: str,
    name: str,
    line: str,
    alone: Verdict,
) -> None:
    signed = await _verify(kind, path, office[name])

    assert pipeline.verdict_of(signed.report.checks) is alone
    assert len(judged) == 1
    assert line in judged[0] and PLACEHOLDER not in judged[0]
    assert signed.report.judge is not None
    assert signed.report.verdict is Verdict.PASS


async def test_a_judge_shown_only_a_placeholder_cannot_sign_a_pass(
    silent_dispatcher: None,
    office: dict[str, bytes],
    judged: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A judge's PASS over a placeholder is recorded but leaves the receipt UNVERIFIED."""

    async def left_no_text(**_kwargs: Any) -> verification.SuiteRun:
        return verification.SuiteRun(
            [
                pipeline.Check(
                    name="format:docx", status=pipeline.CheckStatus.PASS, kind=pipeline.CheckKind.STRUCTURAL
                )
            ]
        )

    monkeypatch.setattr(verification, "run_in_a_fresh_frame", left_no_text)

    signed = await _verify("document", "/artifacts/essay.docx", office["essay.docx"])

    assert len(judged) == 1 and PLACEHOLDER in judged[0]
    assert signed.report.judge is not None and signed.report.judge.verdict is Verdict.PASS
    assert signed.report.verdict is Verdict.UNVERIFIED


async def test_a_judge_that_can_see_is_shown_the_pages(
    silent_dispatcher: None, office: dict[str, bytes], sighted: list[Any]
) -> None:
    """A docx is rendered in the verify frame and its pages reach a vision judge after its text."""
    signed = await _verify("document", "/artifacts/essay.docx", office["essay.docx"])

    assert len(sighted) == 1 and isinstance(sighted[0], list)
    text, *rest = sighted[0]
    assert "Steep for four minutes." in text
    assert "[page 1]" in rest
    assert [part.media_type for part in rest if isinstance(part, BinaryContent)] == ["image/png"]
    assert signed.report.verdict is Verdict.PASS
