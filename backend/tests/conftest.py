# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Test wiring.

Two things, and both were learned the hard way.

**The engine.** It is an `lru_cache`d singleton, one warmed pool opened once, and asyncpg connections
are bound to the event loop that created them. A pool cached across two loops produces `attached to
a different loop`, and the error tells you nothing about the cause. So the suite runs ONE event loop
per session (per xdist worker), set in pyproject's pytest config, and the pool lives exactly as long
as that loop: `one_engine_per_session` disposes it once, at the end.

**The cleanup.** The first version of these fixtures ended with `delete from actors` - which nuked
the whole table, including the developer's `prismoid login` session. It bit me four times: run the
suite, then watch every CLI command fail with `unauthorized`, and spend a minute working out why.

**A test that destroys your dev data is a test you stop running.** So cleanup is scoped to *what
this test created*, by time window: anything born after the fixture started. The dev's login was
born before, and survives.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any

from tests.support.databases import DEFAULT_URL, api_root, build_fresh_template

# ── xdist isolation: a PRIVATE database per worker, cloned from the dev DB as a template ──────
#
# `just test` runs the suite with `-n auto --dist loadgroup` (the speed pass): the
# time-window cleanup below is only safe when concurrent tests cannot see each other's rows, so
# every xdist worker gets its own database - `prismoid_xt_gw3` - recreated fresh from the dev DB
# each run (a Postgres TEMPLATE clone is a file copy: fast, migrations and seeds carried).
# NOTIFY channels, sequences, and cleanup windows are all per-database, so workers cannot race.
#
# A developer's own ~/.prismoid/config must never reach a test: point the user-config path at nothing.
os.environ.setdefault("PRISMOID_CONFIG_FILE", "/nonexistent/prismoid-config")

# These blocks run at MODULE IMPORT, before any `prismoid.*` import below, because `prismoid.conf`
# reads the env exactly once at its own import. A plain `pytest` (no xdist) has no
# PYTEST_XDIST_WORKER and keeps today's semantics: the dev DB (or the fresh one), directly.
if os.environ.get("PRISMOID_TEST_FRESH_DB") == "1" and not os.environ.get("PYTEST_XDIST_WORKER"):
    build_fresh_template()

if worker := os.environ.get("PYTEST_XDIST_WORKER"):
    import asyncpg  # the driver the app already ships; no new dependency

    _base = os.environ.get("PRISMOID_WORKER_DATABASE_URL", DEFAULT_URL)
    _root, _, _dbname = _base.rpartition("/")
    _clone = f"{_dbname}_xt_{worker}"

    async def _bootstrap_worker_db() -> None:
        conn = await asyncpg.connect(f"{_root}/postgres")
        try:
            await conn.execute(f'drop database if exists "{_clone}" with (force)')
            for attempt in range(10):
                try:
                    await conn.execute(f'create database "{_clone}" template "{_dbname}"')
                    return
                except asyncpg.PostgresError as exc:
                    # Concurrent clones serialize on the template's lock; a lingering dev
                    # connection blocks it entirely - say so instead of hanging.
                    if attempt == 9:
                        raise RuntimeError(
                            f"could not clone {_dbname!r} for worker {worker}: {exc} - close "
                            "connections to the dev DB (just stop; exit psql) and rerun"
                        ) from exc
                    await asyncio.sleep(0.3 * (attempt + 1))
        finally:
            await conn.close()

    asyncio.run(_bootstrap_worker_db())
    # Point BOTH process roles at the clone (`api_root` says how the API's server is found).
    _api_server = api_root(_root)
    # 127.0.0.1, never localhost: fourteen workers' parallel getaddrinfo storms macOS's
    # resolver into transient gaierrors (seen live on the first parallel run) - numeric hosts
    # skip DNS entirely. And a SMALL pool per worker: the defaults × 14 workers brush
    # Postgres's max_connections=200; 2+3 × 14 ≈ 70 leaves headroom for the clones and psql.
    os.environ["PRISMOID_WORKER_DATABASE_URL"] = f"{_root}/{_clone}".replace("localhost", "127.0.0.1")
    os.environ["PRISMOID_API_DATABASE_URL"] = f"{_api_server}/{_clone}".replace("localhost", "127.0.0.1")
    os.environ.setdefault("PRISMOID_API_DB_POOL_SIZE", "2")
    os.environ.setdefault("PRISMOID_API_DB_MAX_OVERFLOW", "3")

import pytest
from sqlalchemy import text

from prismoid.db.engine import dispose_engine, get_engine
from prismoid.db.session import get_sessionmaker, session_scope
from tests.support.dispatchers import RecordingDispatcher, SilentDispatcher

