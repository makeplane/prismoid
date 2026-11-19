// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// agentArena is a world-traversable directory root made, so the agent uid can reach it.
func agentArena(t *testing.T) string {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Skip("dropping to the agent uid needs root")
	}
	dir, err := os.MkdirTemp("", "guestd-asuid-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

// answer runs one request through handle and parses its frame.
func answer(t *testing.T, req guestwire.Request, body []byte) (guestwire.Response, []byte) {
	t.Helper()
	var conn bytes.Buffer
	conn.Write(body)
	var out bytes.Buffer
	handle(rw{&conn, &out}, req)
	b := out.Bytes()
	n := int(b[0])<<24 | int(b[1])<<16 | int(b[2])<<8 | int(b[3])
	var resp guestwire.Response
	if err := json.Unmarshal(b[4:4+n], &resp); err != nil {
		t.Fatal(err)
	}
	return resp, b[4+n:]
}

type rw struct {
	r *bytes.Buffer
	w *bytes.Buffer
}

func (x rw) Read(p []byte) (int, error)  { return x.r.Read(p) }
func (x rw) Write(p []byte) (int, error) { return x.w.Write(p) }

// A read as the agent's uid: what root alone may read is refused with the forbidden code, what the
// agent may read comes back, and the daemon is still root afterwards.
func TestGuestdReadDropsToUID(t *testing.T) {
	dir := agentArena(t)
	secret := filepath.Join(dir, "secret")
	open := filepath.Join(dir, "open")
	if err := os.WriteFile(secret, []byte("root only"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(open, []byte("shared"), 0o644); err != nil {
		t.Fatal(err)
	}
	resp, body := answer(t, guestwire.Request{Op: guestwire.OpReadFile, Path: secret, UID: core.AgentUID}, nil)
	if resp.OK || resp.Code != guestwire.CodeForbiddenPath || len(body) != 0 {
		t.Fatalf("a root-only file read as the agent must be forbidden with no bytes, got %+v %q", resp, body)
	}
	resp, body = answer(t, guestwire.Request{Op: guestwire.OpReadFile, Path: open, UID: core.AgentUID}, nil)
	if !resp.OK || string(body) != "shared" {
		t.Fatalf("a readable file must come back, got %+v %q", resp, body)
	}
	resp, body = answer(t, guestwire.Request{Op: guestwire.OpReadFile, Path: secret}, nil)
	if !resp.OK || string(body) != "root only" {
		t.Fatalf("a read with no uid is root's own, got %+v %q", resp, body)
	}
	if cur, _ := unix.SetfsuidRetUid(-1); os.Geteuid() != 0 || cur != 0 {
		t.Fatal("the daemon's own identity must stay root")
	}
}

// An agent write is made by the agent: it lands owned by the agent where the agent may write, is
// forbidden (and writes nothing) where it may not, and a system write (uid 0) is still root's.
func TestGuestdAgentWriteDropsToUID(t *testing.T) {
	dir := agentArena(t)
	open := filepath.Join(dir, "open")
	if err := os.Mkdir(open, 0o777); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(open, 0o777); err != nil {
		t.Fatal(err)
	}
	locked := filepath.Join(dir, "locked")
	if err := os.Mkdir(locked, 0o755); err != nil {
		t.Fatal(err)
	}
	data := []byte("hello")
	write := func(path string, uid int) guestwire.Response {
		resp, _ := answer(t, guestwire.Request{Op: guestwire.OpWriteFile, Path: path, Mode: 0o644, UID: uid, BodyLen: int64(len(data))}, data)
		return resp
	}
	if resp := write(filepath.Join(open, "a", "f"), core.AgentUID); !resp.OK {
		t.Fatalf("an agent write where the agent may write: %+v", resp)
	}
	var st unix.Stat_t
	if err := unix.Stat(filepath.Join(open, "a", "f"), &st); err != nil || st.Uid != core.AgentUID {
		t.Fatalf("the file must belong to the agent: %v uid %d", err, st.Uid)
	}
	resp := write(filepath.Join(locked, "f"), core.AgentUID)
	if resp.OK || resp.Code != guestwire.CodeForbiddenPath {
		t.Fatalf("an agent write into a root-owned folder must be forbidden, got %+v", resp)
	}
	if _, err := os.Stat(filepath.Join(locked, "f")); err == nil {
		t.Fatal("a forbidden write must leave nothing")
	}
	if resp := write(filepath.Join(locked, "f"), 0); !resp.OK {
		t.Fatalf("a system write is root's: %+v", resp)
	}
	if err := unix.Stat(filepath.Join(locked, "f"), &st); err != nil || st.Uid != 0 {
		t.Fatalf("a system write must belong to root: %v uid %d", err, st.Uid)
	}
}

// The thread an agent operation runs on is a real uid-1000 thread: euid and gid 1000, no
// supplementary groups, no capability left. The daemon's own threads stay root with their groups.
func TestAsUIDIsARealUnprivilegedThread(t *testing.T) {
	agentArena(t)
	groups, err := unix.Getgroups()
	if err != nil {
		t.Fatal(err)
	}
	var euid, egid, ngroups int
	var status string
	err = asUID(core.AgentUID, func() error {
		euid, egid = os.Geteuid(), os.Getegid()
		g, _ := unix.Getgroups()
		ngroups = len(g)
		b, err := os.ReadFile("/proc/thread-self/status")
		status = string(b)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if euid != core.AgentUID || egid != core.AgentUID || ngroups != 0 {
		t.Fatalf("want a 1000:1000 thread with no groups, got euid %d egid %d groups %d", euid, egid, ngroups)
	}
	if !strings.Contains(status, "CapEff:\t0000000000000000") {
		t.Fatalf("the agent thread must hold no capability:\n%s", status)
	}
	after, _ := unix.Getgroups()
	if os.Geteuid() != 0 || !reflect.DeepEqual(groups, after) {
		t.Fatalf("the daemon must stay root with its groups: euid %d groups %v -> %v", os.Geteuid(), groups, after)
	}
}

// A 0400 root file is the agent's to refuse (not its owner, no group or other bits), an absent file is
// no such path, a planted pipe is no regular file (and blocks nothing), and a listing is the agent's
// too.
func TestGuestdAgentReadsAreTypedAndNeverBlock(t *testing.T) {
	dir := agentArena(t)
	read := func(path string) guestwire.Response {
		resp, _ := answer(t, guestwire.Request{Op: guestwire.OpReadFile, Path: path, UID: core.AgentUID}, nil)
		return resp
	}
	ro := filepath.Join(dir, "ro")
	if err := os.WriteFile(ro, []byte("x"), 0o400); err != nil {
		t.Fatal(err)
	}
	if r := read(ro); r.OK || r.Code != guestwire.CodeForbiddenPath {
		t.Fatalf("a 0400 root file: %+v", r)
	}
	if r := read(filepath.Join(dir, "absent")); r.OK || r.Code != guestwire.CodeNoSuchPath {
		t.Fatalf("an absent file: %+v", r)
	}
	fifo := filepath.Join(dir, "fifo")
	if err := unix.Mkfifo(fifo, 0o644); err != nil {
		t.Fatal(err)
	}
	if r := read(fifo); r.OK || r.Code != "" {
		t.Fatalf("a pipe is no regular file: %+v", r)
	}
	locked := filepath.Join(dir, "locked")
	if err := os.Mkdir(locked, 0o700); err != nil {
		t.Fatal(err)
	}
	resp, _ := answer(t, guestwire.Request{Op: guestwire.OpListDir, Path: locked, UID: core.AgentUID}, nil)
	if resp.OK || resp.Code != guestwire.CodeForbiddenPath {
		t.Fatalf("a listing the agent may not make: %+v", resp)
	}
	resp, _ = answer(t, guestwire.Request{Op: guestwire.OpListDir, Path: dir, UID: core.AgentUID}, nil)
	if !resp.OK || len(resp.Entries) == 0 {
		t.Fatalf("a listing the agent may make: %+v", resp)
	}
}

// After a burst of agent operations the process's leader thread is still root: a goroutine that
// dropped to uid 1000 and ended on it would leave guestd killable by the workload.
func TestLeaderThreadStaysRootAfterManyAgentOperations(t *testing.T) {
	dir := agentArena(t)
	defer runtime.GOMAXPROCS(runtime.GOMAXPROCS(4))
	if err := os.WriteFile(filepath.Join(dir, "f"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 200; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = asUID(core.AgentUID, func() error { _, err := readRegular(filepath.Join(dir, "f"), core.AgentUID); return err })
		}()
	}
	wg.Wait()
	b, err := os.ReadFile("/proc/self/status") // the process's leader, not this thread
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), "Uid:\t0\t0\t0\t0") {
		t.Fatalf("the leader thread is not root:\n%s", b)
	}
}

// A pipe planted at a write target blocks nothing and is never written.
func TestWriteToAPlantedPipeIsRefused(t *testing.T) {
	dir := agentArena(t)
	fifo := filepath.Join(dir, "fifo")
	if err := unix.Mkfifo(fifo, 0o666); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(fifo, 0o666); err != nil { // the umask trimmed it
		t.Fatal(err)
	}
	if err := writeFileAs(fifo, []byte("x"), 0o644, 0); err == nil {
		t.Fatal("a write to a pipe with no reader must fail, not block")
	}
	r, err := os.OpenFile(fifo, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	if err := writeFileAs(fifo, []byte("x"), 0o644, core.AgentUID); err == nil || !strings.Contains(err.Error(), "no regular file") {
		t.Fatalf("a write to a pipe with a reader must be refused as no regular file, got %v", err)
	}
}

// An agent's read follows a link as uid 1000 would, as the docker backend's does: a link to a file it
// may read gives the target's bytes, a link to one it may not is forbidden, a dangling link is no
// such path, and a /proc magic link (guestd's own, since the reading thread is in its thread group)
// is forbidden. Root's own read never follows a link.
func TestGuestdAgentReadFollowsALinkAsTheAgent(t *testing.T) {
	dir := agentArena(t)
	read := func(path string, uid int) (guestwire.Response, []byte) {
		return answer(t, guestwire.Request{Op: guestwire.OpReadFile, Path: path, UID: uid}, nil)
	}
	link := func(target, name string) string {
		t.Helper()
		p := filepath.Join(dir, name)
		if err := os.Symlink(target, p); err != nil {
			t.Fatal(err)
		}
		return p
	}
	open, secret := filepath.Join(dir, "open"), filepath.Join(dir, "secret")
	if err := os.WriteFile(open, []byte("shared"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(secret, []byte("root only"), 0o600); err != nil {
		t.Fatal(err)
	}
	if r, body := read(link(open, "to-open"), core.AgentUID); !r.OK || string(body) != "shared" {
		t.Fatalf("a link to a file the agent may read: %+v %q", r, body)
	}
	if r, _ := read(link(secret, "to-secret"), core.AgentUID); r.OK || r.Code != guestwire.CodeForbiddenPath {
		t.Fatalf("a link to a file the agent may not read: %+v", r)
	}
	if r, _ := read(link(filepath.Join(dir, "gone"), "dangling"), core.AgentUID); r.OK || r.Code != guestwire.CodeNoSuchPath {
		t.Fatalf("a dangling link: %+v", r)
	}
	if r, _ := read(link("/proc/self/root"+open, "magic"), core.AgentUID); r.OK || r.Code != guestwire.CodeForbiddenPath {
		t.Fatalf("a /proc magic link: %+v", r)
	}
	if r, _ := read(filepath.Join(dir, "to-open"), 0); r.OK || r.Code != guestwire.CodeForbiddenPath {
		t.Fatalf("root's own read of a link: %+v", r)
	}
}
