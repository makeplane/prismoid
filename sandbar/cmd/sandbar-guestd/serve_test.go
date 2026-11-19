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
	"strconv"
	"syscall"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// TestExecNeverRunsAStartupFile: guestd runs every exec - the agent's and the caller's own system
// commands alike - so none may read a shell startup file. The guest's HOME is the kernel's "/"
// today; a HOME the agent can write must still hook nothing.
func TestExecNeverRunsAStartupFile(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("doExec drops to the agent uid, which needs root")
	}
	if _, err := os.Stat("/work"); err != nil {
		t.Skip("needs the guest layout's /work")
	}
	home, err := os.MkdirTemp("", "guestd-home-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(home) })
	if err := os.Chmod(home, 0o777); err != nil { // the agent's own, as /work is
		t.Fatal(err)
	}
	marker := filepath.Join(home, "profile-ran")
	if err := os.WriteFile(filepath.Join(home, ".profile"), []byte("touch "+marker+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", home)

	var conn bytes.Buffer
	doExec(&conn, guestwire.Request{Op: guestwire.OpExec, Command: "true"})
	if conn.Len() < 4 {
		t.Fatalf("no response framed: %q", conn.Bytes())
	}
	var resp guestwire.Response
	if err := json.Unmarshal(conn.Bytes()[4:], &resp); err != nil {
		t.Fatalf("response: %v", err)
	}
	if !resp.OK || resp.ExitCode != 0 {
		t.Fatalf("exec failed: %+v", resp)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the exec ran $HOME/.profile")
	}
}

// An exec ends with its shell, as on the docker backend (whose Engine closes an exec's streams
// about 2 s after its process exits): a background child still holding stdout neither holds the
// answer to the timeout nor is killed - `sleep 303 & echo hi` answers exit 0 with "hi" and the
// child runs on.
func TestExecWhoseShellLeftABackgroundChildEndsWithTheShell(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("doExec drops to the agent uid, which needs root")
	}
	if _, err := os.Stat("/work"); err != nil {
		t.Skip("needs the guest layout's /work")
	}
	began := time.Now()
	resp := execResponse(t, guestwire.Request{Op: guestwire.OpExec, Command: "sleep 303 & echo hi", TimeoutS: 30})
	pids := processesRunning(t, "sleep\x00303\x00")
	for _, pid := range pids {
		t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
	}
	if resp.ExitCode != 0 || resp.Stdout != "hi\n" || time.Since(began) > 10*time.Second {
		t.Fatalf("want exit 0 with hi soon after the shell exits, got %+v after %v", resp, time.Since(began))
	}
	if len(pids) != 1 {
		t.Fatalf("the background child must run on, found %d", len(pids))
	}
}

// Output that is all NULs escapes to six bytes a byte: two full outputs would make a header past
// the bound the host reads under, and the exec would fail. guestd cuts them until the header fits,
// and the answer says it was cut.
func TestNulHeavyExecOutputStillFramesUnderTheBound(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("doExec drops to the agent uid, which needs root")
	}
	if _, err := os.Stat("/work"); err != nil {
		t.Skip("needs the guest layout's /work")
	}
	var conn bytes.Buffer
	doExec(&conn, guestwire.Request{Op: guestwire.OpExec,
		Command: "head -c 17000000 /dev/zero; head -c 17000000 /dev/zero >&2"})
	b := conn.Bytes()
	if len(b) < 4 {
		t.Fatalf("no response framed: %d bytes", len(b))
	}
	n := int(b[0])<<24 | int(b[1])<<16 | int(b[2])<<8 | int(b[3])
	if n > guestwire.MaxHeaderLen || n != len(b)-4 {
		t.Fatalf("header of %d bytes (frame %d) is over the %d-byte bound", n, len(b)-4, guestwire.MaxHeaderLen)
	}
	var resp guestwire.Response
	if err := json.Unmarshal(b[4:], &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.OK || resp.ExitCode != 0 || !resp.Truncated || resp.Stdout == "" || resp.Stderr == "" {
		t.Fatalf("want a truncated success with both outputs' starts: exit %d, truncated %v, stdout %d, stderr %d",
			resp.ExitCode, resp.Truncated, len(resp.Stdout), len(resp.Stderr))
	}
}

// processesRunning lists the pids whose /proc cmdline is exactly cmdline (NUL-separated args).
func processesRunning(t *testing.T, cmdline string) []int {
	t.Helper()
	dirs, err := filepath.Glob("/proc/[0-9]*")
	if err != nil {
		t.Fatal(err)
	}
	var pids []int
	for _, d := range dirs {
		b, err := os.ReadFile(filepath.Join(d, "cmdline"))
		if err != nil || string(b) != cmdline {
			continue
		}
		if pid, err := strconv.Atoi(filepath.Base(d)); err == nil {
			pids = append(pids, pid)
		}
	}
	return pids
}