# (table, its timestamp column). Deleted in FK order: tasks reference actors with ON DELETE
# RESTRICT (attribution outlives the profile), so the database refuses to do it the other way
# round - and it is right to.
#
# `usage_events.at` is not `created_at`, and that is deliberate: a ledger row's timestamp is *the
# moment of spend*, not the moment the row was written. Assuming one column name across every table
# is exactly the kind of thing that works until the one table where it matters.
_WORKSPACE_OWNED = (  # children first: conversations/bots/tasks before the projects they may hang off
    "skills_registry",
    "bot_template_installs",
    "plugin_installs",
    "inbox_items",
    "captures",
    "api_keys",
    "conversations",
    "bot_channels",
    "bots_registry",
    "connectors",
    "egress_allowances",
    "memories",
    "routines",
    "machines",
    "tasks",
    "project_invites",
    "projects",
)
_SCOPED_TABLES = (
    ("artifacts", "created_at"),
    ("usage_events", "at"),
    ("project_usage_daily", None),  # no timestamp; scoped by the projects it hangs off
    ("plan_steps", "created_at"),
    # Before tasks/projects/actors: created_by_actor_id is RESTRICT, so a leftover memory would
    # wedge the actor delete below it. Uses before memories (FK), memories before tasks.
    ("memory_uses", "created_at"),
    ("memories", "created_at"),
    # Before actors: owner_actor_id CASCADEs, but the delete order still runs children first
    # so a scoped actor's connectors never surprise a FK below.
    ("connectors", "created_at"),
    # Before actors: owner_actor_id CASCADEs, same reasoning as connectors above.
    ("egress_allowances", "created_at"),
    # Before actors: admin_actions.admin_actor_id is a plain FK (the audit trail is append-only
    # and actors are never deleted in prod - but a test's actors ARE, and their audit rows must
    # go first or the actor delete below wedges on the constraint).
    ("admin_actions", "created_at"),
    # Runs before routines (FK CASCADE would do it, but explicit beats cascade here), and both
    # before tasks/actors: tasks.origin_routine_id is SET NULL, routines.owner is RESTRICT.
    ("routine_runs", "created_at"),
    ("routines", "created_at"),
    ("tasks", "created_at"),
    ("projects", "created_at"),
    # Every tenant root a workspace owns goes before the workspace (workspace_id is RESTRICT); a "workspace_id" column
    # means "rows of the workspaces this test created".
    *((table, "workspace_id") for table in _WORKSPACE_OWNED),
    # Before actors: workspaces.created_by_actor_id is RESTRICT; the ledger is already gone.
    ("workspace_invites", "created_at"),
    ("workspace_members", "created_at"),
    ("workspaces", "created_at"),
    ("actors", "created_at"),
    # A pushed draft is a row; a test that pushes one must not leave it behind. Scoped by created_at,
    # so the twelve builtins (seeded long before any test starts) are never in the blast radius.
    ("skills_registry", "created_at"),
    # A test that adds a model to the catalog must not leave it (add_model conflicts on re-add). Prices
    # before models, so a price row never outlives the model it prices. Seeded models are old, and safe.
    ("price_book", "created_at"),
    ("model_catalog", "created_at"),
)


# Marks that run only when a `-m` expression names them: a command-line `-m` replaces addopts' whole
# `-m`, so CI's `-m 'not perf and not frames'` would otherwise select a paid `live` call.
_OPT_IN: tuple[str, ...] = ("live", "drill")


