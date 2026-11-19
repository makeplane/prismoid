# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The maker: one agent makes every file a format skill builds, and the format skill seats its run.

The format skills' own manifests (`produces`, `format`, `capability`) are the table: which kinds the
maker makes, which skill a step's file takes, which model class that work wants and whether the system
publishes a typed answer. A maker step carries exactly one format skill, its deliverable's
(`skills.pin_format`), so a stored plan naming a retired maker resumes with its file's craft.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import definition_for, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.skills import deliverables
from prismoid.ai.skills import format as skill_format
from prismoid.operations import skills as skill_ops
from prismoid.schemas.contract import Contract, SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepSpec

NO_SESSION = cast("AsyncSession", None)  # `resolve_ref` is the only reader, and the fixture stands in for it


def _pin(name: str, version: int = 1) -> SkillPin:
    return SkillPin(name=SkillName(name), version=version)


def test_the_maker_makes_every_kind_a_format_skill_builds() -> None:
    assert set(get("maker").kinds) == {
        ArtifactType.DOCUMENT,
        ArtifactType.SPREADSHEET,
        ArtifactType.DECK,
        ArtifactType.CODE,
        ArtifactType.COLLECTION,
    }
    assert get("writer").kinds == (ArtifactType.DOCUMENT,), "a single-kind agent keeps its one kind"


def test_a_document_without_a_named_format_is_a_word_file() -> None:
    made = deliverables.format_for(ArtifactType.DOCUMENT)
    assert made is not None and made.skill == "docx"
    pdf = deliverables.format_for(ArtifactType.DOCUMENT, "pdf")
    assert pdf is not None and pdf.skill == "pdf"
    assert deliverables.format_for(ArtifactType.FLEET_REPORT) is None


def test_produces_and_format_round_trip_through_the_skill_file() -> None:
    parsed = skill_format.parse_dir(deliverables.BUILTIN_DIR / "pdf")
    again = skill_format.parse(
        "pdf", skill_format.render(parsed.manifest, parsed.body), "routing: []", strict=False
    )
    assert (again.manifest.produces, again.manifest.format) == (ArtifactType.DOCUMENT, "pdf")


def test_a_produces_that_names_no_kind_is_refused() -> None:
    text = "---\ndescription: Load when x, never for y.\nproduces: poster\n---\nbody\n"
    with pytest.raises(skill_format.SkillError, match="not an artifact kind"):
        skill_format.parse("poster", text, strict=False)


@pytest.mark.parametrize(
    ("skill", "model_class", "output_ref", "publishes_output"),
    [
        ("code", ModelClass.CODE, None, False),
        ("deck", ModelClass.FAST_EXECUTOR, None, False),
        ("page", ModelClass.FAST_EXECUTOR, None, False),
        ("organize", ModelClass.FAST_EXECUTOR, "collection_result", True),
    ],
)
def test_the_pinned_format_skill_seats_the_run(
    skill: str, model_class: ModelClass, output_ref: str | None, publishes_output: bool
) -> None:
    seated = definition_for("maker", TaskDeps(skill_pins=[_pin("report"), _pin(skill)]))
    assert seated.model_class is model_class
    assert (seated.output_ref, seated.publishes_output) == (output_ref, publishes_output)
    assert seated.tools == get("maker").tools


def test_a_maker_with_no_format_skill_is_the_bare_seat() -> None:
    assert definition_for("maker", TaskDeps(skill_pins=[_pin("report")])) == get("maker")


def _step(kind: ArtifactType | None, *pins: str, filename: str | None = None) -> StepSpec:
    output = OutputRef(type=kind, title="it", filename=filename) if kind is not None else None
    return StepSpec(
        agent=AgentName("maker"),
        contract=Contract(goal="make it", acceptance=[], output=output),
        skill_pins=[_pin(p) for p in pins],
    )


@pytest.fixture
def heads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`resolve_ref` against a registry whose every head is version 7; records what it resolved."""
    asked: list[str] = []

    async def resolve_ref(_session: Any, ref: str, **_: Any) -> SkillPin:
        asked.append(ref)
        return _pin(ref, 7)

    monkeypatch.setattr(skill_ops, "resolve_ref", resolve_ref)
    return asked


@pytest.mark.parametrize(
    ("step", "doc_format", "pinned"),
    [
        (_step(ArtifactType.CODE), None, ["code@7"]),
        (_step(ArtifactType.DECK, "model", "report"), None, ["report@1", "deck@7"]),
        (_step(ArtifactType.DOCUMENT, filename="brief.PDF"), "docx", ["pdf@7"]),
        (_step(ArtifactType.DOCUMENT, "page"), "pdf", ["page@1"]),
        (_step(ArtifactType.DOCUMENT), "html", ["page@7"]),
        (_step(ArtifactType.DOCUMENT), None, ["docx@7"]),
        (_step(ArtifactType.COLLECTION, "organize"), None, ["organize@1"]),
    ],
)
async def test_a_maker_step_carries_exactly_its_deliverables_format_skill(
    heads: list[str], step: StepSpec, doc_format: str | None, pinned: list[str]
) -> None:
    """By the file name's extension, else a format skill already pinned for the kind, else the task's
    file format; a pin of another kind's format skill goes, and every other skill stays."""
    pinned_now = await skill_ops.pin_format(NO_SESSION, step, doc_format)
    assert [pin.ref() for pin in pinned_now.skill_pins] == pinned


async def test_a_step_that_publishes_no_file_keeps_its_pins(heads: list[str]) -> None:
    step = _step(None, "model")
    assert await skill_ops.pin_format(NO_SESSION, step, None) is step
    assert heads == []
