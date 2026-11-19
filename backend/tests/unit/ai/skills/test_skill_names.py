# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A skill's name becomes a directory in a Machine and a folder in an export, so it is a grammar,
not free text: every door a name enters by refuses what the Agent Skills spec does not allow."""

from __future__ import annotations

from pathlib import Path

import pytest

from prismoid.ai.plugins import format as plugin_format
from prismoid.ai.skills import format as skill_format
from prismoid.schemas.ids import skill_name

_HOSTILE = [
    "x;id",
    "$(id)",
    "`id`",
    "a b",
    "../x",
    "a/b",
    "-a",
    "a-",
    "a--b",
    "Report",
    "under_score",
    "tab\tname",
    "line\n",
    "",
    "a" * 65,
]

_SKILL_MD = "---\ndescription: Load when a test needs a skill. Not for anything else.\n---\nBody.\n"
_CASES = "routing:\n  - goal: a goal\n    expect: report\n"


@pytest.mark.parametrize("name", ["report", "a", "q3-board-update", "x1", "a" * 64])
def test_a_spec_legal_name_is_a_skill_name(name: str) -> None:
    assert skill_name(name) == name


@pytest.mark.parametrize("name", _HOSTILE)
def test_the_name_door_refuses_what_the_spec_does_not_allow(name: str) -> None:
    with pytest.raises(ValueError, match="not a legal skill name"):
        skill_name(name)


@pytest.mark.parametrize("name", _HOSTILE)
@pytest.mark.parametrize("strict", [True, False])
def test_publishing_or_importing_a_hostile_name_is_a_skill_error(name: str, strict: bool) -> None:
    with pytest.raises(skill_format.SkillError) as refused:
        skill_format.parse(name, _SKILL_MD, _CASES, strict=strict)
    assert "name" in refused.value.fields


def test_a_plugin_skill_folder_with_a_hostile_name_is_skipped_with_a_note(tmp_path: Path) -> None:
    plugin = tmp_path / "demo-plugin"
    for folder in ("clause-notes", "x;id"):
        (plugin / "skills" / folder).mkdir(parents=True)
        (plugin / "skills" / folder / "SKILL.md").write_text(_SKILL_MD)
    (plugin / "plugin.json").write_text(
        '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": "demo-plugin"}'
    )

    layout = plugin_format.discover(plugin)

    assert layout.skill_dirs == ("skills/clause-notes",)
    assert any("x;id" in note and "skipped" in note for note in layout.notes)


_HOSTILE_PATHS = [
    "scripts/run.py\n",
    "scripts/../../etc/cron.d/x",
    "/etc/passwd",
    "scripts",
    "other/run.py",
    "scripts/$(id)",
    "scripts/deep dir/run.py",
]


@pytest.mark.parametrize("path", _HOSTILE_PATHS)
def test_a_bundled_file_outside_the_path_law_is_a_skill_error(path: str) -> None:
    with pytest.raises(skill_format.SkillError) as refused:
        skill_format.parse("report", _SKILL_MD, _CASES, files=[(path, b"x")])
    assert refused.value.fields == {"files": path}


@pytest.mark.parametrize("path", _HOSTILE_PATHS)
def test_a_stored_skill_with_a_hostile_bundled_path_is_refused(path: str) -> None:
    with pytest.raises(skill_format.SkillError):
        skill_format.check_stored("report", [path])


@pytest.mark.parametrize("name", _HOSTILE)
def test_a_stored_skill_with_a_hostile_name_is_refused(name: str) -> None:
    with pytest.raises(skill_format.SkillError, match="not a legal skill name"):
        skill_format.check_stored(name, [])


def test_a_stored_skill_within_the_rules_is_its_name() -> None:
    assert skill_format.check_stored("report", ["scripts/run.py", "references/a.b/c.md"]) == "report"
