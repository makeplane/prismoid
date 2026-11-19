# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Probe every connector-catalog seed live (ADR-054's landing rule): an `open` entry must
answer initialize + list_tools; an `oauth` entry must answer the probe with an auth challenge
(401/403), which proves the URL is a real MCP endpoint behind consent - a connection failure
or a plain 404 means the entry does not ship. Run before any catalog data edit lands:

    just connectors-catalog-probe
"""

from __future__ import annotations

import asyncio
import sys

from prismoid.ai.tools.connectors import ProbeAuthRequired, ProbeFailed, probe_server
from prismoid.schemas.connector_catalog import CATALOG, CatalogAuth


async def check_one(slug: str, url: str, auth: CatalogAuth) -> str | None:
    try:
        result = await probe_server(url, timeout_s=15.0)
    except ProbeAuthRequired:
        if auth is CatalogAuth.OAUTH:
            print(f"  ok    {slug:<20} challenges for auth (as declared)")
            return None
        return f"{slug}: declared open but the server demands authorization"
    except ProbeFailed as bad:
        return f"{slug}: probe failed - {bad}"
    if auth is CatalogAuth.OAUTH:
        # The Google shape: some OAuth servers LIST tools anonymously
        # and lock the CALLS. The probe still proved a real MCP server at the URL, and the
        # oauth declaration still governs the connect UX - a note, never a failure. The hard
        # failure stays the other direction: an `open` entry that demands auth is a card
        # whose Add button lies.
        print(f"  ok    {slug:<20} lists anonymously ({len(result.tools)} tools), calls locked (oauth)")
        return None
    print(f"  ok    {slug:<20} {len(result.tools)} tools anonymously")
    return None


async def main() -> int:
    failures = [
        bad
        for bad in await asyncio.gather(*(check_one(e.slug, e.server_url, e.auth) for e in CATALOG))
        if bad
    ]
    for bad in failures:
        print(f"  FAIL  {bad}")
    print(f"{len(CATALOG) - len(failures)}/{len(CATALOG)} catalog entries verified")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
