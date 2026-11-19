# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The private Postgres Prismoid runs on when it is the only thing on the computer (ADR-077).

One cluster under `<home>/postgres`, reachable ONLY through a Unix socket (`listen_addresses=''`,
so there is no port to find), every login scram-sha-256 with the password in `<home>/pg.pass`
(0600), no `trust` line anywhere. The binaries ship in the `pixeltable-pgserver` wheel; this
module runs them, it does not use the package's own server wrapper (its port-and-trust model
is the opposite of ours).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import secrets
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import psycopg

USER = "prismoid"
DATABASE = "prismoid"
_SOCKET_FILE = ".s.PGSQL.5432"
# macOS allows ~104 bytes for a socket path; leave room for the file name and a margin.
_MAX_SOCKET_PATH = 100


# The wheel ships no contrib, and DBOS's first migration runs CREATE EXTENSION "uuid-ossp" (it
# never calls the function). Postgres 18 can search extra extension dirs, so the cluster gets a
# stand-in of that name whose one function is core's gen_random_uuid().
_UUID_OSSP_CONTROL = (
    "comment = 'stand-in for uuid-ossp (the embedded build ships no contrib; ADR-077)'\n"
    "default_version = '1.1'\nrelocatable = true\n"
)
_UUID_OSSP_SQL = (
    "CREATE FUNCTION uuid_generate_v4() RETURNS uuid AS 'SELECT gen_random_uuid()' "
    "LANGUAGE sql VOLATILE PARALLEL SAFE;\n"
)


@dataclass(frozen=True)
class PgUrls:
    """The same database for both drivers: asyncpg (the API) and libpq/psycopg (DBOS)."""

    async_url: str
    sync_url: str


