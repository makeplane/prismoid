# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A long document's receipt and its assembly floor, on real rows.

The receipt's claims are read from the parts the document was assembled from, packed whole into readings
of the reader's bound, so a wrong figure in a late part fails the receipt though the suite's extract is cut
long before it. A revision's assembly counts and reads only the parts that feed it, never an earlier turn's.
The claim reader is the real `claim_extract` agent on a scripted model; no live model.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.verification import grounding
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import PolicyDenied
from prismoid.operations import grounding as grounding_ops
from prismoid.operations import longform as longform_ops
from prismoid.operations import models as model_ops
from prismoid.operations import planning_steps
from prismoid.operations import routing as routing_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution.mounts import upstream_artifacts
from prismoid.schemas.claims import Claim
from prismoid.schemas.longform import OutlineResult, SectionOutline
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.plan import StepState
from prismoid.schemas.usage import Unit
from prismoid.schemas.verification import CheckStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration

BRIEF = "# Northwind Q3 2026\n\n- Revenue: $41.6M in Q3 2026.\n- Gross margin: 46.5%.\n"
FILLER = "The bin needs air, water and patience, turned once a week. " * 700  # ~42k chars a part
REVENUE = Claim(
    id="c1",
    statement="Revenue was $41.6M in Q3 2026.",
    figure="$41.6M",
    quote="Revenue: $41.6M in Q3 2026",
    quoted_figure="$41.6M",
)


def _margin(figure: str) -> Claim:
    """The closing part's margin claim, at the figure the part states."""
    return Claim(
        id="c2",
        statement=f"Gross margin closed the year at {figure}.",
        figure=figure,
        quote="Gross margin: 46.5%",
        quoted_figure="46.5%",
    )


def _outline(titles: tuple[str, ...]) -> OutlineResult:
    return OutlineResult(
        filename="northwind",
        title="Northwind",
        format_target="md",
        continuity_bible="Plain words.",
        summary="A report.",
        sections=[
            SectionOutline(id=f"s{i:02d}", title=t, brief=f"Cover {t}.", target_words=900)
            for i, t in enumerate(titles, start=1)
        ],
    )


async def _lane(
    session: AsyncSession, task: Task, titles: tuple[str, ...], *, base: int
) -> tuple[PlanStep, list[PlanStep]]:
    """One turn's lane as its outline expanded, written: the assembly row and its part rows."""
    [(outline_id, outline)], _, _ = longform_ops.longform_plan(task.goal, target_pages=45)
    wave, edges = longform_ops.expand(
        _outline(titles), source_paths=[], goal=task.goal, outline_step=outline_id
    )
    rows = await planning_steps.write_steps(
        session, task, [(outline_id, outline), *wave], edges, base_ordinal=base
    )
    return rows[-1], rows[1:-1]


def _published(task: Task, step: PlanStep, body: str, *, role: str = "working") -> Artifact:
    data = body.encode()
    digest = storage.put(data)
    row_id = uuid.uuid4()
    return Artifact(
        id=row_id,
        lineage_id=row_id,
        task_id=task.id,
        step_id=step.id,
        type="document",
        title=step.title,
        role=role,
        sha256=digest,
        bytes=len(data),
        storage_key=storage.key_for(digest),
        provenance={"path": f"/artifacts/{step.title}.md", "sources": []},
        created_at=utcnow(),
    )


