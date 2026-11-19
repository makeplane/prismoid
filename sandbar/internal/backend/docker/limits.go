// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// Limits are what bound one container sandbox beyond its spec's cpus and mem_mb: how many
// processes it may hold, how many files each may open, whether its root filesystem is writable,
// how much disk it may fill, and which OCI runtime runs it. The defaults are safe for any Engine;
// the opt-ins depend on the host (a storage cap needs a driver that supports one, a runtime must
// be installed), so an operator turns them on knowingly.
type Limits struct {
	// PidsLimit caps the processes and threads in the sandbox's cgroup, so a fork bomb stops at
	// the sandbox instead of the host. 0 means no cap.
	PidsLimit int64
	// NoFile is RLIMIT_NOFILE (soft and hard) for every process in the sandbox. 0 leaves the
	// Engine's default.
	NoFile int64
	// NProc is RLIMIT_NPROC. Unset (0) by default: the kernel counts it per uid across the whole
	// host, and every sandbox runs its workload as the same uid, so one busy sandbox would use up
	// every other sandbox's share. PidsLimit is the per-sandbox bound.
	NProc int64
	// ReadonlyRootfs makes the image's filesystem read-only; Tmpfs then names the writable paths.
	ReadonlyRootfs bool
	// Tmpfs maps a path to its mount options (e.g. "/tmp" to "rw,nosuid,size=1g"). The Engine's
	// archive routes do not see into a tmpfs, so the file routes cannot write below one.
	Tmpfs map[string]string
	// StorageSize caps the writable layer (the Engine's StorageOpt size, e.g. "20G"). Only some
	// storage drivers support it (overlay2 on xfs with pquota, btrfs, zfs); the Engine refuses
	// every create on one that does not. "" means no cap.
	StorageSize string
	// Runtime is the OCI runtime (e.g. "runsc" for gVisor, which gives each sandbox a kernel of its
	// own in user space). "" means the Engine's default (runc).
	Runtime string
}

// DefaultLimits bound every sandbox on any Engine: 4096 processes and threads (a browser holds a
// few hundred), 65536 open files per process.
func DefaultLimits() Limits {
	return Limits{PidsLimit: 4096, NoFile: 65536}
}

// defaultTmpfs is the one writable path a read-only sandbox gets when the operator names none.
var defaultTmpfs = map[string]string{"/tmp": "rw,nosuid,nodev,size=1g"}

// LimitsFromEnv reads the SANDBAR_DOCKER_* limits; an unset variable keeps its default.
//
//	SANDBAR_DOCKER_PIDS_LIMIT       processes and threads per sandbox (default 4096, 0 = no cap)
//	SANDBAR_DOCKER_NOFILE           open files per process (default 65536, 0 = the Engine's default)
//	SANDBAR_DOCKER_NPROC            RLIMIT_NPROC (default unset - it counts per uid across the host)
//	SANDBAR_DOCKER_READONLY_ROOTFS  "on" makes the image's filesystem read-only (default off)
//	SANDBAR_DOCKER_TMPFS            writable mounts, space-separated path:options entries
//	                                (default /tmp when the rootfs is read-only)
//	SANDBAR_DOCKER_STORAGE_SIZE     the writable layer's cap, e.g. 20G (default none)
//	SANDBAR_DOCKER_RUNTIME          the OCI runtime, e.g. runsc (default the Engine's)
func LimitsFromEnv(getenv func(string) string) (Limits, error) {
	l := DefaultLimits()
	for key, dst := range map[string]*int64{
		"SANDBAR_DOCKER_PIDS_LIMIT": &l.PidsLimit,
		"SANDBAR_DOCKER_NOFILE":     &l.NoFile,
		"SANDBAR_DOCKER_NPROC":      &l.NProc,
	} {
		raw := strings.TrimSpace(getenv(key))
		if raw == "" {
			continue
		}
		n, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || n < 0 {
			return Limits{}, fmt.Errorf("%s must be a whole number of at least 0, got %q", key, raw)
		}
		*dst = n
	}
	switch v := strings.ToLower(strings.TrimSpace(getenv("SANDBAR_DOCKER_READONLY_ROOTFS"))); v {
	case "", "off", "false", "0":
	case "on", "true", "1":
		l.ReadonlyRootfs = true
	default:
		return Limits{}, fmt.Errorf("SANDBAR_DOCKER_READONLY_ROOTFS must be on or off, got %q", v)
	}
	tmpfs, err := parseTmpfs(getenv("SANDBAR_DOCKER_TMPFS"))
	if err != nil {
		return Limits{}, err
	}
	if tmpfs == nil && l.ReadonlyRootfs {
		tmpfs = defaultTmpfs
	}
	l.Tmpfs = tmpfs
	l.StorageSize = strings.TrimSpace(getenv("SANDBAR_DOCKER_STORAGE_SIZE"))
	l.Runtime = strings.TrimSpace(getenv("SANDBAR_DOCKER_RUNTIME"))
	return l, nil
}

// parseTmpfs reads space-separated path:options entries (options may hold commas); nil when empty.
func parseTmpfs(raw string) (map[string]string, error) {
	fields := strings.Fields(raw)
	if len(fields) == 0 {
		return nil, nil
	}
	out := make(map[string]string, len(fields))
	for _, f := range fields {
		p, opts, _ := strings.Cut(f, ":")
		if !strings.HasPrefix(p, "/") {
			return nil, fmt.Errorf("SANDBAR_DOCKER_TMPFS entries are /path or /path:options, got %q", f)
		}
		out[p] = opts
	}
	return out, nil
}

// WithLimits sets the limits every sandbox is created with (default DefaultLimits).
func WithLimits(l Limits) Option {
	return func(b *Backend) { b.limits = l }
}

// hostConfig is a sandbox container's HostConfig: its network, its spec's cpu and memory with no
// swap beyond the memory, every capability dropped, no privilege gain, and the backend's limits.
func (l Limits) hostConfig(netMode string, spec core.Spec) map[string]any {
	mem := int64(spec.MemMB) << 20
	host := map[string]any{
		"NetworkMode": netMode,
		"AutoRemove":  false,
		"Memory":      mem,
		"MemorySwap":  mem, // equal to Memory: the sandbox gets no swap on top of its memory
		"NanoCpus":    int64(spec.CPUs) * 1_000_000_000,
		"CapDrop":     []string{"ALL"},
		"SecurityOpt": []string{"no-new-privileges"},
		// The Engine's init as pid 1 reaps what an exec orphans: `sleep infinity` never would, and
		// every zombie it left would hold one of the sandbox's PidsLimit slots for good.
		"Init": true,
	}
	if l.PidsLimit > 0 {
		host["PidsLimit"] = l.PidsLimit
	}
	var ulimits []map[string]any
	for name, n := range map[string]int64{"nofile": l.NoFile, "nproc": l.NProc} {
		if n > 0 {
			ulimits = append(ulimits, map[string]any{"Name": name, "Soft": n, "Hard": n})
		}
	}
	if len(ulimits) > 0 {
		host["Ulimits"] = ulimits
	}
	if l.ReadonlyRootfs {
		host["ReadonlyRootfs"] = true
	}
	if len(l.Tmpfs) > 0 {
		host["Tmpfs"] = l.Tmpfs
	}
	if l.StorageSize != "" {
		host["StorageOpt"] = map[string]string{"size": l.StorageSize}
	}
	if l.Runtime != "" {
		host["Runtime"] = l.Runtime
	}
	return host
}
