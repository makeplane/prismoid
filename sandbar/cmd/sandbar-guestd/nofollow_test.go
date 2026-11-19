// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

package main

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// A link the workload planted never turns a write into one outside its place: a write through a
// linked directory is refused, and a file written where a link stands replaces the link.
func TestAWriteNeverFollowsASymlink(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(root, "outside")
	if err := os.Mkdir(outside, 0o755); err != nil {
		t.Fatal(err)
	}
	secret := filepath.Join(outside, "secret")
	if err := os.WriteFile(secret, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	work := filepath.Join(root, "work")
	if err := os.Mkdir(work, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(work, "dir")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(work, "file")); err != nil {
		t.Fatal(err)
	}
	uid, gid := os.Getuid(), os.Getgid()

	if err := writeFileAs(filepath.Join(work, "dir", "secret"), []byte("owned"), 0o644, uid); err == nil {
		t.Fatal("a write through a linked directory must be refused")
	}
	if err := writeFileAs(filepath.Join(work, "file"), []byte("mine"), 0o644, uid); err != nil {
		t.Fatalf("a write where a link stands replaces it: %v", err)
	}
	if got, _ := os.ReadFile(secret); string(got) != "keep" {
		t.Fatalf("the link's target was written: %q", got)
	}
	if fi, err := os.Lstat(filepath.Join(work, "file")); err != nil || !fi.Mode().IsRegular() {
		t.Fatalf("the link must be replaced by a regular file: %v %v", fi, err)
	}
	if _, err := readRegular(filepath.Join(work, "dir", "secret"), 0); err == nil {
		t.Fatal("a read through a linked directory must be refused")
	}
	if err := os.Symlink(secret, filepath.Join(work, "again")); err != nil {
		t.Fatal(err)
	}
	if _, err := readRegular(filepath.Join(work, "again"), 0); err == nil {
		t.Fatal("a read of a link must be refused (it holds no regular file)")
	}
	_ = gid
}

// A tar round trip keeps symlinks as links, both ways; an archive entry that would land below a
// link it made is refused instead of written through it.
func TestATarKeepsSymlinksAndNeverWritesThroughOne(t *testing.T) {
	src := t.TempDir()
	box := filepath.Join(src, "box")
	if err := os.MkdirAll(filepath.Join(box, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(box, "sub", "a.txt"), []byte("a"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("sub/a.txt", filepath.Join(box, "link")); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := tarPath(tw, box); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	dest := t.TempDir()
	if err := extractTar(tar.NewReader(bytes.NewReader(buf.Bytes())), dest); err != nil {
		t.Fatalf("extract: %v", err)
	}
	if target, err := os.Readlink(filepath.Join(dest, "box", "link")); err != nil || target != "sub/a.txt" {
		t.Fatalf("the symlink must come back as a link to the same target: %q %v", target, err)
	}

	outside := t.TempDir()
	var evil bytes.Buffer
	ew := tar.NewWriter(&evil)
	_ = ew.WriteHeader(&tar.Header{Name: "x/", Typeflag: tar.TypeDir, Mode: 0o755})
	_ = ew.WriteHeader(&tar.Header{Name: "x/out", Typeflag: tar.TypeSymlink, Linkname: outside})
	_ = ew.WriteHeader(&tar.Header{Name: "x/out/planted", Typeflag: tar.TypeReg, Mode: 0o644, Size: 1})
	_, _ = ew.Write([]byte("!"))
	_ = ew.Close()
	if err := extractTar(tar.NewReader(&evil), t.TempDir()); err == nil {
		t.Fatal("an entry below a link the archive made must be refused")
	}
	if _, err := os.Stat(filepath.Join(outside, "planted")); err == nil {
		t.Fatal("the archive wrote through its own link")
	}
}

func execResponse(t *testing.T, req guestwire.Request) guestwire.Response {
	t.Helper()
	var conn bytes.Buffer
	doExec(&conn, req)
	var resp guestwire.Response
	if conn.Len() < 4 || json.Unmarshal(conn.Bytes()[4:], &resp) != nil {
		t.Fatalf("no response framed: %q", conn.Bytes())
	}
	return resp
}

// A command past its timeout is killed with its whole process group - a background child
// included - and exits 124; its output is bounded and says so.
func TestExecTimeoutKillsTheGroupAndOutputIsBounded(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("doExec drops to the agent uid, which needs root")
	}
	if _, err := os.Stat("/work"); err != nil {
		t.Skip("needs the guest layout's /work")
	}
	dir, err := os.MkdirTemp("/tmp", "guestd-") // under /tmp itself, so the agent uid can reach it
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	if err := os.Chmod(dir, 0o777); err != nil {
		t.Fatal(err)
	}
	control := filepath.Join(dir, "control")
	if r := execResponse(t, guestwire.Request{Op: guestwire.OpExec, Command: "touch " + control}); r.ExitCode != 0 {
		t.Fatalf("positive control: the agent uid must be able to write the marker's directory: %+v", r)
	}
	marker := filepath.Join(dir, "survived")
	began := time.Now()
	resp := execResponse(t, guestwire.Request{
		Op: guestwire.OpExec, TimeoutS: 0.5,
		Command: "(sleep 2; touch " + marker + ") & echo begun; sleep 60",
	})
	if resp.ExitCode != core.ExitTimedOut || !strings.Contains(resp.Stdout, "begun") || time.Since(began) > 10*time.Second {
		t.Fatalf("want exit 124 with the output so far, got %+v after %v", resp, time.Since(began))
	}
	time.Sleep(3 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("a background child outlived its command's timeout")
	}

	big := execResponse(t, guestwire.Request{Op: guestwire.OpExec, Command: "head -c 17000000 /dev/zero | tr '\\0' x"})
	if !big.Truncated || len(big.Stdout) != core.MaxExecOutput || big.ExitCode != 0 {
		t.Fatalf("want stdout cut at %d and truncated, got %d bytes, truncated %v, exit %d", core.MaxExecOutput, len(big.Stdout), big.Truncated, big.ExitCode)
	}
}
