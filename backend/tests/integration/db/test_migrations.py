# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Migrations are reversible - proven, not promised (promoted by ADR-008).

An untested downgrade is a lie: the one time it runs for real is an incident, which is exactly the
wrong moment to discover a `downgrade()` that never worked. So the whole chain is walked - head →
base → head - against a scratch database on the same server, leaving the dev database untouched.
Subprocess `alembic`, because that is what an operator types; the URL override rides the
environment, because the URL is a setting and settings read the environment.

Needs the stack: `just up`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import asyncpg
import pytest

from prismoid.conf import settings
from prismoid.db.models import ALL_TABLES

pytestmark = pytest.mark.integration

SCRATCH = "prismoid_migration_roundtrip"


def _dsn(database: str) -> str:
    # asyncpg speaks plain postgres:// - the SQLAlchemy driver suffix means nothing to it.
    url = settings.api.database_url.replace("postgresql+asyncpg", "postgresql")
    return url.rsplit("/", 1)[0] + f"/{database}"


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(_dsn("postgres"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _tables() -> set[str]:
    conn = await asyncpg.connect(_dsn(SCRATCH))
    try:
        rows = await conn.fetch(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' and table_type = 'BASE TABLE'"
        )
        return {r["table_name"] for r in rows}
    finally:
        await conn.close()


def _alembic(*args: str) -> None:
    env = {
        **os.environ,
        "PRISMOID_API_DATABASE_URL": _dsn(SCRATCH).replace("postgresql", "postgresql+asyncpg", 1),
    }
    done = subprocess.run(["uv", "run", "alembic", *args], env=env, capture_output=True, text=True)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout}\n{done.stderr}"


def test_the_whole_chain_walks_down_and_back() -> None:
    """Sync on purpose: alembic's env.py calls `asyncio.run`, which refuses to start inside a
    running loop - and a subprocess is the honest shape anyway (it is the operator's command)."""
    asyncio.run(_admin(f'drop database if exists "{SCRATCH}"'))
    asyncio.run(_admin(f'create database "{SCRATCH}"'))
    try:
        _alembic("upgrade", "head")
        missing = set(ALL_TABLES) - asyncio.run(_tables())
        assert not missing, f"upgrade head did not create: {sorted(missing)}"

        _alembic("downgrade", "base")
        left = asyncio.run(_tables()) - {"alembic_version"}
        assert not left, f"downgrade base left tables behind: {sorted(left)}"

        _alembic("upgrade", "head")
        missing = set(ALL_TABLES) - asyncio.run(_tables())
        assert not missing, f"the second upgrade did not rebuild: {sorted(missing)}"
    finally:
        asyncio.run(_admin(f'drop database if exists "{SCRATCH}"'))
