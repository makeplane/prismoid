#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Monthly restore drill against the OFF-HOST backup - a backup nobody has restored is a wish.
#
# Pulls the latest restic snapshot and proves each part of it restores:
#   - `prismoid`: restored into a scratch database, the six tables the in-repo drill checks counted;
#   - `prismoid_dbos_sys` (DBOS's workflow state): restored into a scratch database, its workflow
#     table counted, so a recovery would find the in-flight work it needs;
#   - the role definitions (globals.sql) are present and non-empty;
#   - the MinIO volume (self-hosted mode): a random sample of files read back from the snapshot,
#     each compared byte for byte with the live volume where the live file still exists.
# Then it drops the scratch databases and asks restic to re-read 5% of the repository's data
# blobs. Env: /etc/prismoid-backup.env (same as backup.sh, including its PRISMOID_* location overrides).
set -euo pipefail

SRV="${PRISMOID_SRV:-/srv/prismoid}"
PROJECT="${PRISMOID_COMPOSE_PROJECT:-prismoid}"
MINIO_VOLUME="${PRISMOID_MINIO_VOLUME:-${PROJECT}_miniodata}"
SAMPLE="${PRISMOID_RESTORE_SAMPLE:-5}"
SCRATCH=$(mktemp -d /tmp/prismoid-restore.XXXXXX)
COMPOSE=(docker compose -p "$PROJECT" -f "$SRV/deploy/compose.prod.yaml" --env-file "$SRV/deploy/.env")
PSQL=("${COMPOSE[@]}" exec -T postgres psql -U prismoid -d postgres -tAc)

cleanup() {
    rm -rf "$SCRATCH"
    "${PSQL[@]}" "drop database if exists prismoid_restore;" >/dev/null 2>&1 || true
    "${PSQL[@]}" "drop database if exists prismoid_dbos_sys_restore;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "restore-verify FAILED: $*" >&2
    exit 1
}

echo "restore-verify: pulling latest snapshot's dumps"
restic restore latest --target "$SCRATCH" --include '*/prismoid.dump' --include '*/prismoid_dbos_sys.dump' --include '*/globals.sql'
find_one() { find "$SCRATCH" -name "$1" -type f | head -1; }
DUMP=$(find_one prismoid.dump)
DBOS_DUMP=$(find_one prismoid_dbos_sys.dump)
GLOBALS=$(find_one globals.sql)
[[ -n "$DUMP" ]] || fail "no prismoid.dump in latest snapshot"
[[ -n "$DBOS_DUMP" ]] || fail "no prismoid_dbos_sys.dump in latest snapshot"
[[ -s "$GLOBALS" ]] || fail "no globals.sql (role definitions) in latest snapshot"

# restore_into DB DUMP: a fresh scratch database holding the dump.
restore_into() {
    "${PSQL[@]}" "drop database if exists $1;" >/dev/null
    "${PSQL[@]}" "create database $1;" >/dev/null
    "${COMPOSE[@]}" exec -T postgres pg_restore -U prismoid -d "$1" --no-owner <"$2"
}

count() { # count DB TABLE
    "${COMPOSE[@]}" exec -T postgres psql -U prismoid -d "$1" -tAc "select count(*) from $2;"
}

restore_into prismoid_restore "$DUMP"
for table in tasks actors usage_events model_catalog artifacts task_events; do
    echo "restore-verify: prismoid.$table = $(count prismoid_restore "$table") rows"
done

restore_into prismoid_dbos_sys_restore "$DBOS_DUMP"
echo "restore-verify: prismoid_dbos_sys.dbos.workflow_status = $(count prismoid_dbos_sys_restore dbos.workflow_status) rows"

# The object store, self-hosted mode only (a managed store has no volume in the snapshot). Objects
# are immutable, so a sampled file that still exists live must match it exactly; one deleted since
# the snapshot only has to read back. MinIO's own state (.minio.sys: config, usage caches) changes
# by the minute, so the sample is drawn from the buckets only.
VOLUME=$(docker volume inspect -f '{{.Mountpoint}}' "$MINIO_VOLUME" 2>/dev/null || true)
if [[ -n "$VOLUME" && -d "$VOLUME" ]]; then
    mapfile -t picked < <(restic ls --json --recursive latest "$VOLUME" |
        grep '"type":"file"' | grep -v '/\.minio\.sys/' | sed -n 's/.*"path":"\([^"]*\)".*/\1/p' | shuf -n "$SAMPLE")
    [[ ${#picked[@]} -gt 0 ]] || fail "no MinIO files under $VOLUME in latest snapshot"
    matched=0
    for path in "${picked[@]}"; do
        out="$SCRATCH/object"
        restic dump latest "$path" >"$out" || fail "could not read $path back from the snapshot"
        if [[ -f "$path" ]]; then
            cmp -s "$out" "$path" || fail "$path differs from the live volume"
            matched=$((matched + 1))
        fi
    done
    echo "restore-verify: miniodata sample = ${#picked[@]} files read back, $matched matched live byte for byte"
else
    echo "restore-verify: no MinIO volume ($MINIO_VOLUME) - managed object store, sample skipped"
fi

echo "restore-verify: restic check (5% data re-read)"
restic check --read-data-subset=5%
echo "restore-verify: PASS"
