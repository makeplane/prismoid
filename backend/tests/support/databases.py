# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Where the suite's databases come from, read by `tests/conftest.py` before any `prismoid` import.

It imports nothing from `prismoid`: `prismoid.conf` reads the environment once, at its own import, and these
helpers exist to set that environment first."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from tests.support.paths import BACKEND

DEFAULT_URL = "postgresql://prismoid:prismoid@localhost:5433/prismoid"


def api_root(worker_root: str) -> str:
    """The API's server URL (no database name). It may live only in .env (pydantic reads that
    itself), so it is derived by scheme-swap unless the env already carries one - and an
    os.environ value beats .env in pydantic-settings, which is the whole mechanism. (The env value
    still holds its /dbname and needs the rpartition; the scheme-swap fallback is ALREADY a root -
    rpartitioning it too sliced at the scheme's "//" and sent the clone's name to DNS as a host.)"""
    env_api = os.environ.get("PRISMOID_API_DATABASE_URL")
    if env_api:
        return env_api.rpartition("/")[0]
    return worker_root.replace("postgresql://", "postgresql+asyncpg://", 1)


def build_fresh_template() -> None:
    """`PRISMOID_TEST_FRESH_DB=1` (CI, or a contributor with no dev DB): the workers clone a database
    this run migrates and seeds from nothing, never whatever the developer's DB holds today.

    The conftest runs it once, in the process that is not an xdist worker (the controller, or a
    plain run), before any worker starts. It points both URLs at the fresh database; the workers
    inherit that environment, so their clone step copies it like any other template. The seeds are
    the ones a fresh install needs (models, builtin skills), plus one account made on the box: the
    first account is the platform admin, and a test about an ordinary member must not be first."""
    import asyncpg

    root, _, name = os.environ.get("PRISMOID_WORKER_DATABASE_URL", DEFAULT_URL).rpartition("/")
    fresh = f"{name}_xt_template"

    async def recreate() -> None:
        conn = await asyncpg.connect(f"{root}/postgres")
        try:
            await conn.execute(f'drop database if exists "{fresh}" with (force)')
            await conn.execute(f'create database "{fresh}"')
        finally:
            await conn.close()

    asyncio.run(recreate())
    os.environ["PRISMOID_API_DATABASE_URL"] = f"{api_root(root)}/{fresh}"
    os.environ["PRISMOID_WORKER_DATABASE_URL"] = f"{root}/{fresh}"
    prismoid = [sys.executable, "-m", "prismoid.runner.main"]
    for argv in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [*prismoid, "models", "seed"],
        [*prismoid, "skills", "seed"],
        [*prismoid, "admin", "bootstrap", "--email", "admin@example.com", "--name", "Admin"],
    ):
        subprocess.run(argv, cwd=BACKEND, check=True, stdout=subprocess.DEVNULL)
