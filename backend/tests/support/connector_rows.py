# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Rows and probes the connector tests share: a second person, a Project they share, and a server
whose tools say which of them only read. A plain module, so the connector test files import it and
never each other."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.connector import Connector
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.operations import connectors as connector_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult
from tests.support.cast import member


async def second_actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Bea")
    await db.commit()
    return row


async def shared_project(db: AsyncSession, *, owner: Actor, member: Actor) -> Project:
    project = Project(name="shared", owner_actor_id=owner.id)
    db.add(project)
    await db.commit()
    db.add(ProjectMember(project_id=project.id, actor_id=member.id, role=Role.EDITOR, added_by=owner.id))
    await db.commit()
    return project


def patch_email_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe answers a server with one write (`send_email`) and one read (`search`)."""

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


MCP_URL = "https://mcp.example.test/mcp"


async def connect_gmail(db: AsyncSession, owner: Actor, url: str = MCP_URL) -> Connector:
    """The owner connects Gmail: connecting is the consent, so every tool runs on her own turn."""
    return await connector_ops.create_connector(db, owner, server_url=url, name="Gmail")


ALL_ASK = {"send_email": "ask", "search": "ask"}  # what a mount's policies read when every tool asks


async def adas_turn_with_bob_in_the_project(db: AsyncSession, ada: Actor) -> tuple[Actor, Any]:
    """Ada's Project task, her Gmail explicitly attached, turn 1 live and hers; Bob edits the Project."""
    bob = await second_actor(db)
    project = await shared_project(db, owner=ada, member=bob)
    await connect_gmail(db, ada)
    task = await task_ops.create_task(
        db, ada, goal="triage my inbox", project_id=project.id, connectors=["gmail"]
    )
    await db.commit()
    assert task.turn_actor_id == ada.id and not task.is_terminal()
    return bob, task


async def gmail_mount(db: AsyncSession, task: Any) -> tuple[dict[str, str], list[str]]:
    """The tool policies the task's Gmail mounts with, and the notes the mount left."""
    resolved = await connector_ops.resolve_for_task(db, task)
    (spec,) = resolved.specs
    return {n: str(p) for n, p in spec.tool_policies.items()}, resolved.notes