def _reader(monkeypatch: pytest.MonkeyPatch, claims: list[Claim], shown: list[str]) -> Any:
    """The real claim reader on a scripted model that lists each claim its reading's document states."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = str(messages)
        shown.append(prompt)
        listed = [c.model_dump() for c in claims if c.statement in prompt]
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"claims": listed})]
        )

    row = ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get("claim_extract").model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )
    runner = build("claim_extract", TaskDeps(task_id=None), [row])

    async def catalog(_session: object) -> tuple[list[ModelSpec], dict[str, list[str]]]:
        return [row], {}

    async def settings(_session: object) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(model_ops, "routed_catalog", catalog)
    monkeypatch.setattr(routing_ops, "settings_for", settings)
    monkeypatch.setattr(grounding_ops.agents, "build", lambda *_a, **_k: runner)
    return runner.agent.override(model=FunctionModel(model))


async def _receipt(monkeypatch: pytest.MonkeyPatch, margin: str) -> tuple[CheckStatus, list[str]]:
    """The grounding check a three-part document signs, its closing part stating `margin`; the readings."""
    async with session_scope() as session:
        actor, _ = await member(session, "Grounder")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="a report on Northwind")
        assembly, parts = await _lane(session, task, ("Revenue", "Context", "Margin"), base=0)
        texts = [
            f"Revenue was $41.6M in Q3 2026.\n\n{FILLER}",
            FILLER,
            f"Gross margin closed the year at {margin}.",
        ]
        for part, text in zip(parts, texts, strict=True):
            session.add(_published(task, part, text))
        book = "\n\n".join(texts)
        document = book[: grounding.DOCUMENT_CHARS]  # what the suite's extract hands on: the book, cut
        deliverable = _published(task, assembly, book, role="deliverable")
        session.add(deliverable)
        await session.flush()
        assert book.index("Gross margin") > grounding.DOCUMENT_CHARS, (
            "the margin sits past one reading's bound"
        )
        shown: list[str] = []
        files = {"brief.md": BRIEF.encode()}
        with _reader(monkeypatch, [REVENUE, _margin(margin)], shown):
            [check] = await grounding_ops.ground(
                session, deliverable, VerifierSuite.DOCUMENT, document, files=files, pages=[]
            )
        await session.rollback()
    return check.status, shown


async def test_a_wrong_figure_in_a_late_part_fails_the_long_documents_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, shown = await _receipt(monkeypatch, "52.5%")
    assert status is CheckStatus.FAIL, "the planted margin in the closing part fails the receipt"
    assert len(shown) == 2, "the parts are packed into readings: the whole book, never one prompt"
    assert all(len(prompt) < 2 * grounding.READING_CHARS for prompt in shown), "each reading is bounded"


async def test_the_clean_long_document_passes_its_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    status, _ = await _receipt(monkeypatch, "46.5%")
    assert status is CheckStatus.PASS


async def test_a_revision_assembles_from_its_own_turns_parts_only() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Reviser")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="a report on Northwind")
        earlier = utcnow() - dt.timedelta(hours=1)
        first, old_parts = await _lane(session, task, ("A", "B", "C"), base=0)
        for part in [*old_parts, first]:
            part.state = StepState.DONE.value
        for part in old_parts:
            row = _published(task, part, f"turn one: {part.title}")
            row.created_at = earlier
            session.add(row)
        second, new_parts = await _lane(session, task, ("A", "B", "C"), base=10)
        new_parts[0].state = StepState.DONE.value
        for part in new_parts[1:]:
            part.state = StepState.ESCALATED.value
        session.add(_published(task, new_parts[0], f"turn two: {new_parts[0].title}"))
        await session.flush()

        with pytest.raises(PolicyDenied, match="only 1 of 3 parts"):
            await longform_ops.assembly_ready_guard(session, task, second)
        mounted = {a.step_id for a in await upstream_artifacts(session, task, second)}
        assert mounted == {new_parts[0].id}, "assembly mounts only the parts of its own turn"

        for part in new_parts[1:]:
            part.state = StepState.DONE.value
            session.add(_published(task, part, f"turn two: {part.title}"))
        await session.flush()
        await longform_ops.assembly_ready_guard(session, task, second)  # every part of this turn is in
        deliverable = _published(task, second, "the revision", role="deliverable")
        session.add(deliverable)
        await session.flush()
        assert await grounding_ops.parts(session, deliverable) == [f"turn two: {p.title}" for p in new_parts]
        await session.rollback()
