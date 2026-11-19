// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// The Engine has no verb that kills an exec, so a command past its timeout used to run on while
// its caller got a 502. Now it is killed - every process that carries its mark, by its own uid -
// and the timeout is a result: exit 124, as `timeout` reports it.
func TestAnExecPastItsTimeoutIsKilledAndExits124(t *testing.T) {
	inst, creates := execEngine{createCode: http.StatusCreated, hangFirst: true}.play()
	res, err := inst.Exec(context.Background(), core.ExecRequest{Command: "sleep 600", TimeoutS: 0.2, User: "browser"})
	if err != nil {
		t.Fatalf("a timeout is a result, not an error: %v", err)
	}
	if res.ExitCode != core.ExitTimedOut || !strings.Contains(res.Stderr, "timed out after 0.2s") {
		t.Fatalf("want exit 124 saying it timed out, got %+v", res)
	}
	c := creates()
	if len(c) != 2 {
		t.Fatalf("want the exec and its kill, got %+v", c)
	}
	var mark string
	for _, kv := range c[0].Env {
		if v, ok := strings.CutPrefix(kv, execMark+"="); ok {
			mark = v
		}
	}
	kill := c[1]
	if mark == "" || kill.User != "1001" || len(kill.Cmd) != 5 || kill.Cmd[2] != killMarkedScript || kill.Cmd[4] != mark {
		t.Fatalf("the kill runs as the exec's own uid with its mark as an argument: mark %q, kill %+v", mark, kill)
	}
	for _, kv := range kill.Env {
		if strings.HasPrefix(kv, execMark+"=") {
			t.Fatal("the kill must not carry the mark itself, or it would kill itself")
		}
	}
}

// A caller that leaves mid-exec gets its error, and what the exec started is still killed.
func TestAnExecWhoseCallerLeftIsKilled(t *testing.T) {
	inst, creates := execEngine{createCode: http.StatusCreated, hangFirst: true}.play()
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(100*time.Millisecond, cancel)
	if _, err := inst.Exec(ctx, core.ExecRequest{Command: "sleep 600", TimeoutS: 60}); err == nil {
		t.Fatal("a caller that left gets an error")
	}
	if c := creates(); len(c) != 2 || c[1].User != "1000" {
		t.Fatalf("want the exec and its kill as the agent uid, got %+v", c)
	}
}

// countSleeps counts the processes running `sleep <secs>` in a sandbox (pid 1's `sleep infinity`
// never matches).
func countSleeps(t *testing.T, ctx context.Context, inst core.Instance, secs string) string {
	t.Helper()
	res, err := inst.Exec(ctx, core.ExecRequest{Command: "ps -o args | grep -c '^sleep " + secs + "$'; true"})
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(res.Stdout)
}

// On a real Engine: an exec ends when its shell does, not when the last process holding its output
// lets go. The Engine closes an exec's streams about 2 s after its process exits, so `sleep 302 &
// echo hi` answers exit 0 with "hi" and the background child runs on. guestd matches it
// (TestExecWhoseShellLeftABackgroundChildEndsWithTheShell).
func TestDockerExecEndsWithItsShellAndLeavesABackgroundChild(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	inst, err := b.Create(ctx, "sbtest-background", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())

	started := time.Now()
	res, err := inst.Exec(ctx, core.ExecRequest{Command: "sleep 302 & echo hi", TimeoutS: 30})
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 0 || res.Stdout != "hi\n" || time.Since(started) > 10*time.Second {
		t.Fatalf("want exit 0 with hi soon after the shell exits, got %+v after %v", res, time.Since(started))
	}
	if n := countSleeps(t, ctx, inst, "302"); n != "1" {
		t.Fatalf("the background child must run on, found %s", n)
	}
}

// On a real Engine: a command past its timeout exits 124 and leaves nothing running - its
// background children included.
func TestDockerExecTimeoutKillsEverythingItStarted(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	inst, err := b.Create(ctx, "sbtest-timeout", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())

	started := time.Now()
	res, err := inst.Exec(ctx, core.ExecRequest{Command: "sleep 301 & sleep 301 & echo begun; sleep 301", TimeoutS: 1})
	if err != nil {
		t.Fatalf("a timeout is a result: %v", err)
	}
	if res.ExitCode != core.ExitTimedOut || res.Stdout != "begun\n" || time.Since(started) > 20*time.Second {
		t.Fatalf("want exit 124 with the output so far, got %+v after %v", res, time.Since(started))
	}
	if n := countSleeps(t, ctx, inst, "301"); n != "0" {
		t.Fatalf("the timed-out exec left %s processes running", n)
	}
}

// On a real Engine: the process cap holds, so a fork bomb stops at the sandbox.
func TestDockerPidsLimitHolds(t *testing.T) {
	sock := dockerSockOrSkip(t)
	b := New(sock, WithLimits(Limits{PidsLimit: 32, NoFile: 1024}))
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	inst, err := b.Create(ctx, "sbtest-pids", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())

	// 64 children under a cap of 32: the shell cannot fork them all, and says so. It waits for the
	// ones it got, so the cap is free again for the next exec.
	bomb, err := inst.Exec(ctx, core.ExecRequest{Command: "i=0; while [ $i -lt 64 ]; do sleep 2 & i=$((i+1)); done; wait", TimeoutS: 30})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(bomb.Stderr, "fork") && !strings.Contains(bomb.Stderr, "Resource temporarily unavailable") {
		t.Fatalf("64 processes under a cap of 32 must fail to fork, got %+v", bomb)
	}
	res, _ := inst.Exec(ctx, core.ExecRequest{Command: "ulimit -n"})
	if strings.TrimSpace(res.Stdout) != "1024" {
		t.Fatalf("want an open-file limit of 1024, got %q", res.Stdout)
	}
}
