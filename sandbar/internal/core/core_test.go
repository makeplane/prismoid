// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package core

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"strings"
	"sync"
	"testing"
)

// fakeBackend is an in-memory Instance for testing the registry without any real
// isolation tech - the registry's job is bookkeeping, and that is testable pure.
type fakeBackend struct {
	name    string
	created int
	mu      sync.Mutex
}

func (f *fakeBackend) Name() string { return f.name }
func (f *fakeBackend) Create(_ context.Context, id string, _ Spec) (Instance, error) {
	f.mu.Lock()
	f.created++
	f.mu.Unlock()
	return &fakeInstance{id: id}, nil
}

type fakeInstance struct {
	id        string
	destroyed bool
}

func (i *fakeInstance) Exec(context.Context, ExecRequest) (ExecResult, error) {
	return ExecResult{ExitCode: 0, Stdout: i.id}, nil
}
func (i *fakeInstance) WriteFile(context.Context, string, []byte, fs.FileMode, Owner) error {
	return nil
}
func (i *fakeInstance) ReadFile(context.Context, string) ([]byte, error)   { return nil, nil }
func (i *fakeInstance) ListDir(context.Context, string) ([]string, error)  { return nil, nil }
func (i *fakeInstance) Archive(context.Context, []string, io.Writer) error { return nil }
func (i *fakeInstance) Extract(context.Context, io.Reader) error           { return nil }
func (i *fakeInstance) Denials(context.Context) ([]Denial, error)          { return nil, nil }
func (i *fakeInstance) Egress(context.Context, []string) error             { return nil }
func (i *fakeInstance) Destroy(context.Context) error                      { i.destroyed = true; return nil }

func TestSpecValidateAndDefaults(t *testing.T) {
	if err := (Spec{}).Validate(); err == nil {
		t.Fatal("empty image must be refused")
	}
	s := Spec{Image: "x"}.WithDefaults()
	if s.CPUs != 2 || s.MemMB != 2048 {
		t.Fatalf("defaults not applied: %+v", s)
	}
	if err := (Spec{Image: "x", CPUs: 999}).Validate(); err == nil {
		t.Fatal("absurd cpu count must be refused")
	}
}

func TestValidID(t *testing.T) {
	for _, ok := range []string{"task-1", "sb-abc123", "a", "A.b_c-1"} {
		if !ValidID(ok) {
			t.Errorf("%q should be valid", ok)
		}
	}
	for _, bad := range []string{"", "../etc", "has space", "-leading", strings.Repeat("x", 200)} {
		if ValidID(bad) {
			t.Errorf("%q should be invalid", bad)
		}
	}
}

func TestRegistryLifecycle(t *testing.T) {
	fb := &fakeBackend{name: "fake"}
	reg := NewRegistry("fake", fb)

	box, err := reg.Create(context.Background(), "t1", Spec{Image: "img"})
	if err != nil {
		t.Fatal(err)
	}
	if box.Backend != "fake" || box.Spec.CPUs != 2 {
		t.Fatalf("record wrong: %+v", box)
	}
	if _, err := reg.Get("t1"); err != nil {
		t.Fatalf("get after create: %v", err)
	}
	if _, err := reg.Create(context.Background(), "t1", Spec{Image: "img"}); err == nil {
		t.Fatal("duplicate id must be refused")
	}
	if len(reg.List()) != 1 {
		t.Fatalf("list should have 1, has %d", len(reg.List()))
	}

	inst := box.Instance().(*fakeInstance)
	if err := reg.Destroy(context.Background(), "t1"); err != nil {
		t.Fatal(err)
	}
	if !inst.destroyed {
		t.Fatal("destroy did not reach the instance")
	}
	if _, err := reg.Get("t1"); err != ErrNotFound {
		t.Fatalf("want ErrNotFound after destroy, got %v", err)
	}
}

func TestRegistryUnknownBackend(t *testing.T) {
	reg := NewRegistry("fake", &fakeBackend{name: "fake"})
	_, err := reg.Create(context.Background(), "t1", Spec{Image: "img", Backend: "nope"})
	if err == nil || !strings.Contains(err.Error(), "no backend") {
		t.Fatalf("want unknown-backend error, got %v", err)
	}
}

