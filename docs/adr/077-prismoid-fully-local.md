# ADR-077 · Prismoid fully local

**Status:** accepted · 2026-10-30 · the owner's ask of 2026-10-28 (Prismoid runs whole on one Mac, no
Docker, no MinIO) and the device-machine design of 2026-10-29 (unpublished).
**Changes** that design in the places named below. HANDOFF §3 gains the `pixeltable-pgserver` row.

## Context

Prismoid needs Postgres (rows, DBOS's journal), an object store (MinIO/S3) and a hosted-shaped set of
processes. For a person with one Mac that is Docker plus a port-forwarded stack. `prismoid up` replaces
it with one supervised process tree on the user's own computer, and the Mac is the Machine
(ADR-078). This ADR records the decisions the first task (C1) and the next ones (C2-C4) build on.

## Decision

1. **`PRISMOID_ENV=local`** is a fifth environment beside dev/test/staging/prod. It allows the console
   mailer (the staging/prod refusals of ADR-031 are unchanged) and requires
   `PRISMOID_WORKER_FRAME_DRIVER=device`. Its data home is `settings.local.home`
   (`~/Library/Application Support/Prismoid`): `postgres/`, `objects/`, `run/`, `pg.pass`, ...
2. **Postgres is embedded: `pixeltable-pgserver==0.6.0`.** It is the maintained fork of `pgserver`
   (itself a fork of `postgresql-wheel`). Checked 2026-10-30 on PyPI and in the wheel: 0.6.0
   (2026-07-14) ships `cp312` wheels for `macosx_11_0_arm64` and `macosx_10_13_x86_64`
   (also manylinux and Windows), each carrying Postgres **16.14 and 18.4**. Rejected: `pgserver`
   0.1.4 (June 2024, Postgres 16.2, unmaintained), `postgresql-wheel` (no macOS wheels, 14.x,
   2021), `pgembed` (arm64 only). Pinned to 0.6.0 over 0.7.x, which was a day old. We use only its
   binaries (`prismoid/local/postgres.py` runs `initdb`/`pg_ctl`/`pg_isready` itself) - its own server
   wrapper binds a TCP port and trusts local clients, the opposite of what we want.
   - **We run the Postgres 18 build.** The wheel ships no contrib, and DBOS's first migration runs
     `CREATE EXTENSION "uuid-ossp"` (it never calls the function). Postgres 18 can search extra
     extension directories (`extension_control_path`), so the cluster carries a stand-in
     `uuid-ossp` under `<home>/ext` whose one function is core's `gen_random_uuid()`. Live-proved:
     `durable.init`/`launch` migrate cleanly against it. Fallback if the wheel stops being fit:
     Homebrew's `postgresql@16` (it has real contrib); not built.
   - **Unix socket only.** `listen_addresses=''` (no port exists), `pg_hba.conf` is the single line
     `local all all scram-sha-256` (no `trust`), socket dir mode 0700. The password is generated on
     first run into `<home>/pg.pass` (0600).
   - **Socket path length.** macOS caps a socket path near 104 bytes. The socket dir is
     `<home>/run` (71 bytes with the file name at the default home), but if that path exceeds 100
     bytes it moves to `/tmp/prismoid-<uid>/<8 hex of sha256(home)>`, created 0700 and refused unless
     it is a real directory owned by the user.
   - **Both drivers over the socket.** `postgresql+asyncpg://prismoid:<pw>@/prismoid?host=<%-encoded dir>` and
     the same with `postgresql://` for libpq/psycopg (DBOS; HANDOFF §14).
   - `running()` asks the server (`pg_isready`), never `postmaster.pid`; `stop()` is
     `pg_ctl stop -m fast`.
3. **Storage: a filesystem backend** for `prismoid/storage.py` under `<home>/objects/<bucket>/`,
   content-addressed as today, selected by config; S3 stays the hosted backend. Files are
   served to the browser through a **signed blob door** (short-lived signed URLs the API checks,
   no object-store port). Named here so the door is on the audit's list.
4. **`prismoid up`** starts Postgres, runs Alembic, starts the worker and then the API (the API cannot
   boot before the worker has made `<db>_dbos_sys`), the device helper and the SPA, as one
   supervised foreground tree; `prismoid down` stops a backgrounded one; a second `up` attaches.
5. **First-run password replaces the one-time sign-in link.** The first
   `prismoid up` bootstraps one workspace and one owner; the owner sets a password and signs in
   normally. A one-time link would be a new unauthenticated auth door, and CLAUDE.md's list of
   those is closed.
6. **Machine prices are conf keyed by `driver.profile`** (the design named no price). The
   default carries `device.base=0` and `device.browser=0`: a computer you own costs nothing to
   run. An operator override of the price map must keep both keys or the device Machine is unpriced.

7. **`~/.prismoid/config` is for a person's Prismoid, never a deployed service.** It is read after `.env`
   unless `PRISMOID_ENV` says `staging` or `prod` (`conf.env_files`). That is asked of the real
   environment, before any settings exist, which is where the deploys set it (`compose.prod.yaml`):
   a `PRISMOID_ENV` set only in `.env` does not count. `PRISMOID_ENVIRONMENT`, its other spelling, does.
8. **A key file is what its file says now.** `blob.key` and `helper.key` are cached with an `lstat`
   fingerprint (inode, mtime, size), so one lstat per ask and no read. Delete `blob.key` and every
   link signed with it stops working at once, in the API and the worker alike, and a new key is made.

## Consequences

- A new dependency (one row in HANDOFF §3) of ~21 MB per platform; the schema needs no extension,
  so nothing Prismoid owns depends on contrib. The uuid-ossp stand-in is DBOS's alone and goes if
  DBOS does (ADR-069's port).
- Tests run a real cluster in a temp dir; they skip with a reason where the binaries are absent.
- The device-driver and `prismoid up` remain macOS-only; the embedded Postgres itself also works on
  Linux, which is why its tests run in CI.
