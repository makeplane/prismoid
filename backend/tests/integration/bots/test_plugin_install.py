# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Install and export (ADR-045): a bundle in, drafts and receipts out - and back again.

The connector half is exercised only to its refusals here (stdio, and a URL the SSRF guard
rejects) because a live HTTP server is a network dependency; the create path itself is
the connector door's, tested where it lives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.skill import SkillRecord
from prismoid.db.session import session_scope
from prismoid.errors import Conflict
from prismoid.operations import bots as bot_ops
from prismoid.operations import plugins as ops
from prismoid.operations import skills as skill_ops
from prismoid.schemas.bots import BotSource, SelectionCaseResult, SelectionReport
from prismoid.schemas.plugins import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _actor(session: AsyncSession, name: str = "Installer") -> Actor:
    row, _ = await member(session, name)
    await session.flush()
    return row


def _bundle(root: Path, *, bot: str, skill: str) -> Path:
    plugin = root / "bundle"
    skill_dir = plugin / "skills" / skill
    skill_dir.mkdir(parents=True)
    # Spec-legal, doctrine-shy on purpose: no "Load when/Not for", no cases - the import door
    # must accept it as a draft, and the publish gate must then hold the line.
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: Reference notes for contract reviewers.\n"
        "license: Apache-2.0\nmetadata:\n  author: example-workspace\n---\nThe notes.\n"
    )
    bot_dir = plugin / "so.plane.prismoid" / "bots" / bot
    bot_dir.mkdir(parents=True)
    (bot_dir / "BOT.md").write_text(
        f"---\nname: {bot}\ndescription: 'Reviews imported things carefully. "
        "Load when: an import review is asked. Not for: drafting.'\n---\nReview carefully.\n"
    )
    (bot_dir / "cases.yaml").write_text(f"selection:\n  - goal: Review it\n    expect: {bot}\n")
    (plugin / "plugin.json").write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": "bundle", "version": "1.0.0"})
    )
    (plugin / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "local-tool": {"type": "stdio", "command": "./bin/server"},
                    "metadata-service": {"type": "streamable-http", "url": "http://169.254.169.254/mcp"},
                },
            }
        )
    )
    return plugin


async def test_a_bundle_lands_as_drafts_with_a_receipt(tmp_path: Path) -> None:
    import uuid as _uuid

    bot = f"imported-{_uuid.uuid4().hex[:8]}"
    skill = f"notes-{_uuid.uuid4().hex[:8]}"
    async with session_scope() as session:
        installer = await _actor(session)
        report = await ops.install(session, installer, _bundle(tmp_path, bot=bot, skill=skill))

        by_name = {i.name: i for i in report.items}
        assert by_name[skill].outcome == "draft v1"
        assert by_name[bot].outcome == "draft v1"
        # stdio: refused by policy; the metadata URL: refused by the SSRF guard.
        assert not by_name["local-tool"].ok and "do not execute" in by_name["local-tool"].outcome
        assert not by_name["metadata-service"].ok

        # drafts, not published - nothing an install produces is live
        skill_row = await SkillRecord.latest_draft(session, skill, installer.id)
        assert skill_row is not None and skill_row.status == "draft"
        bot_row = await BotRecord.latest_draft(session, bot, installer.id, project_id=None)
        assert bot_row is not None and bot_row.status == "draft"
        assert (bot_row.source or "").startswith("plugin:bundle@1.0.0")

        # and the receipt is durable
        receipts = await ops.installs_for(session, installer)
        assert receipts[0].name == "bundle"
        assert len(receipts[0].report["items"]) == 4


async def test_an_imported_skill_cannot_publish_until_it_meets_the_doctrine(tmp_path: Path) -> None:
    """Parse to the spec, gate on publish: the 409 still owns the bar."""
    import uuid as _uuid

    bot = f"imported-{_uuid.uuid4().hex[:8]}"
    skill = f"notes-{_uuid.uuid4().hex[:8]}"
    async with session_scope() as session:
        installer = await _actor(session)
        await ops.install(session, installer, _bundle(tmp_path, bot=bot, skill=skill))
        with pytest.raises(Conflict, match="Load when"):
            await skill_ops.publish(session, actor_id=installer.id, name=skill)


async def test_export_round_trips_through_install(tmp_path: Path) -> None:
    """A bot leaves as a bundle another install can read back - the portability claim, proven."""
    import uuid as _uuid

    name = f"traveller-{_uuid.uuid4().hex[:8]}"
    source = BotSource(
        bot_md=(
            f"---\nname: {name}\ndescription: 'Reviews travelling things. "
            "Load when: a travel review is asked. Not for: drafting.'\n"
            "capability: 'frontier-reasoner'\ntools: [frame.read_file]\n---\nReview on the road.\n"
        ),
        cases_yaml=f"selection:\n  - goal: Review my itinerary\n    expect: {name}\n",
    )
    async with session_scope() as session:
        author = await _actor(session, "Exporter")
        row = await bot_ops.push(session, actor=author, name=name, source=source)
        await bot_ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=row.version,
            report=SelectionReport(
                bot=name,
                version=row.version,
                passed=1,
                total=1,
                cases=(SelectionCaseResult(goal="g", expected=name, chosen=name, ok=True),),
            ),
        )
        await bot_ops.publish(session, actor=author, name=name)

        out, notes = await ops.export_bot(session, author, name, tmp_path / "out")
        assert notes == []
        assert (out / "plugin.json").is_file()

        # …and a second user installs the very bundle that just left
        importer = await _actor(session, "Importer")
        report = await ops.install(session, importer, out)
        assert {i.name: i.outcome for i in report.items} == {name: "draft v1"}
        back = await BotRecord.latest_draft(session, name, importer.id, project_id=None)
        assert back is not None
        assert back.instructions == "Review on the road."