class Cluster:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.data = home / "postgres"
        self._pass_file = home / "pg.pass"

    @staticmethod
    def _bin() -> Path | None:
        spec = importlib.util.find_spec("pixeltable_pgserver")
        if spec is None or not spec.submodule_search_locations:
            return None
        b = Path(next(iter(spec.submodule_search_locations))) / "pginstall18" / "bin"
        return b if (b / "pg_ctl").exists() else None

    @classmethod
    def available(cls) -> bool:
        return cls._bin() is not None

    @property
    def socket_dir(self) -> Path:
        """`<home>/run`, unless that path is too long for a socket (the default home has a space
        and is long, but not that long) - then a short per-user dir under /tmp, checked 0700 and
        ours. It is derived from the home, so the same home always finds the same dir."""
        run = self.home / "run"
        if len(str(run / _SOCKET_FILE).encode()) <= _MAX_SOCKET_PATH:
            return run
        tag = hashlib.sha256(str(self.home).encode()).hexdigest()[:8]
        return Path(f"/tmp/prismoid-{os.getuid()}") / tag

    def _own_socket_dir(self) -> Path:
        d = self.socket_dir
        if d != self.home / "run":
            d.parent.mkdir(mode=0o700, exist_ok=True)
            pst = d.parent.lstat()
            if not stat.S_ISDIR(pst.st_mode) or pst.st_uid != os.getuid() or pst.st_mode & 0o077:
                raise RuntimeError(f"{d.parent} is not private to this user; refusing to use it")
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = d.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise RuntimeError(f"{d} is not a directory owned by this user; refusing to use it")
        if st.st_mode & 0o077:
            d.chmod(0o700)
        return d

    def _run(self, tool: str, *args: str) -> subprocess.CompletedProcess[str]:
        b = self._bin()
        if b is None:
            raise RuntimeError("the embedded Postgres binaries are not installed (pixeltable-pgserver)")
        return subprocess.run(
            [str(b / tool), *args], capture_output=True, text=True, check=False, timeout=120, env=_pg_env()
        )

    def ensure(self, password: str) -> None:
        """Create the cluster on first run; later calls change nothing (the stored password wins)."""
        self.home.mkdir(parents=True, exist_ok=True)
        sock = self._own_socket_dir()
        if (self.data / "PG_VERSION").exists():
            return
        fd = os.open(self._pass_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # a file left by an earlier run keeps its old mode otherwise
        with os.fdopen(fd, "w") as f:
            f.write(password)
        # Build beside, rename in: `PG_VERSION` appears at the end, so a crash mid-way leaves
        # no cluster (initdb's own default config listens on TCP) - only a stray tmp dir.
        tmp = self.data.with_name("postgres.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        r = self._run(
            "initdb", "-D", str(tmp), "-U", USER, f"--pwfile={self._pass_file}",
            "--auth=scram-sha-256", "-E", "UTF8", "--locale=C",
        )  # fmt: skip
        if r.returncode:
            raise RuntimeError(f"initdb failed: {r.stderr.strip()}")
        ext = self.home / "ext" / "extension"
        ext.mkdir(parents=True, exist_ok=True)
        (ext / "uuid-ossp.control").write_text(_UUID_OSSP_CONTROL)
        (ext / "uuid-ossp--1.1.sql").write_text(_UUID_OSSP_SQL)
        # the only way in is the socket, and it asks for the password
        (tmp / "pg_hba.conf").write_text("local all all scram-sha-256\n")
        with (tmp / "postgresql.conf").open("a") as conf:
            conf.write(
                "\nlisten_addresses = ''\n"
                f"unix_socket_directories = '{sock}'\n"
                "unix_socket_permissions = 0700\n"
                f"extension_control_path = '{self.home / 'ext'}:$system'\n"
                "password_encryption = 'scram-sha-256'\n"
            )
        tmp.rename(self.data)

    def password(self) -> str:
        return self._pass_file.read_text()

    def running(self) -> bool:
        """Ask the server, not `postmaster.pid`: a pid file outlives a crash."""
        if not (self.socket_dir / _SOCKET_FILE).exists():
            return False
        return self._run("pg_isready", "-h", str(self.socket_dir), "-U", USER).returncode == 0

    def start(self) -> PgUrls:
        if not self.running():
            r = self._run(
                "pg_ctl", "start", "-w", "-t", "60", "-D", str(self.data),
                "-l", str(self.home / "postgres.log"),
            )  # fmt: skip
            if r.returncode:
                raise RuntimeError(f"pg_ctl start failed: {r.stderr.strip() or r.stdout.strip()}")
        self._create_database()
        return self.urls()

    def urls(self) -> PgUrls:
        host = quote(str(self.socket_dir), safe="")
        auth = f"{USER}:{quote(self.password(), safe='')}"
        tail = f"@/{DATABASE}?host={host}"
        return PgUrls(
            async_url=f"postgresql+asyncpg://{auth}{tail}",
            sync_url=f"postgresql://{auth}{tail}",
        )

    def _create_database(self) -> None:
        with psycopg.connect(
            host=str(self.socket_dir), user=USER, password=self.password(),
            dbname="postgres", autocommit=True,
        ) as db:  # fmt: skip
            if db.execute("select 1 from pg_database where datname = %s", (DATABASE,)).fetchone():
                return
            db.execute(f'create database "{DATABASE}"')  # constant name, not input

    def stop(self) -> None:
        if not (self.data / "PG_VERSION").exists():
            return
        r = self._run("pg_ctl", "stop", "-m", "fast", "-w", "-t", "60", "-D", str(self.data))
        if r.returncode and self.running():
            raise RuntimeError(f"pg_ctl stop failed: {r.stderr.strip() or r.stdout.strip()}")


# What initdb, pg_ctl and the server they start may see of ours: never a model key, a database URL
# or a first-run password (`pg_ctl start` hands its whole environment to the postmaster for life).
_PG_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "TZ")


def _pg_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _PG_ENV_KEYS or k.startswith("LC_")}


def new_password() -> str:
    return secrets.token_urlsafe(24)
