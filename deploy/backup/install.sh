#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Install the backup + restore-verify timers. Run as root, once, after /etc/prismoid-backup.env
# exists (see backup.sh header) and `restic init` has been run against the repository.
set -euo pipefail
cd "$(dirname "$0")"

command -v restic >/dev/null || { echo "install restic first: apt install restic" >&2; exit 1; }
[[ -f /etc/prismoid-backup.env ]] || { echo "create /etc/prismoid-backup.env first (0600, see backup.sh)" >&2; exit 1; }

# The units name the checkout's path; a box that keeps it elsewhere installs with PRISMOID_SRV set (and
# puts the same PRISMOID_SRV in /etc/prismoid-backup.env, which the scripts read).
SRV="${PRISMOID_SRV:-/srv/prismoid}"
for unit in prismoid-backup.service prismoid-restore-verify.service; do
    sed "s|/srv/prismoid|$SRV|g" "$unit" >"/etc/systemd/system/$unit"
done
cp prismoid-backup.timer prismoid-restore-verify.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now prismoid-backup.timer prismoid-restore-verify.timer
systemctl list-timers 'prismoid-*' --no-pager
