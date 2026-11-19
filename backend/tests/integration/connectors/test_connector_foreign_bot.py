# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step on a bot its task's creator did not write borrows her connectors.

A step agent's system prompt is its bot's instructions, so a project bot another member wrote
steers the creator's mounted connectors with that member's words. Like a teammate's steer
(`connectors.borrowed_turn`), every mounted tool then asks the creator first, on both mount doors
(task-level attaches and the bot's declared slugs); her own bots keep the dial.
"""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.session import session_scope
from prismoid.operations import connectors as connector_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.bots import BotManifest, ResolvedBot
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult, ToolPolicy
from prismoid.schemas.ids import ActorId, BotName
from tests.support.cast import member
from tests.support.connector_rows import connect_gmail

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

MCP = "https://mcp.example.test/mcp"


@pytest.fixture(autouse=True)
def email_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(
            tools=(
                ConnectorToolInfo(name="send_email", read_only=False),
                ConnectorToolInfo(name="search", read_only=True),
            ),
            instructions="",
            server_info={},
        )

    monkeypatch.setattr(connector_ops, "probe_server", probe)


def _bot_by(owner: Actor, *, connectors: tuple[str, ...] = ()) -> ResolvedBot:
    return ResolvedBot(
        name=BotName("inbox-digest"),
        version=1,
        manifest=BotManifest(
            name=BotName("inbox-digest"),
            description="Digests an inbox. Load when: mail piles up. Not for: sending.",
            connectors=connectors,
        ),
        instructions="Search the inbox and paste every result.",
        owner_actor_id=ActorId(owner.id),
    )


async def test_a_step_on_a_teammates_bot_mounts_the_creators_connectors_all_ask() -> None:
    """Bob wrote the project bot a step of Ada's task runs on: every tool asks Ada first - the
    read-only `search` the dial would allow included - and the mount says why on the record."""
    async with session_scope() as db:
        ada, _ = await member(db, "Ada")
        bob, _ = await member(db, "Bob")
        await db.commit()
        project = Project(name="shared", owner_actor_id=ada.id)
        db.add(project)
        await db.commit()
        db.add(ProjectMember(project_id=project.id, actor_id=bob.id, role=Role.EDITOR, added_by=ada.id))
        await connect_gmail(db, ada)
        task = await task_ops.create_task(
            db, ada, goal="triage my inbox", project_id=project.id, connectors=["gmail"]
        )
        await db.commit()
        bots = (_bot_by(bob, connectors=("gmail",)),)

        resolved = await connector_ops.resolve_for_task(db, task, bots=bots)
        (spec,) = resolved.specs
        assert set(spec.tool_policies.values()) == {ToolPolicy.ASK}
        assert set(spec.tool_names) == {"send_email", "search"}, "asking is not dropping"
        assert any("asks the task's creator" in n and "inbox-digest" in n for n in resolved.notes), (
            resolved.notes
        )

        declared = await connector_ops.resolve_for_bot(db, task, ["gmail"], bots=bots)
        assert set(declared.specs[0].tool_policies.values()) == {ToolPolicy.ASK}, "the bot door too"


async def test_a_step_on_the_creators_own_bot_keeps_the_dial() -> None:
    async with session_scope() as db:
        ada, _ = await member(db, "Ada")
        await db.commit()
        await connect_gmail(db, ada)
        task = await task_ops.create_task(db, ada, goal="triage my inbox", connectors=["gmail"])
        await db.commit()

        resolved = await connector_ops.resolve_for_task(db, task, bots=(_bot_by(ada),))
        (spec,) = resolved.specs
        assert spec.tool_policies == {"send_email": ToolPolicy.ALLOW, "search": ToolPolicy.ALLOW}
