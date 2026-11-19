# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plugin installs - the receipts. Installing runs in the CLI (it reads a local directory the
server has no path to), so the API's surface is read-only: what was installed, what each part
became. Every route requires an Actor; the rows are personal, like connectors."""

from __future__ import annotations

from prismoid.api.deps import ActorDep, PageDep, SessionDep
from prismoid.api.serializers.common import PageOut
from prismoid.api.serializers.plugins import PluginInstallOut, plugin_install_out
from prismoid.operations import plugins as ops


async def list_plugin_installs(
    actor: ActorDep, session: SessionDep, page: PageDep
) -> PageOut[PluginInstallOut]:
    rows = await ops.installs_for(session, actor, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1].cursor() if len(rows) == page.limit else None
    return PageOut(items=[plugin_install_out(r) for r in rows], next_cursor=next_cursor)
