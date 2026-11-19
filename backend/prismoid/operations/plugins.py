# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plugins - Agent Plugins v1 bundles, installed and exported (ADR-045).

The composer that walks a discovered bundle through the right doors: `skills/` through the
skills format's IMPORT door (spec-shaped, doctrine deferred to publish), our namespace's bots
through the bot door as drafts, `mcp.json`'s HTTP servers through the connectors create path -
which means the connectors' SSRF guard, the probe, and Fernet at rest, none of it re-implemented
here. **`stdio` servers are refused by policy**: we do not execute imported binaries, and the
refusal is a line on the receipt rather than a failure of the install.

Nothing an install produces is live: skills and bots land as DRAFTS behind their own publish
gates, and a connector lands `pending_auth` until its owner authorizes it. An imported bundle
can therefore never add capability to a running task by itself - the same posture every other
door in this product takes.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.bots import format as bot_format
from prismoid.ai.bots.format import BOT_FILE, BOTS_DIR, CASES_FILE, NAMESPACE, BotError
from prismoid.ai.plugins import format as plugin_format
from prismoid.ai.skills import format as skill_format
from prismoid.ai.skills.format import SkillError
from prismoid.db.models.actor import Actor
from prismoid.db.models.plugin_install import PluginInstall
from prismoid.errors import Conflict, ValidationFailed
from prismoid.schemas.bots import BotSource
from prismoid.schemas.plugins import InstalledItem, InstallReport


async def install(
    session: AsyncSession, actor: Actor, root: Path, *, project_id: uuid.UUID | None = None
) -> InstallReport:
    """One bundle → drafts and rows, with a line-item receipt.

    Per-component failure isolation is the spec's law and this function's shape: a skill that
    fails its door costs that skill a line, never the install. The receipt is committed as a
    `plugin_installs` row so `GET /v1/plugins` can answer later without re-reading any disk.
    """
    from prismoid.operations import bots as bot_ops
    from prismoid.operations import connectors as connector_ops
    from prismoid.operations import skills as skill_ops

    layout = plugin_format.discover(root)
    items: list[InstalledItem] = []

    for rel in layout.skill_dirs:
        directory = root / rel
        name = directory.name
        try:
            # The IMPORT door: the Agent Skills spec's rules; the house doctrine meets it at publish.
            parsed = skill_format.parse_dir(directory, strict=False)
            record = await skill_ops.push(
                session,
                actor_id=actor.id,
                name=name,
                skill_md=skill_format.render(parsed.manifest, parsed.body),
                cases_yaml=skill_format.render_cases(parsed.routing_cases, parsed.golden_cases),
                files=[(f.path, f.data) for f in parsed.files],
                strict=False,  # already validated to the spec by the lenient walk above
            )
            items.append(InstalledItem(kind="skill", name=name, outcome=f"draft v{record.version}"))
        except (SkillError, ValidationFailed, Conflict) as exc:  # Conflict: a builtin's name
            items.append(InstalledItem(kind="skill", name=name, outcome=f"skipped: {exc}", ok=False))

    for rel in layout.bot_dirs:
        directory = root / rel
        name = directory.name
        try:
            cases_path = directory / CASES_FILE
            source = BotSource(
                bot_md=(directory / BOT_FILE).read_text(),
                cases_yaml=cases_path.read_text() if cases_path.is_file() else "",
            )
            record = await bot_ops.push(session, actor=actor, name=name, source=source, project_id=project_id)
            record.source = f"plugin:{layout.manifest.name}@{layout.manifest.version or 'unversioned'}"
            items.append(InstalledItem(kind="bot", name=name, outcome=f"draft v{record.version}"))
        except (BotError, ValidationFailed) as exc:
            items.append(InstalledItem(kind="bot", name=name, outcome=f"skipped: {exc}", ok=False))

    for server in layout.mcp_servers:
        if server.type == "stdio":
            # Policy, not parsing: a stdio server is a binary the bundle wants executed, and we
            # do not run imported code outside a frame. The bundle stays honest on the receipt.
            items.append(
                InstalledItem(
                    kind="connector",
                    name=server.id,
                    outcome="skipped: stdio servers are refused - we do not execute imported binaries",
                    ok=False,
                )
            )
            continue
        try:
            row = await connector_ops.create_connector(
                session,
                actor,
                server_url=server.url,
                name=server.id,
                # Spec: "Configured env values are visible package data, not a portable secret
                # mechanism." Headers get the same reading - a bundled Authorization header is a
                # published credential, and we refuse to store one. The owner authorizes for real.
            )
            items.append(InstalledItem(kind="connector", name=row.slug, outcome=f"connector {row.status}"))
        except Exception as exc:  # the connector door 4xxs with reasons; each is one line here
            items.append(InstalledItem(kind="connector", name=server.id, outcome=f"skipped: {exc}", ok=False))

    report = InstallReport(
        plugin=layout.manifest.name,
        version=layout.manifest.version,
        items=tuple(items),
        notes=layout.notes,
    )
    session.add(
        PluginInstall(
            id=uuid.uuid4(),
            owner_actor_id=actor.id,
            name=layout.manifest.name,
            version=layout.manifest.version,
            report=report.model_dump(mode="json"),
        )
    )
    await session.commit()
    logfire.info(
        "installed plugin {name}: {n} item(s), {bad} skipped",
        name=layout.manifest.name,
        n=len(items),
        bad=sum(1 for i in items if not i.ok),
    )
    return report


