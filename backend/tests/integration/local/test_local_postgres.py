# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The embedded Postgres (ADR-077): a private cluster on a Unix socket, nothing on the network."""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import psycopg
import pytest
from sqlalchemy.engine import make_url

from prismoid.local.postgres import Cluster, PgUrls
from tests.support.paths import BACKEND

pytestmark = pytest.mark.skipif(
    not Cluster.available(), reason="no embedded Postgres binaries for this platform"
)


@pytest.fixture
def home() -> Iterator[Path]:
    # a short path: pytest's tmp_path can push the socket past the 104-byte limit on its own
    h = Path(f"/tmp/prismoid-t-{secrets.token_hex(4)}")
    h.mkdir(mode=0o700)
    yield h
    Cluster(h).stop()
    subprocess.run(["rm", "-rf", str(h)], check=False)


@pytest.fixture
def started(home: Path) -> Iterator[tuple[Cluster, PgUrls]]:
    c = Cluster(home)
    c.ensure("pw-" + secrets.token_hex(4))
    yield c, c.start()


async def test_ensure_then_start_connects_over_socket(started: tuple[Cluster, PgUrls]) -> None:
    c, urls = started
    assert c.running()
    url = make_url(urls.async_url)
    conn = await asyncpg.connect(
        user=url.username, password=url.password, database=url.database, host=url.query["host"]
    )
    try:
        assert await conn.fetchval("select 1") == 1
        assert await conn.fetchval("show listen_addresses") == ""
    finally:
        await conn.close()
    with psycopg.connect(urls.sync_url) as sync:
        assert sync.execute("select 2").fetchone() == (2,)
    assert oct((c.home / "pg.pass").stat().st_mode & 0o777) == "0o600"


def test_second_ensure_reuses_cluster(started: tuple[Cluster, PgUrls]) -> None:
    c, urls = started
    with psycopg.connect(urls.sync_url, autocommit=True) as db:
        db.execute("create table keep (x int)")
    c.stop()
    assert not c.running()
    c.ensure("a-different-password")  # must not re-init, nor change the password
    again = c.start()
    assert again == urls
    with psycopg.connect(again.sync_url) as db:
        assert db.execute("select count(*) from keep").fetchone() == (0,)


def test_pg_hba_has_no_trust(started: tuple[Cluster, PgUrls]) -> None:
    c, _ = started
    lines = [
        ln.split("#")[0].strip() for ln in (c.home / "postgres" / "pg_hba.conf").read_text().splitlines()
    ]
    rules = [ln for ln in lines if ln]
    assert rules and all("trust" not in r for r in rules)
    assert all(r.split()[0] == "local" for r in rules)
    assert all(r.endswith("scram-sha-256") for r in rules)


def test_no_tcp_listener(started: tuple[Cluster, PgUrls]) -> None:
    c, _ = started
    pid = int((c.home / "postgres" / "postmaster.pid").read_text().splitlines()[0])
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-i"], capture_output=True, text=True, check=False
    ).stdout
    assert out.strip() == "", out


def test_stale_pid_file_does_not_fool_running(home: Path) -> None:
    c = Cluster(home)
    c.ensure("pw")
    (home / "postgres" / "postmaster.pid").write_text("999999\n")
    assert not c.running()
    c.start()  # a stale pid file never blocks a start
    assert c.running()
    c.stop()
    assert not c.running()


def test_socket_dir_fits_the_default_home_and_falls_back_when_long() -> None:
    default = Path("~/Library/Application Support/Prismoid").expanduser()
    assert Cluster(default).socket_dir == default / "run"
    long_home = Path("/tmp") / ("x" * 120)
    d = Cluster(long_home).socket_dir
    assert len(str(d / ".s.PGSQL.5432").encode()) < 100
    assert str(d).startswith("/tmp/prismoid-")


def test_dbos_accepts_socket_url(started: tuple[Cluster, PgUrls]) -> None:
    _, urls = started
    from prismoid.jobs import durable

    durable.init(name="prismoid-c1", database_url=urls.sync_url, executor_id=None, version="t")
    try:
        durable.launch()
    finally:
        durable.shutdown()
    with psycopg.connect(urls.sync_url) as db:
        row = db.execute(
            "select count(*) from information_schema.schemata where schema_name='dbos'"
        ).fetchone()
    assert row == (1,)


def test_alembic_upgrade_head_over_the_socket_url(started: tuple[Cluster, PgUrls]) -> None:
    _, urls = started  # the `%2F` in host= must survive alembic's config; pins the PG18 schema
    env = {**os.environ, "PRISMOID_API_DATABASE_URL": urls.async_url}
    r = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BACKEND, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    with psycopg.connect(urls.sync_url) as db:
        assert db.execute("select to_regclass('public.tasks')").fetchone() != (None,)


def test_crash_between_initdb_and_config_leaves_no_cluster(home: Path) -> None:
    # the old order: initdb wrote PG_VERSION, then died before the socket-only config
    c = Cluster(home)
    stale = home / "postgres.tmp"
    c._run("initdb", "-D", str(stale), "-U", "prismoid", "--auth=trust")  # pyright: ignore[reportPrivateUsage]
    assert (stale / "PG_VERSION").exists() and not (home / "postgres").exists()
    c.ensure("pw")  # sees no cluster, discards the stale tmp, builds a complete one
    assert not stale.exists()
    conf = (home / "postgres" / "postgresql.conf").read_text()
    assert "listen_addresses = ''" in conf
    assert "trust" not in (home / "postgres" / "pg_hba.conf").read_text()
    c.start()
    pid = int((home / "postgres" / "postmaster.pid").read_text().splitlines()[0])
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-i"], capture_output=True, text=True, check=False
    ).stdout
    assert out.strip() == ""


def test_pg_pass_is_0600_even_if_the_file_pre_existed(home: Path) -> None:
    (home / "pg.pass").write_text("old")
    (home / "pg.pass").chmod(0o644)
    Cluster(home).ensure("pw")
    assert (home / "pg.pass").stat().st_mode & 0o777 == 0o600
