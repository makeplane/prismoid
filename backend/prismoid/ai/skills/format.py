# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The skill file format, and its validator.

A skill on disk is a directory: `SKILL.md` (YAML frontmatter + a markdown body of judgment) and
`cases.yaml` (its routing evals). This module turns that directory - or the equivalent bytes coming
off an HTTP push - into a `ParsedSkill`, and enforces the write-time doctrine while it does.

**The doctrine is enforced here, in one place, so every door obeys it.** The seeder loading builtins
and the author pushing a draft both come through `parse`, so a body over budget or a description
missing its "Load when" is a `SkillError` no matter which way it arrived - never a rule that holds on
one path and not the other.

The token budgets are counted by the one estimate (`schemas.tokens.TOKENS`): guard-rails against a
runaway body, not billing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast

import yaml

from prismoid.schemas.ids import SkillName, skill_name
from prismoid.schemas.machine_profile import MachineProfile
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.skills import (
    MAX_BODY_TOKENS,
    MAX_DESCRIPTION_WORDS,
    MAX_INDEX_TOKENS,
    MAX_SKILL_BUNDLE_BYTES,
    MAX_SKILL_FILES,
    GoldenSkillCase,
    ParsedSkill,
    RoutingCase,
    SkillFileData,
    SkillManifest,
)
from prismoid.schemas.tokens import TOKENS
from prismoid.schemas.yaml_doc import load_untrusted

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)

# The bundled-file path law (the open Agent Skills shape): three fixed subdirs, safe
# names, no traversal, plus a root licence file. Enforced HERE - the one door - so seed and HTTP
# push obey identically.
_FILE_PATH = re.compile(r"(scripts|references|assets)/[A-Za-z0-9][A-Za-z0-9._/-]*|LICENSE(\.[A-Za-z]+)?")
SKILL_FILE_DIRS = ("scripts", "references", "assets")
_LICENSE_GLOB = "LICENSE*"

# Prismoid's own frontmatter keys ride in the spec's `metadata` map on export; the top level is the
# authoring form.
_PRISMOID_METADATA = {
    "capability": "prismoid-capability",
    "index": "prismoid-index",
    "produces": "prismoid-produces",
    "format": "prismoid-format",
    "frame-profile": "prismoid-frame-profile",
}


# The Agent Skills spec's own description ceiling - what the lenient (import) door checks.
SPEC_DESCRIPTION_MAX = 1024
SPEC_COMPATIBILITY_MAX = 500