func TestDestroyUnknownIsNotFound(t *testing.T) {
	reg := NewRegistry("fake", &fakeBackend{name: "fake"})
	if err := reg.Destroy(context.Background(), "ghost"); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestSpecValidateRejectsTooManyOrOversizedLabels(t *testing.T) {
	ok := Spec{Image: "x", Labels: map[string]string{"plane.kind": "machine"}}
	if err := ok.Validate(); err != nil {
		t.Fatalf("a small label set is valid: %v", err)
	}
	long := Spec{Image: "x", Labels: map[string]string{strings.Repeat("k", 64): "v"}}
	if err := long.Validate(); !errors.Is(err, ErrBadSpec) {
		t.Fatalf("a 64-char key must be refused, got %v", err)
	}
	many := Spec{Image: "x", Labels: map[string]string{}}
	for i := 0; i < 33; i++ {
		many.Labels[fmt.Sprintf("k%d", i)] = "v"
	}
	if err := many.Validate(); !errors.Is(err, ErrBadSpec) {
		t.Fatalf("33 labels must be refused, got %v", err)
	}
}

func TestSetEgressRefusesAStaleVersion(t *testing.T) {
	reg := NewRegistry("fake", &fakeBackend{name: "fake"})
	box, err := reg.Create(context.Background(), "s1", Spec{Image: "x", AllowDomains: []string{"a.example"}})
	if err != nil {
		t.Fatal(err)
	}
	box.InitEgressVersion(3)
	if _, err := box.SetEgress(context.Background(), []string{"b.example"}, 2); !errors.Is(err, ErrStale) {
		t.Fatalf("version 2 after 3 must be stale, got %v", err)
	}
	if v, err := box.SetEgress(context.Background(), []string{"b.example"}, 4); err != nil || v != 4 {
		t.Fatalf("version 4 applies: v=%d err=%v", v, err)
	}
	if v, err := box.SetEgress(context.Background(), []string{"c.example"}, 0); err != nil || v != 4 {
		t.Fatalf("an unversioned push applies and keeps the version: v=%d err=%v", v, err)
	}
}

// adoptingBackend is a backend whose sandboxes outlive the daemon: Adopt hands back what it holds.
type adoptingBackend struct {
	fakeBackend
	held []Adopted
}

func (a *adoptingBackend) Adopt(context.Context) ([]Adopted, error) { return a.held, nil }

// A restarted sandbard (every deploy restarts it) keeps what its backends still hold: the records
// come back with their specs, a create cannot reuse their ids, and their egress version starts at
// 0, so the caller's next versioned push applies.
func TestAdoptRecordsWhatABackendStillHolds(t *testing.T) {
	kept := &fakeInstance{id: "m1"}
	docker := &adoptingBackend{fakeBackend: fakeBackend{name: "docker"}, held: []Adopted{
		{ID: "m1", Spec: Spec{Image: "frame", Labels: map[string]string{"plane.org": "o1"}}, Instance: kept},
	}}
	r := NewRegistry("docker", docker)
	n, err := r.Adopt(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("adopt: %d %v", n, err)
	}
	box, err := r.Get("m1")
	if err != nil || box.Instance() != kept || box.Backend != "docker" || box.Spec.Labels["plane.org"] != "o1" {
		t.Fatalf("the adopted record: %+v %v", box, err)
	}
	if _, err := r.Create(context.Background(), "m1", Spec{Image: "x"}); !errors.Is(err, ErrBadSpec) {
		t.Fatalf("an adopted id is taken: %v", err)
	}
	if v, err := box.SetEgress(context.Background(), []string{"example.com"}, 1); err != nil || v != 1 {
		t.Fatalf("the caller's first versioned push applies: %d %v", v, err)
	}
}

// The daemon's exit keeps every sandbox a backend re-adopts at the next boot, and tears down the
// rest (a backend that cannot re-adopt would leak them).
func TestShutdownKeepsWhatABackendReadoptsAndDestroysTheRest(t *testing.T) {
	docker := &adoptingBackend{fakeBackend: fakeBackend{name: "docker"}}
	other := &fakeBackend{name: "firecracker"}
	r := NewRegistry("docker", docker, other)
	kept, err := r.Create(context.Background(), "d1", Spec{Image: "x"})
	if err != nil {
		t.Fatal(err)
	}
	gone, err := r.Create(context.Background(), "f1", Spec{Image: "x", Backend: "firecracker"})
	if err != nil {
		t.Fatal(err)
	}
	r.Shutdown(context.Background())
	if kept.Instance().(*fakeInstance).destroyed {
		t.Fatal("a sandbox its backend re-adopts must survive the daemon's exit")
	}
	if !gone.Instance().(*fakeInstance).destroyed {
		t.Fatal("a sandbox its backend cannot re-adopt is torn down at exit")
	}
}

// Root may remove only what the system gave a sandbox: a clean, absolute path strictly below
// SystemRoot. /task itself (its mode and owner are the lock), an escape, a relative path and a
// sibling that merely starts with the same letters are refused before any backend is asked.
func TestSystemPathIsStrictlyBelowTheSystemRoot(t *testing.T) {
	for p, ok := range map[string]bool{
		"/task/inputs/01-a.md": true, "/task/inputs/sub": true, "/task/context.md": true, "/task/.step-start": true,
		"/task": false, "/task/": false, "/task/inputs/": false, "/task/../etc/passwd": false, "/task/./x": false,
		"/tasks/x": false, "/etc/passwd": false, "task/x": false, "": false, "/": false, "/skills/x": false,
	} {
		if err := SystemPath(p); (err == nil) != ok || (err != nil && !errors.Is(err, ErrBadSpec)) {
			t.Errorf("SystemPath(%q) = %v, want ok=%v", p, err, ok)
		}
	}
}

func TestCleanPath(t *testing.T) {
	for p, ok := range map[string]bool{
		"": false, "work/x": false, "/work/../etc/passwd": false, "/work//x": false, "/work/x\x00": false,
		"/work/x": true, "/task/inputs/a.csv": true,
	} {
		if err := CleanPath(p); (err == nil) != ok || (err != nil && !errors.Is(err, ErrBadSpec)) {
			t.Errorf("CleanPath(%q) = %v, want ok=%v", p, err, ok)
		}
	}
}
