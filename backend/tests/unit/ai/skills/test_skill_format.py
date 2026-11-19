# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The skill file format and the write-time doctrine.

`format.parse` is the one door every skill comes through - the seeder loading a builtin and the
author pushing a draft both call it - so the doctrine has to be enforced *here*, once, or it is
enforced nowhere. These tests pin each rule to a failing input, and then pin every shipped
builtin to *passing* - a doctrine no real skill can satisfy is a doctrine that gets deleted.

Two directions, as always: a skill that breaks a rule must raise, and a skill that keeps every rule
must parse. The first stops a liability reaching the registry; the second stops the rules being so
strict that the builtins themselves cannot be seeded.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
import yaml

from prismoid.ai.skills import format as fmt
from prismoid.operations.skills import BUILTIN_DIR

# A minimal skill that satisfies every rule - each test mutates one thing away from valid, so the
# failure it asserts is unambiguously the rule under test and not a second defect.
_GOOD_MD = """---
description: Load when the user wants a written memo or brief - prose a busy reader acts on. Not for spreadsheets, decks, or code.
capability: fast-executor
---
You write prose a busy reader finishes. Lead with the answer; support it; stop.
"""

_GOOD_CASES = """routing:
  - goal: write a board update on Q3
    expect: report
"""


def test_valid_skill_parses() -> None:
    parsed = fmt.parse("report", _GOOD_MD, _GOOD_CASES)
    assert str(parsed.manifest.name) == "report"
    assert "Load when" in parsed.manifest.description
    assert parsed.body.startswith("You write prose")
    assert len(parsed.routing_cases) == 1
    assert str(parsed.routing_cases[0].expect) == "report"


def test_missing_frontmatter_is_rejected() -> None:
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", "no frontmatter here, just a body", _GOOD_CASES)
    assert "body" in e.value.fields


def test_description_missing_load_when_is_rejected() -> None:
    md = _GOOD_MD.replace("Load when the user wants", "The user might want")
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", md, _GOOD_CASES)
    assert "Load when" in e.value.fields["description"]


def test_description_missing_not_for_is_rejected() -> None:
    md = _GOOD_MD.replace(" Not for spreadsheets, decks, or code.", "")
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", md, _GOOD_CASES)
    assert "Not for" in e.value.fields["description"]


def test_over_long_description_is_rejected() -> None:
    filler = " ".join(f"word{i}" for i in range(60))
    md = f"---\ndescription: Load when {filler}. Not for anything.\n---\nbody text here\n"
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", md, _GOOD_CASES)
    assert "words" in e.value.fields["description"]


def test_over_budget_body_is_rejected() -> None:
    huge = "lorem ipsum " * 3000  # ~9000 tokens, well over the 5000 budget
    md = _GOOD_MD.rstrip() + "\n" + huge
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", md, _GOOD_CASES)
    assert "body" in e.value.fields


def test_no_routing_cases_is_rejected() -> None:
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", _GOOD_MD, "")
    assert "cases" in e.value.fields


def test_empty_body_is_rejected() -> None:
    md = "---\ndescription: Load when the user wants X. Not for Y.\n---\n"
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", md, _GOOD_CASES)
    assert e.value.fields.get("body") == "empty"


# ── every shipped builtin must satisfy the doctrine ──────────────────────────────────────────────

_BUILTIN_DIRS = sorted(p for p in BUILTIN_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


def test_every_builtin_folder_is_a_skill() -> None:
    # The seeder skips a folder without SKILL.md silently; a builtin missing it would never ship.
    folders = sorted(p for p in BUILTIN_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    assert folders and folders == _BUILTIN_DIRS, [p.name for p in folders if p not in _BUILTIN_DIRS]


@pytest.mark.parametrize("skill_dir", _BUILTIN_DIRS, ids=lambda p: p.name)
def test_builtin_parses_clean(skill_dir: Path) -> None:
    parsed = fmt.parse_dir(skill_dir)
    assert str(parsed.manifest.name) == skill_dir.name  # the dir name IS the skill name
    assert parsed.routing_cases, f"{skill_dir.name} has no routing cases"
    assert parsed.body, f"{skill_dir.name} has an empty body"


# ── render is the inverse of parse: a pulled skill must round-trip ───────────────────────────────


def test_render_round_trips_through_parse() -> None:
    original = fmt.parse("report", _GOOD_MD, _GOOD_CASES)
    rebuilt = fmt.render(original.manifest, original.body)
    # `render` drops nothing a version stores: parsing its output back must recover the same manifest
    # and body (cases live in cases.yaml, not the frontmatter, so they are supplied again here).
    reparsed = fmt.parse("report", rebuilt, _GOOD_CASES)
    assert reparsed.manifest.description == original.manifest.description
    assert reparsed.manifest.capability == original.manifest.capability
    assert reparsed.body == original.body


@pytest.mark.parametrize("skill_dir", _BUILTIN_DIRS, ids=lambda p: p.name)
def test_builtin_bodies_round_trip(skill_dir: Path) -> None:
    original = fmt.parse_dir(skill_dir)
    reparsed = fmt.parse(skill_dir.name, fmt.render(original.manifest, original.body), _GOOD_CASES)
    assert reparsed.body == original.body
    assert reparsed.manifest.description == original.manifest.description


# Nine levels of `&a [*b, *b, ...]` load as shared references, then expand to 10^9 items the
# moment anything walks them (`str()` of a frontmatter field). The loader refuses the first alias.
_BOMB = "a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
    f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]\n" for i in range(1, 10)
)


