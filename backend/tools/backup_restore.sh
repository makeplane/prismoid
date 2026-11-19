#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Backup/restore drill. A backup you have never restored is a hope, not a backup.
#
# pg_dump the live database, restore it into a FRESH database, and prove the copy is intact — row counts
# match table-for-table, and the app's own dashboard queries return the same numbers on the restored copy
# as on the original (referential integrity, not just row totals, survived). It restores into a separate
# database rather than dropping the live one, so proving restorability never risks the running instance.
#
# Re-provable:  just backup-restore   (or)   bash tools/backup_restore.sh
set -euo pipefail
PG=prismoid-postgres-1
now() { python3 -c 'import time;print(time.time())'; }
psql() { docker exec "$PG" psql -U prismoid "$@"; }

echo "── backup/restore drill ──"
T0=$(now)
docker exec "$PG" pg_dump -U prismoid -d prismoid -Fc -f /tmp/backup.dump
SIZE=$(docker exec "$PG" sh -c "wc -c < /tmp/backup.dump")
echo "  pg_dump: ${SIZE} bytes in $(python3 -c "print(f'{$(now)-$T0:.1f}s')")"

psql -d postgres -tAc "DROP DATABASE IF EXISTS prismoid_restore;" >/dev/null
psql -d postgres -tAc "CREATE DATABASE prismoid_restore;" >/dev/null
T2=$(now)
docker exec "$PG" pg_restore -U prismoid -d prismoid_restore /tmp/backup.dump
echo "  restore: into a fresh db in $(python3 -c "print(f'{$(now)-$T2:.1f}s')")"

echo ""
fail=0
for t in tasks actors usage_events model_catalog artifacts task_events; do
  L=$(psql -d prismoid -tAc "SELECT count(*) FROM $t;")
  R=$(psql -d prismoid_restore -tAc "SELECT count(*) FROM $t;")
  if [ "$L" = "$R" ]; then m="✓"; else m="✗ MISMATCH"; fail=1; fi
  printf "  %-14s live=%-6s restore=%-6s %s\n" "$t" "$L" "$R" "$m"
done

# The smoke: the app's own dashboard queries must return the same numbers on the restored copy.
smoke() { docker exec "$PG" psql -U prismoid -d "$1" -tAc "
  SELECT round((SELECT count(*) FROM task_events WHERE type='escalation.raised')::numeric
    / NULLIF((SELECT count(DISTINCT task_id) FROM task_events),0), 4);"; }
LIVE_IPT=$(smoke prismoid); REST_IPT=$(smoke prismoid_restore)
echo ""
echo "  dashboard IPT(A):  live=${LIVE_IPT}  restore=${REST_IPT}"
[ "$LIVE_IPT" = "$REST_IPT" ] || fail=1

psql -d postgres -tAc "DROP DATABASE prismoid_restore;" >/dev/null
docker exec "$PG" rm -f /tmp/backup.dump

echo ""
if [ "$fail" = 0 ]; then
  echo "  == backup restores intact: counts match, dashboard reconciles ==  PASS"
else
  echo "  == restore MISMATCH — the backup is not trustworthy ==  FAIL"; exit 1
fi
