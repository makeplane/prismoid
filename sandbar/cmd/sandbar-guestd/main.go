// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

// Command sandbar-guestd is the agent that runs INSIDE a Firecracker microVM. It
// listens on AF_VSOCK and serves the guestwire protocol - exec, file I/O, tar
// archive/extract - for the host-side firecracker backend. It is baked into the rootfs
// by scripts/build-rootfs.sh and spawned by the init shim (/sbin/sandbar-init).
//
// It is deliberately tiny and defensive: it runs as the most privileged thing in the
// guest (so it can chown files to either uid), it trusts only the vsock stream from
// its own host VMM (there is no network in the VM), and every operation is bounded.
package main

import (
	"log"
	"os"
	"runtime"

	"golang.org/x/sys/unix"
)

// init pins the main goroutine to the process's first thread for life. That thread is the
// thread-group leader (PID 1 in the VM), and Go never ends it: a goroutine that dropped to uid 1000
// (asUID) and exited on it would leave the leader uid 1000, signalable and limitable by the
// workload. Locked here, no other goroutine can run on it.
func init() { runtime.LockOSThread() }

// port must match firecracker/vsock.go's guestPort.
const port = 52

func main() {
	log.SetOutput(os.Stderr)
	log.SetFlags(log.Ltime | log.Lmicroseconds)

	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		log.Fatalf("vsock socket: %v", err)
	}
	// Bind to VMADDR_CID_ANY:port - accept from the host VMM on this guest.
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		log.Fatalf("vsock bind: %v", err)
	}
	if err := unix.Listen(fd, 64); err != nil {
		log.Fatalf("vsock listen: %v", err)
	}
	log.Printf("sandbar-guestd listening on vsock port %d", port)

	for {
		nfd, _, err := unix.Accept(fd)
		if err != nil {
			if err == unix.EINTR {
				continue
			}
			log.Printf("accept: %v", err)
			continue
		}
		// One request per connection; goroutine so a slow exec never blocks the next.
		go serve(nfd)
	}
}