class SkillError(Exception):
    """A skill that breaks the write-time doctrine. Carries `fields` so the API can 422 with
    the exact rule broken - "invalid skill" is not a message an author can act on."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


def parse(
    name: str,
    skill_md: str,
    cases_yaml: str = "",
    files: Sequence[tuple[str, bytes]] = (),
    *,
    strict: bool = True,
) -> ParsedSkill:
    """`SKILL.md` bytes + `cases.yaml` bytes → a validated `ParsedSkill`, or `SkillError`.

    The frontmatter carries `description` (and optionally `index`, `capability`); everything after it
    is the body. The doctrine is checked here - description in the user's vocabulary with a
    "Load when…" and a "Not for…", index within budget, body within budget, at least one routing case
    - because a skill that a router cannot route on, or a body that will not fit a prompt, is not a
    skill, it is a liability that passes review and fails in production.
    """
    checked = _checked_name(name)
    match = _FRONTMATTER.match(skill_md)
    if not match:
        raise SkillError(
            f"{name}: SKILL.md must start with a `---` YAML frontmatter block",
            {"body": "missing frontmatter"},
        )
    front = _yaml(name, "frontmatter", match.group(1))
    if not isinstance(front, dict):
        raise SkillError(f"{name}: the frontmatter must be a mapping", {"frontmatter": "not a mapping"})
    front = cast("dict[str, Any]", front)
    body = match.group(2).strip()

    description = str(front.get("description", "")).strip()
    if strict:
        check_doctrine(name, description)
    elif not description or len(description) > SPEC_DESCRIPTION_MAX:
        # The import door validates to the Agent Skills SPEC, not the house
        # doctrine: 1–1024 chars, no "Load when/Not for" demand, unknown frontmatter keys
        # (license, compatibility, metadata, allowed-tools) tolerated. The doctrine has not
        # moved - `check_doctrine` holds it at PUBLISH (`operations/skills.py`), so an
        # imported skill lands as a draft an author then sharpens. Parse to the spec, gate
        # on publish: no rule weakens, drafts merely exist.
        raise SkillError(
            f"{name}: description must be 1–{SPEC_DESCRIPTION_MAX} characters (Agent Skills spec)",
            {"description": "missing or over the spec limit"},
        )

    if not body:
        raise SkillError(f"{name}: the body is empty - a skill is judgment, not a title", {"body": "empty"})
    body_tokens = TOKENS.count(body)
    if body_tokens > MAX_BODY_TOKENS:
        raise SkillError(
            f"{name}: body is ~{body_tokens} tokens, over the {MAX_BODY_TOKENS} limit - move heavy "
            "reference to references/ and keep the body judgment",
            {"body": f"~{body_tokens} tokens > {MAX_BODY_TOKENS}"},
        )

    cases = _parse_cases(name, cases_yaml)
    golden = _parse_golden(name, cases_yaml)
    if not cases and strict:
        raise SkillError(
            f"{name}: cases.yaml must have at least one routing case - a skill nothing tests is a "
            "skill nothing can trust",
            {"cases": "missing or empty"},
        )

    bundled = _check_files(name, files)

    manifest = _manifest(checked, front, description)
    return ParsedSkill(manifest=manifest, body=body, routing_cases=cases, golden_cases=golden, files=bundled)


def _manifest(name: SkillName, front: dict[str, Any], description: str) -> SkillManifest:
    """The manifest a frontmatter declares: the spec's fields kept, Prismoid's own keys read from
    `metadata` (the export form) or the top level (the authoring form)."""
    declared = str(front.get("name", name))
    if declared != name:
        raise SkillError(
            f"{name}: frontmatter name {declared!r} must match its directory", {"name": "mismatch"}
        )
    raw_metadata: object = front.get("metadata") or {}
    if not isinstance(raw_metadata, dict):
        raise SkillError(
            f"{name}: metadata must be a mapping (Agent Skills spec)", {"metadata": "not a mapping"}
        )
    metadata = {str(k): str(v) for k, v in cast("dict[object, object]", raw_metadata).items()}
    prismoid = {
        key: metadata.pop(stored, None) or str(front.get(key, "")).strip()
        for key, stored in _PRISMOID_METADATA.items()
    }
    compatibility = str(front.get("compatibility", "")).strip()
    if len(compatibility) > SPEC_COMPATIBILITY_MAX:
        raise SkillError(
            f"{name}: compatibility is over {SPEC_COMPATIBILITY_MAX} characters (Agent Skills spec)",
            {"compatibility": "over the spec limit"},
        )
    index = prismoid["index"] or _first_sentence(description)
    if TOKENS.count(index) > MAX_INDEX_TOKENS:
        raise SkillError(f"{name}: index line is over {MAX_INDEX_TOKENS} tokens", {"index": "too long"})
    return SkillManifest(
        name=name,
        description=description[:597] + "…" if len(description) > 600 else description,
        index=index,
        capability=prismoid["capability"],
        produces=_produces(name, prismoid["produces"]),
        format=prismoid["format"],
        frame_profile=_frame_profile(name, prismoid["frame-profile"]),
        license=str(front.get("license", "")).strip(),
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=tuple(str(front.get("allowed-tools") or "").split()),
    )


def _produces(name: str, declared: str) -> ArtifactType | None:
    """The artifact kind a format skill declares it builds, or None for a skill that builds no file."""
    if not declared:
        return None
    try:
        return ArtifactType(declared)
    except ValueError as exc:
        raise SkillError(
            f"{name}: produces {declared!r} is not an artifact kind", {"produces": declared}
        ) from exc


def _frame_profile(name: str, declared: str) -> MachineProfile | None:
    """The image a format skill's step boots, or None to keep the agent's own."""
    if not declared:
        return None
    try:
        return MachineProfile(declared)
    except ValueError as exc:
        raise SkillError(
            f"{name}: frame-profile {declared!r} is not a frame profile", {"frame-profile": declared}
        ) from exc


def _check_files(name: str, files: Sequence[tuple[str, bytes]]) -> list[SkillFileData]:
    """The path law and the bundle budget - a skill's files are references and
    tools, not a filesystem: three fixed subdirs, no `..`, a hard byte ceiling."""
    if len(files) > MAX_SKILL_FILES:
        raise SkillError(
            f"{name}: {len(files)} bundled files is over the {MAX_SKILL_FILES} limit",
            {"files": f"{len(files)} > {MAX_SKILL_FILES}"},
        )
    total = 0
    bundled: list[SkillFileData] = []
    for path, data in files:
        _check_path(name, path)
        total += len(data)
        bundled.append(SkillFileData(path=path, data=data))
    if total > MAX_SKILL_BUNDLE_BYTES:
        raise SkillError(
            f"{name}: bundled files total {total:,} bytes, over the {MAX_SKILL_BUNDLE_BYTES:,} "
            "budget - a skill carries references, not datasets",
            {"files": f"{total} bytes > {MAX_SKILL_BUNDLE_BYTES}"},
        )
    return bundled


def check_stored(name: str, paths: Iterable[str]) -> SkillName:
    """A stored skill's name and bundled paths under today's rules, before either becomes a path: a row
    written before the rules is refused, never mounted or exported."""
    checked = _checked_name(name)
    for path in paths:
        _check_path(checked, path)
    return checked


def _checked_name(name: str) -> SkillName:
    """`name` as a `SkillName`, or a `SkillError` on the name field."""
    try:
        return skill_name(name)
    except ValueError as exc:
        raise SkillError(str(exc), {"name": "not a legal skill name"}) from exc


def _check_path(name: str, path: str) -> None:
    """The bundled-file path law: under scripts/, references/ or assets/, safe characters, no `..`;
    or the root licence file."""
    if ".." in path.split("/") or not _FILE_PATH.fullmatch(path):
        raise SkillError(
            f"{name}: bundled file {path!r} must live under scripts/, references/, or assets/ "
            "with a safe relative name, or be a root LICENSE file",
            {"files": path},
        )


def parse_dir(directory: Path, *, strict: bool = True) -> ParsedSkill:
    """Read `<dir>/SKILL.md`, `<dir>/cases.yaml`, a root `LICENSE*` and any bundled `scripts/ references/
    assets/` files. The seeder's door (strict) and the plugin import's (`strict=False`, the spec's rules)."""
    name = directory.name
    skill_md = (directory / "SKILL.md").read_text()
    cases_path = directory / "cases.yaml"
    cases_yaml = cases_path.read_text() if cases_path.exists() else ""
    licenses = sorted(directory.glob(_LICENSE_GLOB))
    files = [(p.name, p.read_bytes()) for p in licenses if p.is_file() and _FILE_PATH.fullmatch(p.name)]
    for sub in SKILL_FILE_DIRS:
        root = directory / sub
        if not root.is_dir():
            continue
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            # Compiled droppings are not skill files: anything that imports or byte-compiles a
            # bundled script leaves `__pycache__` beside it, and a seed that then refuses the
            # whole skill (the path law would, rightly) turns a local test run into a broken
            # `just migrate`. Skipped HERE, at the directory reader - the path law itself stays
            # exact, so a pyc pushed explicitly over HTTP is still refused.
            if "__pycache__" in file.parts:
                continue
            files.append((file.relative_to(directory).as_posix(), file.read_bytes()))
    return parse(name, skill_md, cases_yaml, files=files, strict=strict)


def render(manifest: SkillManifest, body: str) -> str:
    """A manifest + body → the `SKILL.md` bytes, the inverse of `parse`, with the spec's frontmatter
    (Prismoid's keys inside `metadata`); `yaml.safe_dump` quotes the colons a description carries."""
    metadata = dict(manifest.metadata)
    if manifest.capability:
        metadata[_PRISMOID_METADATA["capability"]] = manifest.capability
    if manifest.index and manifest.index != _first_sentence(manifest.description):
        metadata[_PRISMOID_METADATA["index"]] = manifest.index
    if manifest.produces is not None:
        metadata[_PRISMOID_METADATA["produces"]] = manifest.produces.value
    if manifest.format:
        metadata[_PRISMOID_METADATA["format"]] = manifest.format
    if manifest.frame_profile is not None:
        metadata[_PRISMOID_METADATA["frame-profile"]] = manifest.frame_profile.value
    front: dict[str, object] = {"name": str(manifest.name), "description": manifest.description}
    if manifest.license:
        front["license"] = manifest.license
    if manifest.compatibility:
        front["compatibility"] = manifest.compatibility
    if manifest.allowed_tools:
        front["allowed-tools"] = " ".join(manifest.allowed_tools)
    if metadata:
        front["metadata"] = metadata
    front_yaml = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000).strip()
    return f"---\n{front_yaml}\n---\n{body.strip()}\n"