@pytest.hookimpl(tryfirst=True)  # before `-m` deselects, so `-m 'not frames'` sees the mark
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A test in a `frames-*` xdist group spins real frames: Docker, the frame images it builds, and
    the compose egress proxy. It gets the `frames` mark here, derived from the group, so CI's PR job
    can leave the whole set to its nightly with `-m 'not frames'` and no hand-kept list. A `live` or
    `drill` test is deselected unless the `-m` expression names its mark."""
    for item in items:
        group = item.get_closest_marker("xdist_group")
        name = (group.args[0] if group.args else group.kwargs.get("name", "")) if group else ""
        if str(name).startswith("frames"):
            item.add_marker(pytest.mark.frames)
    named = str(config.getoption("markexpr") or "")
    shut = [mark for mark in _OPT_IN if mark not in named]
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (dropped if any(item.get_closest_marker(mark) for mark in shut) else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def pytest_runtest_setup(item: pytest.Item) -> None:
    """`just device-proof`: a device test this machine cannot run is a failure, never a skip."""
    if (unmet := item.get_closest_marker("must_run")) is not None:
        pytest.fail(f"PRISMOID_DEVICE_PROOF: {unmet.args[0]}", pytrace=False)


@pytest.fixture(autouse=True, scope="session")
async def one_engine_per_session() -> AsyncIterator[None]:
    """One pool for the whole run (the speed pass - was `fresh_engine_per_test`).

    The per-test dispose existed because pytest-asyncio's per-FUNCTION loops left the cached
    pool bound to a dead loop by test two. The loop scope is `session` now (pyproject), so the
    pool and the loop live and die together - rebuilding the engine ~450 times bought nothing
    but wall-clock. Dispose once at the end so the process exits clean."""
    yield
    await dispose_engine()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(autouse=True)
async def scratch(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Delete what this test made, and nothing else.

    Only for `@pytest.mark.integration` - a unit test has no database, and making 99 of them open
    a connection to clean up rows they never wrote turns a 1-second suite into a 40-second one.
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    started = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    yield

    async with session_scope() as session:
        for table, column in _SCOPED_TABLES:
            if column is None:
                await session.execute(
                    text(
                        f"delete from {table} where project_id in "
                        "(select id from projects where created_at >= :started)"
                    ),
                    {"started": started},
                )
            elif column == "workspace_id":
                await session.execute(
                    text(
                        f"delete from {table} where workspace_id in "
                        "(select id from workspaces where created_at >= :started)"
                    ),
                    {"started": started},
                )
            else:
                await session.execute(
                    text(f"delete from {table} where {column} >= :started"), {"started": started}
                )
        await session.commit()


# ── shared dispatch doubles + row factories (shared, not copy-pasted per file) ──


@pytest.fixture(autouse=True)
async def fresh_burst_windows(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """The auth doors' buckets (login, reset, signup) are rows every test shares: one test's
    signups would 429 a later test's. After each integration test the buckets it opened or failed
    in are deleted, and nothing else, so a run never empties a developer's own buckets. The start
    is read from the database's clock, the one the buckets are stamped with. A unit test has no
    database and no buckets."""
    if request.node.get_closest_marker("integration") is None:
        yield
        return
    async with session_scope() as session:
        started = await session.scalar(text("select now()"))
    yield
    async with session_scope() as session:
        await session.execute(
            text("delete from auth_throttles where window_start >= :started or failed_at >= :started"),
            {"started": started},
        )
        await session.commit()


@pytest.fixture(autouse=True)
def fresh_read_caches() -> None:
    """The read caches (catalog, routing order) are process-global with a small
    TTL - under xdist a cached read from one test could serve the next test's differently
    seeded catalog. Cleared per test; the caches' correctness under TTL is their own unit's
    business."""
    from prismoid.operations import models as model_ops
    from prismoid.operations import routing as routing_ops

    model_ops.invalidate_catalog_cache()
    routing_ops.invalidate_order_cache()


@pytest.fixture
def recording_dispatcher():
    from prismoid.operations import dispatch

    d = RecordingDispatcher()
    dispatch.set_dispatcher(d)
    yield d
    dispatch.set_dispatcher(dispatch.NullDispatcher())


@pytest.fixture
def silent_dispatcher():
    from prismoid.operations import dispatch

    dispatch.set_dispatcher(SilentDispatcher())
    yield
    dispatch.set_dispatcher(dispatch.NullDispatcher())


@pytest.fixture
def push_on():
    """Push is on with a fake sender (no network, nothing leaves the test): the push device routes
    answer and the inbox asks for pushes. Without it a server has push off, as CI does."""
    from prismoid import fcm
    from tests.support.fakes.push_sender import FakePushSender

    sender = FakePushSender()
    fcm.install(sender)
    yield sender
    fcm.install(None)


@pytest.fixture
def fake_frames(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[Any]]:
    """Every frame is a FakeFrame: the Machine's policy, tested without Docker."""
    from prismoid.conf import settings
    from tests.support.fakes.fake_frames import FakeFrame, install

    # Cleared on the way in too: the provider's inventory is class state, and a test that built a
    # FakeFrame without this fixture must not leak its frames into the next one's assertions.
    FakeFrame.world.clear()
    FakeFrame.created.clear()
    FakeFrame.made.clear()
    FakeFrame.destroyed.clear()
    FakeFrame.unreachable = False
    install(monkeypatch)
    # A fake browser needs no Chrome (`execution._check_chrome`, on a device task): any executable.
    monkeypatch.setattr(settings.local, "chrome_path", sys.executable)
    yield FakeFrame
    FakeFrame.world.clear()
    FakeFrame.created.clear()
    FakeFrame.made.clear()
    FakeFrame.destroyed.clear()
    FakeFrame.unreachable = False
