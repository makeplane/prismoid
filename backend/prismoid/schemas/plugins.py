# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plugin shapes - the Agent Plugins v1 bundle, as values (ADR-045).

A plugin is a **directory**: `plugin.json` at the root, portable `skills/`, portable `mcp.json`,
and reverse-domain namespaces for what the standard has not yet standardised - which is where
our bots live (`so.plane.prismoid/bots/`). These values are the parsed form; the failure
boundaries the spec defines (invalid manifest → reject the plugin; invalid component → skip it,
continue, say so) are carried as `notes` rather than exceptions, because "loaded what it could
and said what it couldn't" IS the conformant behaviour, not an error path.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

PLUGIN_SCHEMA_ID: Final = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID: Final = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


class PluginManifest(BaseModel):
    """`plugin.json`, validated to v1. Only `$schema` and `name` are required; the metadata
    fields ride along when present. Unknown top-level keys are report-and-ignore (the spec's
    words), so they land in `ignored_keys` rather than failing the parse."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str = ""
    description: str = ""
    license: str = ""
    ignored_keys: tuple[str, ...] = ()


class McpServer(BaseModel):
    """One `mcpServers` entry. `stdio` carries a command; the HTTP kinds carry a URL - parsed
    for all three, because refusing `stdio` is the INSTALLER's policy call (we do not execute
    imported binaries) and the parser's job is to represent the bundle faithfully."""

    model_config = ConfigDict(frozen=True)

    id: str  # the member name in mcpServers
    type: Literal["stdio", "streamable-http", "sse"]
    url: str = ""  # http kinds
    command: str = ""  # stdio
    headers: dict[str, str] = Field(default_factory=dict)


class PluginLayout(BaseModel):
    """What discovery found at fixed locations - paths and parsed entries, plus the honest notes.

    Paths are strings relative to the plugin root: the value crosses into `operations`, and a
    relative path survives the trip where an absolute one would leak the installer's disk.
    """

    model_config = ConfigDict(frozen=True)

    manifest: PluginManifest
    skill_dirs: tuple[str, ...] = ()  # immediate children of skills/ holding SKILL.md
    bot_dirs: tuple[str, ...] = ()  # so.plane.prismoid/bots/<name>/ holding BOT.md
    mcp_servers: tuple[McpServer, ...] = ()
    notes: tuple[str, ...] = ()  # every skip, with its reason - the spec's failure boundaries


class InstalledItem(BaseModel):
    """One thing an install produced - the receipt's line item."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["skill", "bot", "connector"]
    name: str
    outcome: str  # "draft v2" · "connector pending_auth" · "skipped: …"
    ok: bool = True


class InstallReport(BaseModel):
    """What `plugins install` did, in full - the CLI renders it, `plugin_installs` stores it."""

    model_config = ConfigDict(frozen=True)

    plugin: str
    version: str = ""
    items: tuple[InstalledItem, ...] = ()
    notes: tuple[str, ...] = ()