def render_cases(cases: list[RoutingCase], golden: list[GoldenSkillCase] | None = None) -> str:
    """RoutingCases → `cases.yaml` bytes - the inverse of `_parse_cases`, so a pulled skill's routing
    cases round-trip. `safe_dump`, not hand-formatting, for the same colon reason `render`
    has: a goal is free text and may contain a `:` that an unquoted scalar would misread."""
    data: dict[str, object] = {"routing": [{"goal": c.goal, "expect": str(c.expect)} for c in cases]}
    if golden:
        data["golden"] = [g.model_dump(mode="json") for g in golden]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


def check_doctrine(name: str, description: str) -> None:
    """The house description rules: checked at parse for authored skills, at publish for imported
    drafts."""
    if not description:
        raise SkillError(
            f"{name}: a description is the routing trigger; it cannot be blank", {"description": "required"}
        )
    words = len(description.split())
    if words > MAX_DESCRIPTION_WORDS:
        raise SkillError(
            f"{name}: description is {words} words, over the {MAX_DESCRIPTION_WORDS} limit - a router "
            "reads only this, so it must be tight",
            {"description": f"{words} words > {MAX_DESCRIPTION_WORDS}"},
        )
    lowered = description.lower()
    if "load when" not in lowered:
        raise SkillError(
            f"{name}: description needs a 'Load when…' trigger - that is what a router matches on",
            {"description": "missing 'Load when'"},
        )
    if "not for" not in lowered:
        raise SkillError(
            f"{name}: description needs at least one 'Not for…' - the boundary is what stops a skill "
            "grabbing work that is not its own",
            {"description": "missing 'Not for'"},
        )


