// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

package firecracker

import "syscall"

// detachAttr puts firecracker in its own process group so a signal to the daemon's
// group (e.g. Ctrl-C in dev) does not tear down running VMs out from under Destroy.
func detachAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}
