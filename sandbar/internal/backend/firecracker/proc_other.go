// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build !linux

package firecracker

import "syscall"

// detachAttr is a no-op off Linux. The firecracker backend refuses to construct on a
// non-KVM host anyway (New checks /dev/kvm), so this only exists to let the package
// COMPILE on a developer's macOS machine - the code paths that use it never run there.
func detachAttr() *syscall.SysProcAttr { return &syscall.SysProcAttr{} }
