# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A code step boots the code image, every other step keeps its own, and a code re-run verifies on the same tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from prismoid.ai.agents.base import frame_profile_for, roster
from prismoid.ai.skills import deliverables
from prismoid.ai.skills.format import SkillError, parse, parse_dir, render
from prismoid.db.models.task import Task
from prismoid.operations import machines
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.machine_profile import MachineProfile, widen
from prismoid.schemas.outputs import ArtifactType, OutputRef, VerifierSuite
from prismoid.schemas.plan import StepSpec

CLOUD = Task(machine_driver=None)
DEVICE = Task(machine_driver="device")
CODE_SKILL = Path(deliverables.BUILTIN_DIR) / "code"


@pytest.fixture(autouse=True)
def registry() -> None:
    roster()  # the registry fills on first use


def _step(agent: str, makes: ArtifactType | None = None) -> StepSpec:
    output = OutputRef(type=makes, title="the deliverable") if makes is not None else None
    return StepSpec(agent=AgentName(agent), contract=Contract(goal="do it", output=output))


def test_a_code_step_lands_on_the_code_image() -> None:
    assert machines.seat_profile(CLOUD, _step("maker", ArtifactType.CODE)) == MachineProfile.CODE


@pytest.mark.parametrize(
    ("agent", "makes", "profile"),
    [
        ("maker", ArtifactType.DOCUMENT, MachineProfile.BASE),
        ("maker", ArtifactType.SPREADSHEET, MachineProfile.BASE),
        ("writer", None, MachineProfile.BASE),
        ("researcher", None, MachineProfile.BASE),
        ("a-bot", ArtifactType.CODE, MachineProfile.BASE),
    ],
)
def test_a_step_that_makes_no_code_keeps_its_own_image(
    agent: str, makes: ArtifactType | None, profile: MachineProfile
) -> None:
    assert machines.seat_profile(CLOUD, _step(agent, makes)) == profile


def test_on_the_device_the_persons_own_tools_serve_a_code_step() -> None:
    assert frame_profile_for("maker", device=True, makes=ArtifactType.CODE) != MachineProfile.CODE
    assert machines.seat_profile(DEVICE, StepSpec(agent=AgentName("writer"))) == MachineProfile.BASE


def test_a_code_step_starts_on_the_machine_not_on_paper() -> None:
    def on_paper(spec: StepSpec) -> bool:
        return machines.starts_on_paper(CLOUD, spec, seat=False, has_machine=False)

    assert on_paper(_step("maker", ArtifactType.DOCUMENT))
    assert not on_paper(_step("maker", ArtifactType.CODE))


def test_profiles_only_widen_along_the_line() -> None:
    base, code, browser = MachineProfile.BASE, MachineProfile.CODE, MachineProfile.BROWSER
    assert widen(base, code) == widen(code, base) == code
    assert widen(code, browser) == widen(browser, code) == browser
    assert widen(base, base) == base


def test_the_code_skill_asks_for_the_code_image_and_the_choice_round_trips() -> None:
    parsed = parse_dir(CODE_SKILL)
    assert parsed.manifest.frame_profile is MachineProfile.CODE
    again = parse("code", render(parsed.manifest, parsed.body), (CODE_SKILL / "cases.yaml").read_text())
    assert again.manifest.frame_profile is MachineProfile.CODE
    assert again.manifest.metadata == parsed.manifest.metadata


def test_a_frame_profile_that_is_not_one_is_refused() -> None:
    skill_md = (CODE_SKILL / "SKILL.md").read_text().replace("frame-profile: code", "frame-profile: gpu")
    with pytest.raises(SkillError, match="frame-profile"):
        parse("code", skill_md, (CODE_SKILL / "cases.yaml").read_text())


def test_a_code_artifact_is_verified_on_the_image_it_was_built_on() -> None:
    assert deliverables.verify_profile(VerifierSuite.CODE) is MachineProfile.CODE
    assert deliverables.verify_profile(VerifierSuite.DOCUMENT) is MachineProfile.BASE
    assert deliverables.verify_profile(VerifierSuite.DATASET) is MachineProfile.BASE
