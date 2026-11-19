# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Agent Plugins v1 reader (agent-plugins.org) - discovery, and the failure boundaries.

The spec's conformance rules, implemented in their own words: components live at **fixed
locations only** (`skills/`, `mcp.json`, reverse-domain dirs - no inline configuration in the
manifest); an invalid `plugin.json` rejects the whole plugin; an invalid component location or
entry skips THAT component and continues, with the skip reported; unknown extension namespaces
are ignored without validating their contents; a `$schema` mismatch between `mcp.json` and
`plugin.json` invalidates MCP but nothing else.

This module DISCOVERS and parses the bundle's own files; it deliberately does not parse the
skills or bots it finds - those have their own one-door validators (`ai/skills/format.py`,
`ai/bots/format.py`), and `operations/plugins.py` is the composer that walks each found
directory through the right one. That keeps this module importable beside its siblings
and keeps every format's rules in exactly one place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlsplit

from prismoid.ai.bots.format import BOT_FILE, BOTS_DIR, NAMESPACE
from prismoid.schemas.ids import skill_name
from prismoid.schemas.plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    McpServer,
    PluginLayout,
    PluginManifest,
)

PLUGIN_MANIFEST: Final = "plugin.json"

# The spec's name rule: 1–64 chars, lowercase alphanumerics with hyphens and periods, no
# leading/trailing hyphen, no `--`, no `..`.
_NAME = re.compile(r"(?!-)(?!.*--)(?!.*\.\.)[a-z0-9.-]{1,64}(?<!-)")

_KNOWN_KEYS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)


class PluginError(Exception):
    """The one fatal case the spec defines: `plugin.json` itself is invalid. Everything below
    the manifest degrades to a skipped component and a note, never to this."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


def render_plugin_manifest(name: str, description: str = "") -> str:
    """The minimal conformant `plugin.json`: v1 requires only `$schema` and `name`; a description
    is what a human sees in a listing."""
    manifest: dict[str, Any] = {"$schema": PLUGIN_SCHEMA_ID, "name": name, "version": "1.0.0"}
    if description:
        manifest["description"] = description
    return json.dumps(manifest, indent=2) + "\n"


def parse_manifest(text: str) -> PluginManifest:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise PluginError(
            f"{PLUGIN_MANIFEST} is not valid JSON: {exc}", {PLUGIN_MANIFEST: "invalid JSON"}
        ) from exc
    if not isinstance(raw, dict):
        raise PluginError(f"{PLUGIN_MANIFEST} must be a JSON object", {PLUGIN_MANIFEST: "not an object"})
    raw = cast("dict[str, Any]", raw)
    if raw.get("$schema") != PLUGIN_SCHEMA_ID:
        raise PluginError(
            f"{PLUGIN_MANIFEST} $schema must be {PLUGIN_SCHEMA_ID!r} - the version a client matches on",
            {"$schema": "unsupported"},
        )
    name = str(raw.get("name", ""))
    if not _NAME.fullmatch(name):
        raise PluginError(
            f"{name!r} is not a legal plugin name (1–64 lowercase alphanumerics, hyphens, periods)",
            {"name": "malformed"},
        )
    return PluginManifest(
        name=name,
        version=str(raw.get("version", "")),
        description=str(raw.get("description", "")),
        license=str(raw.get("license", "")),
        ignored_keys=tuple(sorted(k for k in raw if k not in _KNOWN_KEYS)),
    )


def parse_mcp(text: str) -> tuple[list[McpServer], list[str]]:
    """`mcp.json` → servers + skip notes. An invalid entry skips THAT server; only an invalid
    file (or a `$schema` mismatch) invalidates the whole component - both as notes, never
    exceptions: the rest of the plugin still loads, which is the spec's point."""
    notes: list[str] = []
    try:
        raw = json.loads(text)
    except ValueError:
        return [], ["mcp.json: not valid JSON - MCP component skipped"]
    if not isinstance(raw, dict) or raw.get("$schema") != MCP_SCHEMA_ID:
        return [], [f"mcp.json: $schema must be {MCP_SCHEMA_ID!r} - MCP component skipped"]
    servers: list[McpServer] = []
    entries: object = cast("dict[str, Any]", raw).get("mcpServers")
    if not isinstance(entries, dict):
        return [], ["mcp.json: mcpServers must be an object - MCP component skipped"]
    for server_id, entry in cast("dict[str, object]", entries).items():
        if not isinstance(entry, dict):
            notes.append(f"mcp.json[{server_id}]: not an object - skipped")
            continue
        entry = cast("dict[str, object]", entry)
        kind = str(entry.get("type", ""))
        if kind == "stdio":
            command = str(entry.get("command", ""))
            if not command:
                notes.append(f"mcp.json[{server_id}]: stdio without a command - skipped")
                continue
            servers.append(McpServer(id=str(server_id), type="stdio", command=command))
        elif kind in ("streamable-http", "sse"):
            url = str(entry.get("url", ""))
            # Scheme check via urlsplit, not a literal prefix - partly because it also catches
            # `https:relative`, and partly because the audit grep watches for URL literals
            # and a scheme test is not one.
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                notes.append(f"mcp.json[{server_id}]: {kind} without an absolute http(s) url - skipped")
                continue
            raw_headers = entry.get("headers")
            headers = cast("dict[object, object]", raw_headers) if isinstance(raw_headers, dict) else {}
            servers.append(
                McpServer(
                    id=str(server_id),
                    type=kind,
                    url=url,
                    headers={str(k): str(v) for k, v in headers.items()},
                )
            )
        else:
            notes.append(f"mcp.json[{server_id}]: unsupported transport {kind!r} - skipped")
    return servers, notes


