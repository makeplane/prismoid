# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skill shapes - the manifest a registry row carries, and the parsed form of a skill directory.

A skill is **data**: a routing description, a short index line, and a markdown body of *judgment*.
The body is stored in MinIO by content hash; the manifest - everything the Lead needs to
decide whether to load the body - lives in the row, so choosing a skill never costs a fetch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.ids import SkillName
from prismoid.schemas.machine_profile import MachineProfile
from prismoid.schemas.outputs import ArtifactType


class SkillManifest(BaseModel):
    """What the registry row stores about a skill version, the routing surface: the Lead reads only
    `description` to choose it, and `index` is its one-line entry in a menu of every skill."""

    model_config = ConfigDict(frozen=True)

    name: SkillName
    description: str = Field(min_length=1, max_length=600)
    index: str = Field(default="", max_length=400)  # the one-liner in the skill menu
    capability: str = ""  # the ModelClass this skill's work tends to want (a hint, not a lock)
    body_sha256: str = ""
    # The Agent Skills spec's optional fields, kept so an imported skill exports as it arrived.
    license: str = ""
    compatibility: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    # The spec's `allowed-tools`: the tools the skill's work needs. It grants nothing; a step whose agent
    # lacks one is not given the skill.
    allowed_tools: tuple[str, ...] = ()
    # A format skill's file: the kind it builds, and for a document the extension (`docx`, `pdf`, `html`).
    # A maker step whose deliverable is that file runs with this skill (`ai/skills/deliverables.py`).
    produces: ArtifactType | None = None
    format: str = ""
    # The image a step running this format skill boots (`code`: git, ripgrep and node); None keeps the agent's own.
    frame_profile: MachineProfile | None = None


class SkillFile(BaseModel):
    """One bundled file's manifest entry: the open Agent Skills shape - a skill may
    carry `scripts/`, `references/`, and `assets/` beside its SKILL.md. The row stores this
    pointer; the bytes live in MinIO by their own hash, exactly like the body."""

    model_config = ConfigDict(frozen=True)

    path: str  # relative, under scripts/ references/ assets/ - the law is enforced in format.parse
    sha256: str
    size: int


class SkillFileData(BaseModel):
    """A bundled file WITH its bytes - the parsed/resolved form (never stored as-is)."""

    model_config = ConfigDict(frozen=True)

    path: str
    data: bytes


class ParsedSkill(BaseModel):
    """A skill directory read off disk (`ai/skills/format`): the manifest, the body, the cases,
    and any bundled files.

    Not persisted as-is - the body and files go to MinIO and the manifest to the row - but it is
    the value the seeder and the push path both produce, so the *validation* of a skill (the
    write-time rules, plus the file path law) happens in one place against one shape.
    """

    model_config = ConfigDict(frozen=True)

    manifest: SkillManifest
    body: str
    routing_cases: list[RoutingCase] = Field(default_factory=lambda: list[RoutingCase]())
    golden_cases: list[GoldenSkillCase] = Field(default_factory=lambda: list[GoldenSkillCase]())
    files: list[SkillFileData] = Field(default_factory=list[SkillFileData])


class RoutingCase(BaseModel):
    """One routing eval: a goal, and the skill it should load. `just evals --suite
    routing` aggregates every skill's cases into one precision/recall number."""

    model_config = ConfigDict(frozen=True)

    goal: str
    expect: SkillName  # the skill this goal should route to


class GoldenSkillCase(BaseModel):
    """One end-to-end golden a skill ships with: a real task run on the ADVISORY
    nightly lane (`just evals --suite skill-golden`), never the 409 publish gate - gating publish
    on multi-minute real-money runs would kill the sub-minute edit loop, and that is recorded so
    nobody "fixes" it."""

    model_config = ConfigDict(frozen=True)

    goal: str
    inputs: list[str] = Field(default_factory=list)  # fixture paths, relative to backend/
    must_contain: list[str] = Field(default_factory=list)


class ResolvedSkill(BaseModel):
    """A skill pin turned into the bytes an executor copies into a frame: the concrete
    version, its manifest, its body, and its routing cases. The registry row named the version; the
    loader fetched the body by hash. This is the value that crosses back into `operations` - a body,
    not a query - and the value `pull` reconstructs a whole skill directory from."""

    model_config = ConfigDict(frozen=True)

    name: SkillName
    version: int
    manifest: SkillManifest
    body: str
    routing_cases: list[RoutingCase] = Field(default_factory=list[RoutingCase])
    golden_cases: list[GoldenSkillCase] = Field(
        default_factory=list[GoldenSkillCase]
    )  # for pull round-trip (M5)
    files: list[SkillFileData] = Field(default_factory=list[SkillFileData])  # bundled, bytes included (M5)


class RoutingCaseResult(BaseModel):
    """One routing case, run: what it expected, the skill the Lead loaded for it, and whether they matched."""

    model_config = ConfigDict(frozen=True)

    goal: str
    expected: str
    chosen: str
    ok: bool


class RoutingReport(BaseModel):
    """A skill's routing cases, run on the Lead, decision-only. `test` prints it; `push`
    stores the pass/fail as the version's `eval_status`; `publish` refuses a red one."""

    model_config = ConfigDict(frozen=True)

    skill: str
    version: int
    passed: int
    total: int
    cases: list[RoutingCaseResult] = Field(default_factory=list[RoutingCaseResult])

    @property
    def green(self) -> bool:
        """All cases passed, and there was at least one - a skill nothing tests is not green."""
        return self.total > 0 and self.passed == self.total


# Doctrine limits, enforced at write time. Named so a 422 can say which one was broken.
MAX_DESCRIPTION_WORDS = 50
MAX_INDEX_TOKENS = 100
MAX_BODY_TOKENS = 5000

# Bundled-file limits. The body budget stays what it was - judgment stays small;
# heft (a JSON schema, a validate script, a template) moves to files loaded on demand.
MAX_SKILL_FILES = 20
MAX_SKILL_BUNDLE_BYTES = 262_144  # 256 KiB across every bundled file
