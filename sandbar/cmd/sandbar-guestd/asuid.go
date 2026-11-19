// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

package main

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"runtime"

	"golang.org/x/sys/unix"
)

// asUID runs fn as uid (and gid = uid, the agent's layout), never as root. The daemon stays root:
// the switch is one OS thread's. fn runs in a goroutine locked to its own OS thread, which
// becomes a real uid:gid thread (groups cleared, then setresgid and setresuid, all RAW syscalls:
// unlike the libc and unix.Setres* wrappers they act on the calling thread alone), so the kernel
// checks every path fn touches as that uid and the thread holds none of root's capabilities. The
// goroutine never unlocks, so the thread is discarded with it and no other goroutine can ever run
// on it as another uid. A uid the process already holds, or root's own, is no switch.
func asUID(uid int, fn func() error) error {
	if uid == 0 || uid == os.Geteuid() {
		return fn()
	}
	done := make(chan error, 1)
	go func() {
		runtime.LockOSThread() // deliberately never unlocked: see above
		if err := dropThread(uid); err != nil {
			done <- err
			return
		}
		done <- fn()
	}()
	return <-done
}

// dropThread makes the calling thread uid:uid with no supplementary groups, and proves it took.
// Its errors are worded with %v, never wrapped: a failed drop (no CAP_SETUID) is the daemon's
// fault, not a permission refusal of the path.
func dropThread(uid int) error {
	if _, _, e := unix.RawSyscall(unix.SYS_SETGROUPS, 0, 0, 0); e != 0 {
		return fmt.Errorf("drop credentials: setgroups: %v", e)
	}
	id := uintptr(uid)
	if _, _, e := unix.RawSyscall(unix.SYS_SETRESGID, id, id, id); e != 0 {
		return fmt.Errorf("drop credentials: setresgid: %v", e)
	}
	if _, _, e := unix.RawSyscall(unix.SYS_SETRESUID, id, id, id); e != 0 {
		return fmt.Errorf("drop credentials: setresuid: %v", e)
	}
	ru, eu, su := unix.Getresuid()
	rg, eg, sg := unix.Getresgid()
	if ru != uid || eu != uid || su != uid || rg != uid || eg != uid || sg != uid {
		return fmt.Errorf("drop credentials: could not become uid %d", uid)
	}
	return nil
}

// errSymlink is a refusal to follow a link, which is the workload's own link meeting a rule.
var errSymlink = errors.New("refusing to follow a symbolic link")

// forbidden is a refusal the path's permissions or the no-link rule made.
func forbidden(err error) bool {
	return errors.Is(err, fs.ErrPermission) || errors.Is(err, unix.ELOOP) || errors.Is(err, errSymlink)
}
