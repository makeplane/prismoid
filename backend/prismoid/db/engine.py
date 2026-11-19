# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The async engine. One pool, warmed, orjson on the wire, and a small write pool beside it for a
hot-path idempotent statement that must be durable in one round trip (`idempotent_write`).

Lazy by construction: the CLI imports `prismoid.*` too, and a thin API client has no business
opening a Postgres pool as a side effect of an import.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import orjson
from sqlalchemy import CursorResult, Executable
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from prismoid.conf import settings


def _dumps(value: Any) -> str:
    return orjson.dumps(value).decode()


_application_name = "prismoid"


def set_application_name(name: str) -> None:
    """Name this process's pooled connections, so `pg_stat_activity` can attribute them.

    Call it once, **before the first `get_engine()`** - the API's lifespan, the worker's `main` -
    because the name is read when the pool builds a connection. `reset_engine()` rebuilds with
    whatever is set now, which is why the worker's boot dance (dispose → reset → DBOS's own loop)
    still lands on `prismoid-worker`.

    Diagnosability is the point: an unnamed connection stuck `idle in transaction` is a
    ghost in `pg_stat_activity`; a `prismoid-api` one is a bug (a view holding a tx it should have
    released), a `prismoid-worker` one is a step legitimately running. The name tells them apart.
    """
    global _application_name
    _application_name = name


# The write pool's ceiling: each statement holds a connection for one round trip, so a handful
# serves thousands of writes a second without adding much to Postgres's connection count.
WRITE_POOL_SIZE = 4
_write: tuple[AsyncEngine, AsyncEngine] | None = None  # (the main engine it follows, the write pool)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return _engine(
        pool_size=settings.api.db_pool_size,
        max_overflow=settings.api.db_max_overflow,
        pool_pre_ping=settings.api.db_pool_pre_ping,
    )


def _write_engine() -> AsyncEngine:
    """The write pool, rebuilt whenever `get_engine` is (a test that moves the database moves it)."""
    global _write
    main = get_engine()
    if _write is None or _write[0] is not main:
        _write = (
            main,
            _engine(
                pool_size=WRITE_POOL_SIZE,
                max_overflow=WRITE_POOL_SIZE,
                pool_pre_ping=False,
                isolation_level="AUTOCOMMIT",
            ),
        )
    return _write[1]


async def idempotent_write(statement: Executable) -> CursorResult[Any]:
    """One idempotent statement, durable on return, in one autocommit round trip on the write pool;
    a dropped connection runs it once more on a fresh one."""
    try:
        return await _autocommit(statement)
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
        return await _autocommit(statement)


async def _autocommit(statement: Executable) -> CursorResult[Any]:
    async with _write_engine().connect() as conn:
        return await conn.execute(statement)


def _engine(**pool: Any) -> AsyncEngine:
    return create_async_engine(
        settings.api.database_url,
        echo=settings.api.db_echo,
        pool_recycle=settings.api.db_pool_recycle_seconds,
        json_serializer=_dumps,
        json_deserializer=orjson.loads,
        # asyncpg sets these as connection startup parameters - `application_name` names us in
        # pg_stat_activity (see set_application_name). Everything else about staleness the pool
        # already handles: pool_pre_ping reconnects a killed backend transparently (verified), and
        # pool_recycle ages connections out before a proxy's idle timeout can.
        connect_args={"server_settings": {"application_name": _application_name}},
        **pool,
    )


async def warm_pool() -> None:
    """Open the pool before the first request pays for it (a warmed asyncpg pool)."""
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.exec_driver_sql("select 1")


async def dispose_engine() -> None:
    if _write is not None:
        await _write[1].dispose()
    if get_engine.cache_info().currsize:
        await get_engine().dispose()


def reset_engine() -> None:
    """Forget the cached engine and sessionmaker, so the next caller builds a fresh one.

    **An asyncpg pool belongs to the event loop that created it.** The engine is cached per
    process, which is right for a server that owns one loop for its lifetime - and a trap for any
    process that runs `asyncio.run(...)` more than once, because `asyncio.run` *closes* the loop
    it made. The pool survives the loop, the loop does not survive the call, and the next await on
    that pool dies with `RuntimeError: Event loop is closed` - a message that says nothing about
    the cause.

    The worker hits this exactly once, at boot: it does a little async setup (`Actor.sync_agents`)
    before handing control to DBOS, which runs workflows on a loop of its own. Dispose, reset, and
    let DBOS's loop build its own pool.
    """
    from prismoid.db.session import get_sessionmaker

    global _write
    _write = None
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
