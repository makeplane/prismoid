// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"context"
	"testing"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

func envOf(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

// Every sandbox is created bounded: a process cap, an open-file cap, no swap beyond its memory, and
// the posture it always had - so a fork bomb or a memory balloon stops at the sandbox.
func TestEverySandboxIsCreatedBounded(t *testing.T) {
	spy := &engineSpy{}
	b := spy.backend()
	if _, err := b.Create(context.Background(), "sb-lim", core.Spec{Image: "x", MemMB: 512, CPUs: 1}); err != nil {
		t.Fatalf("create: %v", err)
	}
	h := spy.created(t).HostConfig
	mem := float64(512 << 20)
	if h["Memory"] != mem || h["MemorySwap"] != mem {
		t.Fatalf("memory %v with swap %v, want both %v", h["Memory"], h["MemorySwap"], mem)
	}
	if h["PidsLimit"] != float64(4096) || h["Init"] != true {
		t.Fatalf("want PidsLimit 4096 and an init that reaps orphans, got %v %v", h["PidsLimit"], h["Init"])
	}
	ul, _ := h["Ulimits"].([]any)
	if len(ul) != 1 {
		t.Fatalf("want one ulimit (nofile; nproc counts per uid across the host), got %v", h["Ulimits"])
	}
	if u := ul[0].(map[string]any); u["Name"] != "nofile" || u["Soft"] != float64(65536) || u["Hard"] != float64(65536) {
		t.Fatalf("nofile ulimit: %v", u)
	}
	for _, k := range []string{"ReadonlyRootfs", "Tmpfs", "StorageOpt", "Runtime"} {
		if _, ok := h[k]; ok {
			t.Errorf("%s is opt-in, got %v", k, h[k])
		}
	}
	if caps, _ := h["CapDrop"].([]any); len(caps) != 1 || caps[0] != "ALL" {
		t.Fatalf("CapDrop ALL stays, got %v", h["CapDrop"])
	}
}

// The opt-ins an operator turns on reach the Engine as written.
func TestTheOperatorsLimitsReachTheEngine(t *testing.T) {
	l, err := LimitsFromEnv(envOf(map[string]string{
		"SANDBAR_DOCKER_PIDS_LIMIT":      "256",
		"SANDBAR_DOCKER_NOFILE":          "1024",
		"SANDBAR_DOCKER_NPROC":           "512",
		"SANDBAR_DOCKER_READONLY_ROOTFS": "on",
		"SANDBAR_DOCKER_TMPFS":           "/tmp:rw,size=64m /work",
		"SANDBAR_DOCKER_STORAGE_SIZE":    "20G",
		"SANDBAR_DOCKER_RUNTIME":         "runsc",
	}))
	if err != nil {
		t.Fatal(err)
	}
	spy := &engineSpy{}
	b := spy.backend(WithLimits(l))
	if _, err := b.Create(context.Background(), "sb-ops", core.Spec{Image: "x"}.WithDefaults()); err != nil {
		t.Fatalf("create: %v", err)
	}
	h := spy.created(t).HostConfig
	if h["PidsLimit"] != float64(256) || h["ReadonlyRootfs"] != true || h["Runtime"] != "runsc" {
		t.Fatalf("pids, read-only rootfs, runtime: %v", h)
	}
	if tm, _ := h["Tmpfs"].(map[string]any); len(tm) != 2 || tm["/tmp"] != "rw,size=64m" || tm["/work"] != "" {
		t.Fatalf("tmpfs: %v", h["Tmpfs"])
	}
	if so, _ := h["StorageOpt"].(map[string]any); so["size"] != "20G" {
		t.Fatalf("storage cap: %v", h["StorageOpt"])
	}
	if ul, _ := h["Ulimits"].([]any); len(ul) != 2 {
		t.Fatalf("nofile and nproc: %v", h["Ulimits"])
	}
}

func TestLimitsFromEnvDefaultsAndRefusals(t *testing.T) {
	l, err := LimitsFromEnv(envOf(nil))
	if err != nil || l.PidsLimit != 4096 || l.NoFile != 65536 || l.NProc != 0 || l.ReadonlyRootfs || l.Tmpfs != nil {
		t.Fatalf("defaults: %+v %v", l, err)
	}
	ro, err := LimitsFromEnv(envOf(map[string]string{"SANDBAR_DOCKER_READONLY_ROOTFS": "on"}))
	if err != nil || ro.Tmpfs["/tmp"] == "" {
		t.Fatalf("a read-only rootfs gets a writable /tmp by default: %+v %v", ro, err)
	}
	off, err := LimitsFromEnv(envOf(map[string]string{"SANDBAR_DOCKER_PIDS_LIMIT": "0"}))
	if err != nil || off.PidsLimit != 0 {
		t.Fatalf("0 turns the process cap off: %+v %v", off, err)
	}
	for k, v := range map[string]string{
		"SANDBAR_DOCKER_PIDS_LIMIT":      "-1",
		"SANDBAR_DOCKER_NOFILE":          "many",
		"SANDBAR_DOCKER_READONLY_ROOTFS": "maybe",
		"SANDBAR_DOCKER_TMPFS":           "tmp:size=1g",
	} {
		if _, err := LimitsFromEnv(envOf(map[string]string{k: v})); err == nil {
			t.Errorf("%s=%q must refuse", k, v)
		}
	}
}