def test_an_alias_bomb_in_the_frontmatter_is_refused_at_once() -> None:
    md = _GOOD_MD.replace("capability: fast-executor\n", "capability: fast-executor\n" + _BOMB)
    with pytest.raises(fmt.SkillError, match="frontmatter is not valid YAML"):
        fmt.parse("report", md, _GOOD_CASES)


def test_an_alias_bomb_in_cases_yaml_is_refused_at_once() -> None:
    with pytest.raises(fmt.SkillError, match="cases is not valid YAML"):
        fmt.parse("report", _GOOD_MD, _GOOD_CASES + _BOMB)


# ── the Agent Skills spec: what Prismoid exports validates anywhere, what it imports it keeps ─────────

# The spec's validator (agentskills.io/specification, the `skills-ref validate` rules), vendored so
# the check needs no new dependency: the allowed frontmatter fields and each field's constraints.
_SPEC_FIELDS = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})


def _spec_problems(directory: str, skill_md: str) -> list[str]:
    """Every way `skill_md`, in a directory named `directory`, breaks the Agent Skills spec."""
    if not skill_md.startswith("---\n") or "\n---\n" not in skill_md[4:]:
        return ["SKILL.md must start with a YAML frontmatter block"]
    front = yaml.safe_load(skill_md[4:].split("\n---\n", 1)[0])
    if not isinstance(front, dict):
        return ["frontmatter must be a mapping"]
    problems = [f"unexpected field {key!r}" for key in front if key not in _SPEC_FIELDS]
    name = front.get("name")
    if not isinstance(name, str) or not name:
        problems.append("name is required")
    else:
        if len(name) > 64:
            problems.append("name is over 64 characters")
        if name != name.lower() or not all(c.isalnum() or c == "-" for c in name):
            problems.append("name may hold only lowercase letters, digits and hyphens")
        if name.startswith("-") or name.endswith("-") or "--" in name:
            problems.append("name may not start or end with a hyphen or hold '--'")
        if unicodedata.normalize("NFKC", name) != unicodedata.normalize("NFKC", directory):
            problems.append(f"name {name!r} must match its directory {directory!r}")
    description = front.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("description is required")
    elif len(description) > 1024:
        problems.append("description is over 1024 characters")
    compatibility = front.get("compatibility")
    if compatibility is not None and (not isinstance(compatibility, str) or len(compatibility) > 500):
        problems.append("compatibility must be a string of at most 500 characters")
    for field in ("license", "allowed-tools"):
        if field in front and not isinstance(front[field], str):
            problems.append(f"{field} must be a string")
    metadata = front.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())
    ):
        problems.append("metadata must map strings to strings")
    return problems


@pytest.mark.parametrize("skill_dir", _BUILTIN_DIRS, ids=lambda p: p.name)
def test_an_exported_builtin_validates_to_the_spec(skill_dir: Path) -> None:
    parsed = fmt.parse_dir(skill_dir)
    assert _spec_problems(skill_dir.name, fmt.render(parsed.manifest, parsed.body)) == []


_SAMPLE_MD = """---
name: pdf-tools
description: Comprehensive PDF manipulation toolkit for extracting text and tables, creating new PDFs, merging/splitting documents, and handling forms. When Claude needs to fill in a PDF form or programmatically process, generate, or analyze PDF documents at scale.
license: Proprietary. LICENSE.txt has complete terms
compatibility: Requires python3 with pypdf
metadata:
  author: example-org
  version: "1.0"
---
# PDF tools

Use pypdf to read and merge; see references/forms.md for filling forms.
"""


def test_an_imported_spec_skill_round_trips_losslessly(tmp_path: Path) -> None:
    skill_dir = tmp_path / "pdf-tools"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SAMPLE_MD)
    (skill_dir / "LICENSE.txt").write_text("All rights reserved.\n")
    (skill_dir / "references" / "forms.md").write_text("Fill fields by name.\n")

    parsed = fmt.parse_dir(skill_dir, strict=False)
    exported = fmt.render(parsed.manifest, parsed.body)

    assert _front(exported) == _front(_SAMPLE_MD), "every frontmatter field comes back as it arrived"
    assert _spec_problems("pdf-tools", exported) == []
    assert {f.path for f in parsed.files} == {"LICENSE.txt", "references/forms.md"}
    again = fmt.parse("pdf-tools", exported, strict=False)
    assert again.manifest == parsed.manifest and again.body == parsed.body


def test_prismoids_own_keys_ride_in_metadata_and_come_back() -> None:
    original = fmt.parse(
        "report", _GOOD_MD.replace("---\n", "---\nindex: Memos and briefs\n", 1), _GOOD_CASES
    )
    exported = fmt.render(original.manifest, original.body)
    assert _front(exported)["metadata"] == {
        "prismoid-capability": "fast-executor",
        "prismoid-index": "Memos and briefs",
    }
    assert fmt.parse("report", exported, _GOOD_CASES).manifest == original.manifest


def test_a_frontmatter_name_that_is_not_its_directory_is_refused() -> None:
    with pytest.raises(fmt.SkillError) as e:
        fmt.parse("report", _GOOD_MD.replace("---\n", "---\nname: memo\n", 1), _GOOD_CASES)
    assert "name" in e.value.fields


def _front(skill_md: str) -> dict[str, object]:
    return yaml.safe_load(skill_md[4:].split("\n---\n", 1)[0])


def test_a_root_license_name_outside_the_spec_is_ignored_not_refused(tmp_path: Path) -> None:
    skill_dir = tmp_path / "pdf-tools"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_SAMPLE_MD)
    (skill_dir / "LICENSE").write_text("MIT\n")
    (skill_dir / "LICENSE-APACHE").write_text("Apache-2.0\n")

    parsed = fmt.parse_dir(skill_dir, strict=False)

    assert {f.path for f in parsed.files} == {"LICENSE"}
