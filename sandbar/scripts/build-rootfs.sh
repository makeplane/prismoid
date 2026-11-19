#!/usr/bin/env bash
# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Build a Firecracker rootfs (ext4) with sandbar-guestd inside.
#
# Produces <SANDBAR_FC_ROOTFS_DIR>/<name>.ext4 - a minimal Alpine rootfs carrying:
#   /sbin/sandbar-guestd   the in-VM agent (cross-compiled for linux/amd64 by this script)
#   /sbin/sandbar-init     PID 1: mount pseudo-fs, spawn guestd, keep the VM alive
#   /task /work /artifacts /skills   the frame layout, /artifacts owned by uid 1000
#
# Run on a LINUX host (needs mkfs.ext4, a loop mount, root, docker, and the Go toolchain).
# macOS: build the daemon there, but build the rootfs on a Linux box - Firecracker is Linux-only.
#
# Usage: sudo ./scripts/build-rootfs.sh [name]     (default name: prismoid-frame)
set -euo pipefail

NAME="${1:-prismoid-frame}"
ROOTFS_DIR="${SANDBAR_FC_ROOTFS_DIR:-/var/lib/sandbar/rootfs}"
SIZE_MB="${SANDBAR_ROOTFS_SIZE_MB:-1024}"
BASE_IMAGE="${SANDBAR_ROOTFS_BASE:-alpine:3.20}"
AGENT_UID=1000

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOTFS_DIR}/${NAME}.ext4"
WORK="$(mktemp -d)"
trap 'umount "${WORK}/mnt" 2>/dev/null || true; rm -rf "${WORK}"' EXIT

echo "▶ building guestd (linux/amd64)"
( cd "${HERE}" && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
    go build -o "${WORK}/sandbar-guestd" ./cmd/sandbar-guestd )

echo "▶ exporting ${BASE_IMAGE} filesystem"
CID="$(docker create "${BASE_IMAGE}" /bin/true)"
docker export "${CID}" -o "${WORK}/base.tar"
docker rm "${CID}" >/dev/null

echo "▶ assembling ${OUT} (${SIZE_MB} MiB)"
mkdir -p "${ROOTFS_DIR}" "${WORK}/mnt"
dd if=/dev/zero of="${OUT}" bs=1M count="${SIZE_MB}" status=none
mkfs.ext4 -q -F "${OUT}"
mount -o loop "${OUT}" "${WORK}/mnt"

tar -xf "${WORK}/base.tar" -C "${WORK}/mnt"

# The agent + the frame layout.
install -m 0755 "${WORK}/sandbar-guestd" "${WORK}/mnt/sbin/sandbar-guestd"
mkdir -p "${WORK}/mnt"/{task,task/inputs,work,artifacts,skills}
# /artifacts and /work are the agent's; /task is the system's (read-only inputs live under it).
chown -R "${AGENT_UID}:${AGENT_UID}" "${WORK}/mnt/artifacts" "${WORK}/mnt/work"

# A guest user so exec's drop-to-1000 has a valid uid with a home and shell.
if ! grep -q ":${AGENT_UID}:" "${WORK}/mnt/etc/passwd" 2>/dev/null; then
  echo "agent:x:${AGENT_UID}:${AGENT_UID}:agent:/work:/bin/sh" >> "${WORK}/mnt/etc/passwd"
  echo "agent:x:${AGENT_UID}:" >> "${WORK}/mnt/etc/group"
fi

# PID 1: bring up the pseudo-filesystems guestd and workloads need, then run guestd. When guestd
# exits the VM powers off (panic=1 in the kernel cmdline turns that into a clean stop).
cat > "${WORK}/mnt/sbin/sandbar-init" <<'INIT'
#!/bin/sh
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sys /sys 2>/dev/null || true
mount -t devtmpfs dev /dev 2>/dev/null || true
mount -t tmpfs tmp /tmp 2>/dev/null || true
# vsock guest module is usually built-in on Firecracker kernels; load if modular.
modprobe vmw_vsock_virtio_transport 2>/dev/null || true
# The kernel hands PID 1 no PATH, and guestd's execs are plain `sh -c` (never a login shell that
# would read /etc/profile), so the PATH every exec inherits is set here, once.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec /sbin/sandbar-guestd
INIT
chmod 0755 "${WORK}/mnt/sbin/sandbar-init"

sync
umount "${WORK}/mnt"

echo "✓ ${OUT}"
echo "  run: sudo SANDBAR_BACKENDS=firecracker SANDBAR_BACKEND=firecracker \\"
echo "         SANDBAR_FC_KERNEL=/path/to/vmlinux sandbard"
echo "  then create a sandbox with spec.image = \"${NAME}\""