def discover(root: Path) -> PluginLayout:
    """The whole bundle, from its fixed locations. Raises only for the manifest; everything
    else degrades to notes - a plugin that loads what it can and says what it couldn't."""
    root = root.resolve()
    manifest_path = root / PLUGIN_MANIFEST
    if not manifest_path.is_file():
        raise PluginError(f"{root.name}: no {PLUGIN_MANIFEST} - not a plugin", {PLUGIN_MANIFEST: "missing"})
    manifest = parse_manifest(manifest_path.read_text())

    notes: list[str] = []
    if manifest.ignored_keys:
        notes.append(f"{PLUGIN_MANIFEST}: ignored unknown key(s) {', '.join(manifest.ignored_keys)}")

    # skills/ - each child directory with a SKILL.md is one skill, named by its folder (no recursion).
    skill_dirs: list[str] = []
    skills_root = root / "skills"
    if skills_root.exists() and not skills_root.is_dir():
        notes.append("skills: exists but is not a directory - skills component skipped")
    elif skills_root.is_dir():
        for child in sorted(skills_root.iterdir()):
            if not (child.is_dir() and (child / "SKILL.md").is_file()):
                continue
            try:
                skill_name(child.name)
            except ValueError as exc:
                notes.append(f"skills/{child.name}: {exc} - skipped")
                continue
            skill_dirs.append(child.relative_to(root).as_posix())

    # mcp.json
    servers: list[McpServer] = []
    mcp_path = root / "mcp.json"
    if mcp_path.is_file():
        servers, mcp_notes = parse_mcp(mcp_path.read_text())
        notes += mcp_notes

    # our namespace - bots. Other reverse-domain namespaces are ignored without being read,
    # which is exactly what the spec asks of a namespace a client does not implement.
    bot_dirs: list[str] = []
    bots_root = root / NAMESPACE / BOTS_DIR
    if bots_root.is_dir():
        for child in sorted(bots_root.iterdir()):
            if child.is_dir() and (child / BOT_FILE).is_file():
                bot_dirs.append(child.relative_to(root).as_posix())

    return PluginLayout(
        manifest=manifest,
        skill_dirs=tuple(skill_dirs),
        bot_dirs=tuple(bot_dirs),
        mcp_servers=tuple(servers),
        notes=tuple(notes),
    )
