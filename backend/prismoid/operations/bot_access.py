# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A bot's access tab: the connectors it declares, resolved for the viewer who is looking.

Read-only. Editing a connection stays on /connectors (the one-home law); this module only
shows and deep-links. Nothing here commits.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.connector import Connector
from prismoid.db.models.project import Role
from prismoid.operations.bot_scope import authorize, working_record
from prismoid.schemas.bots import BotAccess, BotConnectorAccess
from prismoid.schemas.connector_catalog import CatalogEntry, entry_for
from prismoid.schemas.connectors import ConnectorStatus, ConnectorToolInfo, ToolPolicy, effective_policies

# A connection's stored status, as the readiness word the access tab shows.
_READINESS: Final[dict[str, str]] = {
    ConnectorStatus.LIVE.value: "connected",
    ConnectorStatus.NEEDS_REAUTH.value: "needs_auth",
    ConnectorStatus.ERROR.value: "needs_auth",
    ConnectorStatus.PENDING_AUTH.value: "needs_auth",
    ConnectorStatus.AUTHORIZING.value: "needs_auth",
    ConnectorStatus.DISABLED.value: "disabled",
}


async def access_of(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
) -> BotAccess:
    """The access tab: the bot's declared connectors, resolved for THE CALLING
    VIEWER - identity from the catalog, the author's why from the manifest, my readiness from
    my own rows, my per-tool policy rollup from `effective_policies`. Editing stays
    on /connectors (the one-home law); this only shows and deep-links."""
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    record = await working_record(session, actor, name, project_id=project_id)
    manifest = record.manifest or {}
    slugs = [str(c) for c in manifest.get("connectors", [])]
    why = {str(k): str(v) for k, v in (manifest.get("connector_why") or {}).items()}
    if not slugs:
        return BotAccess()

    mine = await Connector.resolve_refs(session, actor.id, slugs)
    entries: list[BotConnectorAccess] = []
    for slug in slugs:
        row = mine.get(slug)
        catalog_entry = entry_for(slug)
        if row is None:
            entries.append(_unconnected(slug, why.get(slug, ""), catalog_entry))
        else:
            entries.append(_connected(slug, why.get(slug, ""), row, catalog_entry))
    return BotAccess(connectors=tuple(entries))


def _unconnected(slug: str, why: str, catalog_entry: CatalogEntry | None) -> BotConnectorAccess:
    """A declared connector the viewer has no row for: a catalog one to connect, or a custom one."""
    return BotConnectorAccess(
        slug=slug,
        why=why,
        display_name=catalog_entry.name if catalog_entry else slug,
        catalog=catalog_entry is not None,
        status="needs_connect" if catalog_entry else "custom",
    )


def _connected(slug: str, why: str, row: Connector, catalog_entry: CatalogEntry | None) -> BotConnectorAccess:
    """The viewer's own connection: its readiness and its per-tool policy rollup."""
    tools = [ConnectorToolInfo.model_validate(t) for t in (row.tools or [])]
    policy = effective_policies(row.tool_policies, tools)
    return BotConnectorAccess(
        slug=slug,
        why=why,
        display_name=row.name or (catalog_entry.name if catalog_entry else slug),
        catalog=catalog_entry is not None,
        status=_READINESS.get(row.status, "needs_auth"),
        connector_id=str(row.id),
        tools_allow=sum(1 for p in policy.values() if p is ToolPolicy.ALLOW),
        tools_ask=sum(1 for p in policy.values() if p is ToolPolicy.ASK),
        tools_off=sum(1 for p in policy.values() if p is ToolPolicy.OFF),
        tool_policies={n: p.value for n, p in sorted(policy.items())},
    )