def _first_sentence(text: str) -> str:
    return text.split(".")[0].strip()[:400]


def _yaml(name: str, field: str, text: str) -> Any:
    """A document's YAML, or a SkillError naming the field: bad YAML and any alias (an alias bomb) are an
    author's mistake to fix, never a 500."""
    try:
        return load_untrusted(text) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{name}: {field} is not valid YAML", {field: str(exc)[:200]}) from exc


def _parse_golden(name: str, cases_yaml: str) -> list[GoldenSkillCase]:
    """The optional `golden:` list in cases.yaml - advisory end-to-end cases."""
    if not cases_yaml.strip():
        return []
    data = _yaml(name, "cases", cases_yaml)
    raw: list[Any] = cast("dict[str, Any]", data).get("golden", []) if isinstance(data, dict) else []
    golden: list[GoldenSkillCase] = []
    for entry in raw:
        if not isinstance(entry, dict) or "goal" not in entry:
            continue
        entry = cast("dict[str, Any]", entry)
        golden.append(
            GoldenSkillCase(
                goal=str(entry["goal"]),
                inputs=[str(i) for i in entry.get("inputs", [])],
                must_contain=[str(m) for m in entry.get("must_contain", [])],
            )
        )
    return golden


def _parse_cases(name: str, cases_yaml: str) -> list[RoutingCase]:
    if not cases_yaml.strip():
        return []
    data = _yaml(name, "cases", cases_yaml)
    raw: list[Any] = cast("dict[str, Any]", data).get("routing", []) if isinstance(data, dict) else []
    cases: list[RoutingCase] = []
    for entry in raw:
        if not isinstance(entry, dict) or "goal" not in entry:
            continue
        entry = cast("dict[str, Any]", entry)
        cases.append(RoutingCase(goal=str(entry["goal"]), expect=SkillName(str(entry.get("expect", name)))))
    return cases
