// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package firecracker

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// A spec's image names a rootfs template file, never a path: anything else is refused before the
// backend touches the disk.
func TestCreateRefusesAnImageThatIsNotATemplateName(t *testing.T) {
	run := t.TempDir()
	b := &Backend{cfg: Config{RunDir: run, RootfsDir: t.TempDir()}}
	for _, image := range []string{"../../etc/shadow", "a/b", "Frame", "", "frame image"} {
		if _, err := b.Create(context.Background(), "vm1", core.Spec{Image: image}); !errors.Is(err, core.ErrBadSpec) {
			t.Errorf("image %q: want ErrBadSpec, got %v", image, err)
		}
	}
	if entries, _ := os.ReadDir(run); len(entries) != 0 {
		t.Fatalf("a refused image must leave nothing behind, got %v", entries)
	}
}

// fakeGuest answers Firecracker's vsock handshake on a unix socket and then plays guestd with
// answer, called once per request header.
func fakeGuest(t *testing.T, answer func(conn net.Conn, req guestwire.Request)) string {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "sbfc") // short: a unix socket path is capped near 104 bytes
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	sock := filepath.Join(dir, "v.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				br := bufio.NewReader(conn)
				if _, err := br.ReadString('\n'); err != nil {
					return
				}
				_, _ = conn.Write([]byte("OK 1\n"))
				var lb [4]byte
				if _, err := io.ReadFull(br, lb[:]); err != nil {
					return
				}
				n := int(lb[0])<<24 | int(lb[1])<<16 | int(lb[2])<<8 | int(lb[3])
				buf := make([]byte, n)
				if _, err := io.ReadFull(br, buf); err != nil {
					return
				}
				var req guestwire.Request
				_ = json.Unmarshal(buf, &req)
				// Real guestd reads a request's whole body before it answers; a fake that answers
				// first races the host's body write into a broken pipe.
				if req.BodyLen > 0 {
					_, _ = io.CopyN(io.Discard, br, req.BodyLen)
				}
				answer(conn, req)
			}()
		}
	}()
	return sock
}

func reply(conn net.Conn, resp guestwire.Response) {
	hdr, _ := json.Marshal(resp)
	_ = writeLen(conn, len(hdr))
	_, _ = conn.Write(hdr)
}

// A call that runs for minutes no longer holds the instance: Destroy returns at once and ends the
// call, and two calls run side by side.
func TestDestroyDoesNotWaitForACallInFlight(t *testing.T) {
	hold := make(chan struct{})
	sock := fakeGuest(t, func(conn net.Conn, req guestwire.Request) {
		if req.Command == "quick" {
			reply(conn, guestwire.Response{OK: true, Stdout: "done"})
			return
		}
		<-hold // a command that never ends
	})
	t.Cleanup(func() { close(hold) })
	inst := &instance{id: "vm1", dir: t.TempDir(), vsockUDS: sock}

	slow := make(chan error, 1)
	go func() {
		_, err := inst.Exec(context.Background(), core.ExecRequest{Command: "sleep 600"})
		slow <- err
	}()
	time.Sleep(100 * time.Millisecond)
	if res, err := inst.Exec(context.Background(), core.ExecRequest{Command: "quick"}); err != nil || res.Stdout != "done" {
		t.Fatalf("a second call runs beside a slow one: %+v %v", res, err)
	}

	destroyed := make(chan error, 1)
	go func() { destroyed <- inst.Destroy(context.Background()) }()
	select {
	case err := <-destroyed:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Destroy waited for a call in flight")
	}
	select {
	case err := <-slow:
		if err == nil {
			t.Fatal("the call in flight must end with an error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Destroy did not end the call in flight")
	}
	if _, err := inst.Exec(context.Background(), core.ExecRequest{Command: "quick"}); err == nil {
		t.Fatal("no call starts after Destroy")
	}
}

// The guest names the length of what follows a header; a length past the bound on a file is
// refused before anything is held in memory.
func TestAGuestBodyPastTheBoundIsRefused(t *testing.T) {
	sock := fakeGuest(t, func(conn net.Conn, _ guestwire.Request) {
		reply(conn, guestwire.Response{OK: true, BodyLen: core.MaxFileBytes + 1})
	})
	inst := &instance{id: "vm1", dir: t.TempDir(), vsockUDS: sock}
	if _, err := inst.ReadFile(context.Background(), "/work/x"); err == nil {
		t.Fatal("a body over the bound must be refused")
	}
}

// A read is always the agent's, and an agent write carries the agent's uid; a system write is
// root's. The guest's own refusal comes back as core.ErrForbiddenPath.
func TestFilesCarryTheAgentUIDAndAForbiddenAnswerIsTyped(t *testing.T) {
	var got []guestwire.Request
	sock := fakeGuest(t, func(conn net.Conn, req guestwire.Request) {
		got = append(got, req)
		reply(conn, guestwire.Response{Error: "open: permission denied", Code: guestwire.CodeForbiddenPath})
	})
	inst := &instance{id: "vm1", dir: t.TempDir(), vsockUDS: sock}
	ctx := context.Background()
	if _, err := inst.ReadFile(ctx, "/browser/Cookies"); !errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("want ErrForbiddenPath, got %v", err)
	}
	if err := inst.WriteFile(ctx, "/work/x", []byte("a"), 0o644, core.OwnerAgent); !errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("want ErrForbiddenPath, got %v", err)
	}
	_ = inst.WriteFile(ctx, "/task/x", []byte("a"), 0o644, core.OwnerSystem)
	if len(got) != 3 || got[0].UID != core.AgentUID || got[1].UID != core.AgentUID || got[2].UID != 0 {
		t.Fatalf("uids on the wire: %+v", got)
	}
}

// A guest's absent-path answer is core.ErrNoSuchPath, for a file and a listing alike.
func TestAnAbsentPathAnswerIsTyped(t *testing.T) {
	sock := fakeGuest(t, func(conn net.Conn, _ guestwire.Request) {
		reply(conn, guestwire.Response{Error: "open: no such file", Code: guestwire.CodeNoSuchPath})
	})
	inst := &instance{id: "vm1", dir: t.TempDir(), vsockUDS: sock}
	if _, err := inst.ReadFile(context.Background(), "/work/x"); !errors.Is(err, core.ErrNoSuchPath) {
		t.Fatalf("read: want ErrNoSuchPath, got %v", err)
	}
	if _, err := inst.ListDir(context.Background(), "/work/x"); !errors.Is(err, core.ErrNoSuchPath) {
		t.Fatalf("list: want ErrNoSuchPath, got %v", err)
	}
}