async def installs_for(
    session: AsyncSession, actor: Actor, *, limit: int = 50, cursor: str | None = None
) -> list[PluginInstall]:
    return await PluginInstall.for_owner(session, actor.id, limit=limit, cursor=cursor)


async def export_bot(
    session: AsyncSession,
    actor: Actor,
    name: str,
    out_dir: Path,
    *,
    project_id: uuid.UUID | None = None,
) -> tuple[Path, list[str]]:
    """One bot → a conformant plugin directory: the bot in our namespace, its pinned skills as
    the portable half, its connectors as `mcp.json` - **urls only, never a credential**. A skill
    or connector that no longer resolves costs a note, not the export.
    """
    from prismoid.db.models.connector import Connector
    from prismoid.operations import bots as bot_ops
    from prismoid.operations import skills as skill_ops

    bot = await bot_ops.resolve(session, actor, name, project_id=project_id)
    # The cases of the EXPORTED version, not whatever is newest - a draft's half-finished cases
    # must not ride out under the published head's name.
    rows = await bot_ops.versions(session, actor, name, project_id=project_id)
    exported = next((r for r in rows if r.version == bot.version), rows[0])
    cases, golden = bot_format.cases_of(exported.manifest or {}, default_expect=name)
    notes: list[str] = []

    root = out_dir / name
    bot_dir = root / NAMESPACE / BOTS_DIR / name
    bot_dir.mkdir(parents=True, exist_ok=True)
    (root / plugin_format.PLUGIN_MANIFEST).write_text(
        plugin_format.render_plugin_manifest(name, bot.manifest.description.split(".")[0])
    )
    (bot_dir / BOT_FILE).write_text(bot_format.render_bot_md(bot.manifest, bot.instructions))
    (bot_dir / CASES_FILE).write_text(bot_format.render_cases(cases, golden))

    for skill_name in bot.manifest.skills:
        try:
            resolved = await skill_ops.resolve(session, str(skill_name))
        except Exception:
            notes.append(f"skill {skill_name}: not published here - left out of the bundle")
            continue
        try:
            checked = skill_format.check_stored(resolved.name, [f.path for f in resolved.files])
        except SkillError as exc:
            notes.append(f"skill {skill_name!r}: {exc} - left out of the bundle")
            continue
        skill_dir = root / "skills" / checked
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_format.render(resolved.manifest, resolved.body))
        (skill_dir / "cases.yaml").write_text(
            skill_format.render_cases(resolved.routing_cases, resolved.golden_cases)
        )
        for bundled in resolved.files:
            path = skill_dir / bundled.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bundled.data)

    if bot.manifest.connectors:
        rows = await Connector.resolve_refs(session, actor.id, list(bot.manifest.connectors))
        entries: dict[str, dict[str, str]] = {}
        for ref in bot.manifest.connectors:
            row = rows.get(str(ref))
            if row is None:
                notes.append(f"connector {ref}: not yours or gone - left out of the bundle")
                continue
            entries[row.slug] = {"type": "streamable-http", "url": row.server_url}
        if entries:
            import json

            (root / "mcp.json").write_text(
                json.dumps(
                    {
                        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                        "mcpServers": entries,
                    },
                    indent=2,
                )
                + "\n"
            )
    return root, notes
