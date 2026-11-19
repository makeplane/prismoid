#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Nightly backup: BOTH databases + the object store, encrypted, off-host, with retention.
#
# What the in-repo drill (backend/tools/backup_restore.sh) is not: that proves restorability of
# `prismoid` inside the container and deletes its own dump. This ships the real thing off the
# box: `prismoid` AND `prismoid_dbos_sys` (DBOS's own database - all in-flight workflow state;
# the drill never dumps it), the role definitions, and the MinIO volume. MinIO objects are
# content-addressed and immutable, so a file-level snapshot of the live volume is consistent
# for objects; restoring it pairs with the Silo release pinned in compose.prod.yaml (Silo keeps
# MinIO's on-disk format).
#
# Secrets ride /etc/prismoid-backup.env (root, 0600):
#   RESTIC_REPOSITORY=sftp:uXXXXX@uXXXXX.your-storagebox.de:/prismoid   # or an s3: R2 repo
#   RESTIC_PASSWORD=...                                                  # encrypts the repo
#   HEALTHCHECK_URL=https://hc-ping.com/<uuid>                           # optional dead-man ping
#
# Where things live (defaults match PROVISION.md; override in the same env file):
#   PRISMOID_SRV=/srv/prismoid              the checkout holding deploy/compose.prod.yaml and deploy/.env
#   PRISMOID_COMPOSE_PROJECT=prismoid           the compose project name (`name:` in compose.prod.yaml)
#   PRISMOID_MINIO_VOLUME=prismoid_miniodata    the MinIO data volume (default: <project>_miniodata)
#   PRISMOID_BACKUP_DIR=/var/backups/prismoid   where the dumps are staged before restic reads them
#
# Install: deploy/backup/install.sh (systemd timer, nightly 03:30). First run: restic init.
set -euo pipefail

SRV="${PRISMOID_SRV:-/srv/prismoid}"
PROJECT="${PRISMOID_COMPOSE_PROJECT:-prismoid}"
MINIO_VOLUME="${PRISMOID_MINIO_VOLUME:-${PROJECT}_miniodata}"
DUMPS="${PRISMOID_BACKUP_DIR:-/var/backups/prismoid}"
COMPOSE=(docker compose -p "$PROJECT" -f "$SRV/deploy/compose.prod.yaml" --env-file "$SRV/deploy/.env")

# The dumps are the whole database in plaintext: readable by root alone while they exist.
umask 077
mkdir -p "$DUMPS"
chmod 700 "$DUMPS"

echo "backup: dumping prismoid + prismoid_dbos_sys + globals"
"${COMPOSE[@]}" exec -T postgres pg_dump -U prismoid -Fc -d prismoid >"$DUMPS/prismoid.dump"
"${COMPOSE[@]}" exec -T postgres pg_dump -U prismoid -Fc -d prismoid_dbos_sys >"$DUMPS/prismoid_dbos_sys.dump"
"${COMPOSE[@]}" exec -T postgres pg_dumpall -U prismoid --globals-only >"$DUMPS/globals.sql"

echo "backup: restic snapshot (dumps + miniodata when self-hosted)"
# The MinIO volume exists only in self-hosted object-store mode (COMPOSE_PROFILES=minio). On a
# managed store (DO Spaces) the objects' durability is the provider's story - content-addressed,
# immutable keys - and the nightly snapshot is the two databases + globals alone.
TARGETS=("$DUMPS")
VOLUME=$(docker volume inspect -f '{{.Mountpoint}}' "$MINIO_VOLUME" 2>/dev/null || true)
[[ -n "$VOLUME" && -d "$VOLUME" ]] && TARGETS+=("$VOLUME")
restic backup --tag nightly "${TARGETS[@]}"

# The snapshot holds them now, encrypted; a plaintext copy left on the box is only a second place
# to leak the database from. A failed upload stops above (set -e) and keeps them for the retry.
rm -f "$DUMPS/prismoid.dump" "$DUMPS/prismoid_dbos_sys.dump" "$DUMPS/globals.sql"

restic forget --tag nightly --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune

if [[ -n "${HEALTHCHECK_URL:-}" ]]; then
    curl -fsS --max-time 10 "$HEALTHCHECK_URL" >/dev/null || true
fi
echo "backup: done"
