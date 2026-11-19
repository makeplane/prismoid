// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package firecracker is sandbar's EXPERIMENTAL backend: one Firecracker microVM per
// sandbox, driven over Firecracker's own HTTP-over-unix API, with an in-VM agent
// (sandbar-guestd) reached over AF_VSOCK. It is not a shipping backend: it has no egress, no
// display route, no state or restart, no adoption across a daemon restart, and no automated test
// tier; the docker backend is the one sandbar ships. Run it to evaluate it, not to serve users.
//
// Why a microVM and not a container: a container shares the host kernel, so a kernel
// exploit from workload code is a host compromise and every co-tenant sandbox falls
// with it. Firecracker gives each sandbox its own kernel in a KVM guest with a
// ~5 MiB VMM and ~125 ms boot - the isolation of a VM at the density of a container.
// This is what every serious agent platform (E2B, Fly, Cloudflare, AWS Lambda
// MicroVMs) runs untrusted model-generated code inside, and it is where sandbar is headed.
//
// Requirements (documented in docs/firecracker.md): Linux host with /dev/kvm, the
// `firecracker` binary on PATH, a guest kernel (vmlinux) and a rootfs image built by
// scripts/build-rootfs.sh with sandbar-guestd baked in as the init-spawned agent.
package firecracker

import "os"

// Config is the firecracker backend's settings, all from SANDBAR_FC_* env vars.
type Config struct {
	// BinPath is the firecracker executable. Default "firecracker" (PATH lookup).
	BinPath string
	// KernelPath is the guest kernel image (vmlinux). Required on Linux.
	KernelPath string
	// RootfsDir holds rootfs template images by name: <RootfsDir>/<image>.ext4.
	// A sandbox's Spec.Image selects the template; each VM gets a COPY (reflink
	// where the filesystem supports it) so the template stays pristine.
	RootfsDir string
	// RunDir is where per-VM state lives (api socket, vsock socket, rootfs copy,
	// firecracker config). One subdir per sandbox id; removed on Destroy.
	RunDir string
	// KernelArgs is the guest boot cmdline. The default gives a quiet serial console
	// and hands PID 1 to our init shim.
	KernelArgs string
	// BootTimeoutS bounds how long Create waits for guestd to answer on vsock.
	BootTimeoutS float64
}

// ConfigFromEnv reads SANDBAR_FC_* with sane defaults.
func ConfigFromEnv() Config {
	return Config{
		BinPath:      env("SANDBAR_FC_BIN", "firecracker"),
		KernelPath:   os.Getenv("SANDBAR_FC_KERNEL"),
		RootfsDir:    env("SANDBAR_FC_ROOTFS_DIR", "/var/lib/sandbar/rootfs"),
		RunDir:       env("SANDBAR_FC_RUN_DIR", "/var/lib/sandbar/run"),
		KernelArgs:   env("SANDBAR_FC_KERNEL_ARGS", "console=ttyS0 reboot=k panic=1 pci=off i8042.noaux init=/sbin/sandbar-init"),
		BootTimeoutS: envF("SANDBAR_FC_BOOT_TIMEOUT_S", 10),
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envF(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		var f float64
		if _, err := sscan(v, &f); err == nil {
			return f
		}
	}
	return def
}
