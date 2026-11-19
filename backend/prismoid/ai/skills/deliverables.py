# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deliverables: which built-in format skill builds which file, read from the skills' own manifests.

A file's craft lives in its format skill, and the skill says what it builds (`produces`, and for a
document the `format`, its extension). This module is the one table those declarations make: the
kinds a maker can deliver, the skill a step's file takes, the model class that skill's work wants, and
the agent a dialed kind runs on (`kind_agent`).
Only the built-in skills on disk feed it, so a skill someone pushes never changes what a step runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, Protocol

from prismoid.ai.skills.format import parse_dir
from prismoid.schemas.ids import SkillName
from prismoid.schemas.machine_profile import MachineProfile
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ARTIFACT_TYPES, ArtifactType, VerifierSuite

# parents[3] = backend/: this file is backend/prismoid/ai/skills/deliverables.py, and the built-ins ride beside the package.
BUILTIN_DIR: Final = Path(__file__).resolve().parents[3] / "skills"


@cache
def builtin_craft(name: str) -> str:
    """The body of the built-in skill `name`, read from disk: a pushed skill never changes what a step runs."""
    return parse_dir(BUILTIN_DIR / name).body


# A kind whose file the system writes from the maker's typed answer, by that answer's output name.
TYPED: Final[dict[ArtifactType, str]] = {ArtifactType.COLLECTION: "collection_result"}


@dataclass(frozen=True, slots=True)
class Format:
    """One format skill: the file it builds and the model class its work wants."""

    skill: SkillName
    kind: ArtifactType
    extension: str  # a document's file format (`docx`, `pdf`, `html`); empty for every other kind
    model_class: ModelClass
    frame_profile: MachineProfile | None  # the image its step boots; None keeps the agent's own


@cache
def formats() -> tuple[Format, ...]:
    """Every built-in skill that declares the file it builds, by skill name."""
    found: list[Format] = []
    for child in sorted(BUILTIN_DIR.iterdir()):
        if not (child / "SKILL.md").is_file():
            continue
        manifest = parse_dir(child).manifest
        if manifest.produces is None:
            continue
        model_class = ModelClass(manifest.capability or ModelClass.FAST_EXECUTOR)
        found.append(
            Format(manifest.name, manifest.produces, manifest.format, model_class, manifest.frame_profile)
        )
    return tuple(found)


def kinds() -> tuple[ArtifactType, ...]:
    """The kinds the format skills build, in the registry's order."""
    made = {f.kind for f in formats()}
    return tuple(kind for kind in ARTIFACT_TYPES if kind in made)


def of_skill(name: str) -> Format | None:
    """The format a skill builds, or None for a skill that builds no file."""
    return next((f for f in formats() if f.skill == name), None)


def format_for(kind: ArtifactType, extension: str | None = None) -> Format | None:
    """The skill that builds `kind` in `extension`. A kind with several formats and no extension that names one
    takes the first the registry lists for it (a document is then a `.docx`); None when no skill builds the kind."""
    built = [f for f in formats() if f.kind is kind]
    named = next((f for f in built if extension and f.extension == extension), None)
    if named is not None or len(built) < 2:
        return named or next(iter(built), None)
    order = [ext.lstrip(".") for ext in ARTIFACT_TYPES[kind].extensions]
    return min(built, key=lambda f: order.index(f.extension) if f.extension in order else len(order))


def document_formats() -> dict[str, SkillName]:
    """A document's file formats, each with the skill that builds it."""
    return {f.extension: f.skill for f in formats() if f.kind is ArtifactType.DOCUMENT and f.extension}


def verify_profile(suite: VerifierSuite) -> MachineProfile:
    """The image a fresh frame running `suite` boots: the widest its kinds' format skills ask for, else base,
    so a re-run sees the tools the maker built with."""
    kinds = {kind for kind, spec in ARTIFACT_TYPES.items() if spec.verifier_suite is suite}
    asked = [f.frame_profile for f in formats() if f.kind in kinds and f.frame_profile is not None]
    return max(asked, key=list(MachineProfile).index, default=MachineProfile.BASE)


# Who a prose document lands on. Explicit because a kind can have several producers: writer and
# researcher both publish `document`, and a dialed memo once hung for minutes in a researcher's ladder.
PROSE_AGENT: Final = "writer"


class Producer(Protocol):
    """What `kind_agent` reads of an agent: a built-in's definition or its seat."""

    @property
    def name(self) -> str: ...
    @property
    def kinds(self) -> tuple[ArtifactType, ...]: ...
    @property
    def makes_formats(self) -> bool: ...
    @property
    def publishes(self) -> bool: ...
    @property
    def delivers_outcome(self) -> bool: ...


def kind_agent(kind: ArtifactType, able: Sequence[Producer], doc_format: str | None = None) -> str:
    """The agent a kind runs on: the writer for a prose document, the format maker for a file a format skill
    builds (a document in one of their formats included), else the first producer."""
    makers = [a for a in able if a.publishes and not a.delivers_outcome and kind in a.kinds]
    prose = kind is ArtifactType.DOCUMENT and doc_format not in document_formats()
    chosen = next(
        (a for a in makers if (str(a.name) == PROSE_AGENT if prose else a.makes_formats)), makers[0]
    )
    return str(chosen.name)
