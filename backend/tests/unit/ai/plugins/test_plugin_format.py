# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Agent Plugins v1 reader: the spec's conformance rules, held as tests.

Every rule here is a quoted behaviour from the specification - fixed locations only, the
failure boundaries, ignored namespaces - so a failure names the clause we stopped honouring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prismoid.ai.plugins import format as plugin_format
from prismoid.ai.plugins.format import PluginError
from prismoid.schemas.plugins import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID


def manifest(name: str = "demo-plugin", **extra: object) -> str:
    return json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": name, **extra})


def write_bundle(root: Path) -> Path:
    """A conformant bundle: one skill, one bot, two MCP servers (one HTTP, one stdio)."""
    plugin = root / "demo-plugin"
    (plugin / "skills" / "clause-notes").mkdir(parents=True)
    (plugin / "skills" / "clause-notes" / "SKILL.md").write_text(
        "---\nname: clause-notes\ndescription: Notes on contract clauses for reviewers.\n---\nBody.\n"
    )
    bot_dir = plugin / "so.plane.prismoid" / "bots" / "imported-reviewer"
    bot_dir.mkdir(parents=True)
    (bot_dir / "BOT.md").write_text(
        "---\nname: imported-reviewer\ndescription: 'Reviews things. Load when: a review is asked. "
        "Not for: drafting.'\n---\nReview carefully.\n"
    )
    (bot_dir / "cases.yaml").write_text("selection:\n  - goal: Review it\n    expect: imported-reviewer\n")
    (plugin / "plugin.json").write_text(manifest(version="1.2.0"))
    (plugin / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "docs": {"type": "streamable-http", "url": "https://example.com/mcp"},
                    "local-tool": {"type": "stdio", "command": "./bin/server"},
                },
            }
        )
    )
    return plugin


def test_a_conformant_bundle_discovers_whole(tmp_path: Path) -> None:
    layout = plugin_format.discover(write_bundle(tmp_path))
    assert layout.manifest.name == "demo-plugin"
    assert layout.manifest.version == "1.2.0"
    assert layout.skill_dirs == ("skills/clause-notes",)
    assert layout.bot_dirs == ("so.plane.prismoid/bots/imported-reviewer",)
    assert {s.id for s in layout.mcp_servers} == {"docs", "local-tool"}


def test_an_invalid_manifest_rejects_the_whole_plugin(tmp_path: Path) -> None:
    """The one fatal boundary the spec defines."""
    plugin = tmp_path / "broken"
    plugin.mkdir()
    (plugin / "plugin.json").write_text("{not json")
    with pytest.raises(PluginError, match="not valid JSON"):
        plugin_format.discover(plugin)


def test_a_wrong_schema_id_is_fatal_for_the_manifest(tmp_path: Path) -> None:
    """A client selects its supported version by matching the canonical identifier."""
    plugin = tmp_path / "wrong-schema"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(json.dumps({"$schema": "https://elsewhere/1.json", "name": "x"}))
    with pytest.raises(PluginError, match="schema"):
        plugin_format.discover(plugin)


@pytest.mark.parametrize("name", ["UPPER", "-lead", "trail-", "a--b", "a..b", "", "demo\n"])
def test_the_spec_name_rule_is_enforced(tmp_path: Path, name: str) -> None:
    plugin = tmp_path / "named"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": name}))
    with pytest.raises(PluginError, match="not a legal plugin name"):
        plugin_format.discover(plugin)


def test_unknown_manifest_keys_are_report_and_ignore(tmp_path: Path) -> None:
    """ "Unknown plugin.json top-level fields → report and ignore (non-fatal)"."""
    plugin = tmp_path / "extra"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(manifest(experimental=True))
    layout = plugin_format.discover(plugin)
    assert layout.manifest.ignored_keys == ("experimental",)
    assert any("ignored unknown key" in n for n in layout.notes)


def test_a_bad_mcp_entry_skips_that_server_and_continues(tmp_path: Path) -> None:
    """ "Invalid MCP server entry → skip that server, continue"."""
    plugin = tmp_path / "mcp-mixed"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(manifest())
    (plugin / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "good": {"type": "streamable-http", "url": "https://example.com/mcp"},
                    "no-url": {"type": "streamable-http"},
                    "novel": {"type": "carrier-pigeon"},
                },
            }
        )
    )
    layout = plugin_format.discover(plugin)
    assert [s.id for s in layout.mcp_servers] == ["good"]
    assert any("no-url" in n for n in layout.notes)
    assert any("carrier-pigeon" in n for n in layout.notes)


def test_a_schema_mismatch_invalidates_mcp_but_nothing_else(tmp_path: Path) -> None:
    plugin = write_bundle(tmp_path)
    (plugin / "mcp.json").write_text(json.dumps({"$schema": "https://old/0.9.json", "mcpServers": {}}))
    layout = plugin_format.discover(plugin)
    assert layout.mcp_servers == ()
    assert layout.skill_dirs  # the skills component still loaded
    assert any("MCP component skipped" in n for n in layout.notes)


def test_other_namespaces_are_ignored_without_being_read(tmp_path: Path) -> None:
    """ "Clients ignore unimplemented namespaces without validating their contents"."""
    plugin = write_bundle(tmp_path)
    foreign = plugin / "com.example.client" / "hooks"
    foreign.mkdir(parents=True)
    (foreign / "hooks.json").write_text("{this would never parse")
    layout = plugin_format.discover(plugin)  # no error, no note about it
    assert not any("com.example.client" in n for n in layout.notes)


def test_a_skills_file_where_a_directory_should_be_skips_the_component(tmp_path: Path) -> None:
    """ "If a location exists but has wrong type, that component type is invalid and skipped"."""
    plugin = tmp_path / "flat-skills"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(manifest())
    (plugin / "skills").write_text("not a directory")
    layout = plugin_format.discover(plugin)
    assert layout.skill_dirs == ()
    assert any("not a directory" in n for n in layout.notes)
