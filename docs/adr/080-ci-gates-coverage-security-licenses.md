# ADR-080 · CI runs the integration suite, measures coverage, and gates security and licenses

**Status:** accepted · 2026-11-04 · the owner's ask to raise the repo's quality before it is open-sourced.

## Context

CI ran `just check` and sandbar's Go gate, nothing more. The integration and conformance suites
(about 1,800 tests over tenancy, steering, routines and the Machine) ran only on a developer's
laptop, against a template cloned from that developer's own dev database. Nothing measured
coverage. Nothing scanned the locked dependencies for known vulnerabilities, the history for
leaked secrets, or the dependency tree for licenses: an AGPL package entered the web app unnoticed.

## Decision

1. **Every job runs on Blacksmith** (`blacksmith-4vcpu-ubuntu-2404`).
2. **An `integration` job on every PR.** Postgres and MinIO as service containers on the compose
   ports, then the unit tests and the integration and conformance suites. `PRISMOID_TEST_FRESH_DB=1`
   makes `tests/conftest.py` build its template from nothing: a new database, `alembic upgrade
   head`, the model and skill seeds, and one account made with `prismoid admin bootstrap`. Every xdist
   worker clones that template, so no run depends on what a developer's database holds. A
   contributor without a dev stack runs the same recipe against any Postgres.
3. **Frame tests run nightly** (`integration-frames.yml`). A test in a `frames-*` xdist group gets
   the `frames` mark in the conftest, derived from the group; the PR job deselects it. These tests
   start real frames, build the frame images and need the compose egress proxy.
4. **Coverage.** `pytest-cov` drives coverage.py with branch coverage, one data file per process,
   `concurrency = ["greenlet", "thread"]` (SQLAlchemy's async engine runs ORM code in greenlets)
   and `patch = ["subprocess"]`. The job writes the combined unit and integration number to its
   summary and fails under `fail_under`, set to the measured number minus one. Raising it is a
   one-line change when coverage rises.
5. **sandbar** adds `staticcheck`, `go test -race` (the guest daemon's tests as root) and `govulncheck`, at exact versions.
6. **A `security` job**: `pip-audit` over `uv export` (the hashed lockfile, nothing installed),
   `pnpm audit --prod --audit-level high`, `gitleaks` over the commits a push or PR brings (a
   pinned binary, checksum-verified; a reviewed false positive carries an inline `gitleaks:allow` on its line), and the
   license gate.
7. **The license gate** (`backend/tools/license_gate.py`). Prismoid is distributed under
   AGPL-3.0-only, so every installed Python package (`pip-licenses`) and production JS package
   (`pnpm licenses`) must carry a license the gate knows to be compatible with that: permissive
   licenses, LGPL, MPL-2.0, and the GPL-3.0 family. A known-incompatible license fails with its
   name (proprietary or missing, SSPL, BUSL, Elastic, Commons Clause, non-commercial or
   no-derivatives terms, GPL-2.0-only), and so does a license string the gate does not recognise,
   so a person reads it instead of it passing silently. An `A OR B` expression passes when either
   side does; `A AND B` only when both do. A reviewed exception is an allowlist line naming the
   package and the reason; the first is `pypdfium2`, whose license field also says "dependency
   licenses".
8. **Warnings that mean bugs fail tests**: `RuntimeWarning` (a coroutine never awaited), unraisable
   exceptions and unhandled thread exceptions.

## Consequences

- `govulncheck` reports the Go standard library of the toolchain the module names, so the sandbar
  job is red while `sandbar/go.mod` names Go 1.25, which no longer receives fixes (go1.26.10 is
  clean). staticcheck and govulncheck need Go 1.26 to build; the `go` command switches to it.
- The official `minio/minio` images are gone from Docker Hub, so CI uses the archived
  `bitnamilegacy/minio`, which also creates the buckets (a service container cannot run an init
  step). `deploy/compose.yaml` still names the old image.
- The integration job costs a runner for most of an hour per PR; the frame tests' cost moves to a
  nightly, where a failure is found a day later rather than at review.
