// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"io"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// ── The file door on a real Engine: who a write runs as is proven only where the kernel decides ──

// layoutEntry is one directory (a name ending in "/") or file of the layout a test gives its
// sandbox.
type layoutEntry struct {
	name string
	uid  int
	mode int64
	body string
}

// workshop gives /work to the agent, as a frame image does: Docker makes a missing working
// directory root's, 0755, and an agent's write runs as the agent.
var workshop = []layoutEntry{{"work/", core.AgentUID, 0o777, ""}}

// frameLayout is a frame image's layout: the agent's workshop and the folders it owns, the system's
// read-only folders, the browser user's closed profile, and a root-owned, read-only file standing
// for code the image ships.
var frameLayout = append(append([]layoutEntry(nil), workshop...),
	layoutEntry{"artifacts/", core.AgentUID, 0o777, ""},
	layoutEntry{"skills/", core.AgentUID, 0o777, ""},
	layoutEntry{"task/", 0, 0o555, ""},
	layoutEntry{"task/inputs/", 0, 0o555, ""},
	layoutEntry{"verify/", 0, 0o555, ""},
	layoutEntry{"browser/", core.BrowserUID, 0o700, ""},
	layoutEntry{"browser/profile/", core.BrowserUID, 0o700, ""},
	layoutEntry{"opt/side/", 0, 0o555, ""},
	layoutEntry{"opt/side/code.py", 0, 0o444, "print('shipped')\n"},
)

// layOut lays entries down through the Engine, which alone can give a path another uid: root inside
// a sandbox holds no capability.
func layOut(t *testing.T, ctx context.Context, inst core.Instance, entries []layoutEntry) {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, e := range entries {
		hdr := &tar.Header{Name: e.name, Mode: e.mode, Uid: e.uid, Gid: e.uid, Typeflag: tar.TypeDir}
		if !strings.HasSuffix(e.name, "/") {
			hdr.Typeflag, hdr.Size = tar.TypeReg, int64(len(e.body))
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(e.body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := inst.Extract(ctx, &buf); err != nil {
		t.Fatalf("lay out: %v", err)
	}
}

// frameSandbox is an alpine sandbox with frameLayout, destroyed at cleanup.
func frameSandbox(t *testing.T, ctx context.Context, b *Backend, name string) core.Instance {
	t.Helper()
	inst, err := b.Create(ctx, name+"-"+randToken()[:6], core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	t.Cleanup(func() { _ = inst.Destroy(context.Background()) })
	layOut(t, ctx, inst, frameLayout)
	return inst
}

// sh runs a command as user ("" is the agent) and fails the test if it cannot run.
func sh(t *testing.T, ctx context.Context, inst core.Instance, user, cmd string) core.ExecResult {
	t.Helper()
	res, err := inst.Exec(ctx, core.ExecRequest{Command: cmd, User: user, TimeoutS: 30})
	if err != nil {
		t.Fatalf("exec %q: %v", cmd, err)
	}
	return res
}

func socketCtx(t *testing.T) context.Context {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	t.Cleanup(cancel)
	return ctx
}

// What the image gave as root's - its code, the system's folders - the agent cannot write: the
// write is refused (403), the file keeps its bytes, and no half-written temporary is left behind.
func TestSocketAgentCannotWriteRootOwnedFile(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-rootfile")
	for _, p := range []string{"/opt/side/code.py", "/opt/side/new.py", "/task/inputs/a.csv", "/task/planted.md", "/verify/job.json"} {
		if err := inst.WriteFile(ctx, p, []byte("evil"), 0o644, core.OwnerAgent); !errors.Is(err, core.ErrForbiddenPath) {
			t.Fatalf("agent write %s: want ErrForbiddenPath, got %v", p, err)
		}
	}
	// A body far past the socket's buffers, refused before the command reads a byte of it: the
	// refusal still comes back whole.
	big := bytes.Repeat([]byte("x"), 32<<20)
	if err := inst.WriteFile(ctx, "/opt/side/big.bin", big, 0o644, core.OwnerAgent); !errors.Is(err, core.ErrForbiddenPath) || !strings.Contains(err.Error(), "Permission denied") {
		t.Fatalf("a large refused write: want ErrForbiddenPath with its reason, got %v", err)
	}
	if got, err := inst.ReadFile(ctx, "/opt/side/code.py"); err != nil || string(got) != "print('shipped')\n" {
		t.Fatalf("the image's code is unchanged: %q %v", got, err)
	}
	if res := sh(t, ctx, inst, "", "ls -A /opt/side /task /task/inputs /verify"); strings.Join(strings.Fields(res.Stdout), " ") != "/opt/side: code.py /task: inputs /task/inputs: /verify:" {
		t.Fatalf("nothing new, no temporary: %q", res.Stdout)
	}
	if res := sh(t, ctx, inst, "", "stat -c '%u %a' /opt/side/code.py"); res.Stdout != "0 444\n" {
		t.Fatalf("still root's, read-only: %q", res.Stdout)
	}
}

// A link the agent plants into the browser user's closed profile carries no write there: the agent
// cannot enter it, so the write is refused (403) and the profile stays empty. A link to a place the
// agent may write is only the agent's own write: the kernel decides, never a list of paths.
func TestSocketAgentWriteThroughSymlinkToBrowserRefused(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-link")
	sh(t, ctx, inst, "", "ln -s /browser/profile /work/l && ln -s /artifacts /work/out")
	for _, p := range []string{"/work/l/x", "/work/l/sub/x", "/browser/profile/x", "/browser/x"} {
		if err := inst.WriteFile(ctx, p, []byte("cookie"), 0o644, core.OwnerAgent); !errors.Is(err, core.ErrForbiddenPath) {
			t.Fatalf("agent write %s: want ErrForbiddenPath, got %v", p, err)
		}
	}
	if res := sh(t, ctx, inst, "browser", "ls -A /browser /browser/profile"); strings.Join(strings.Fields(res.Stdout), " ") != "/browser: profile /browser/profile:" {
		t.Fatalf("the profile is untouched: %q %q", res.Stdout, res.Stderr)
	}
	if err := inst.WriteFile(ctx, "/work/out/note.md", []byte("mine"), 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("a link to the agent's own folder: %v", err)
	}
	if res := sh(t, ctx, inst, "", "stat -c '%u:%g %a' /artifacts/note.md && cat /artifacts/note.md"); res.Stdout != "1000:1000 644\nmine" {
		t.Fatalf("written as the agent, where the link points: %q", res.Stdout)
	}
}

// Every kind of place a caller gives a sandbox files keeps working as the system: the sources and
// the step marker, a skill (in a folder the agent made, and in one root makes), the loading dock, a
// runner in /tmp, a verifier's desk. Each is root's, 0444, read back exactly, and the agent cannot
// replace the runner in sticky /tmp. A parent the agent linked elsewhere is refused before anything
// is written; a final component the agent linked is replaced, never followed.
func TestSocketSystemWritesStillWork(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-system")
	sh(t, ctx, inst, "", "mkdir -p /skills/s")
	for _, p := range []string{
		"/task/inputs/a.csv", "/task/.step-start", "/skills/s/SKILL.md", "/skills/t/ref/x.md",
		"/artifacts/v.pdf", "/tmp/.plane-publish.py", "/verify/job.json",
	} {
		data := []byte("given " + p + "\x00\xff")
		if err := inst.WriteFile(ctx, p, data, 0o444, core.OwnerSystem); err != nil {
			t.Fatalf("system write %s: %v", p, err)
		}
		if got, err := inst.ReadFile(ctx, p); err != nil || !bytes.Equal(got, data) {
			t.Fatalf("read %s: %q %v", p, got, err)
		}
		if res := sh(t, ctx, inst, "", "stat -c '%u %a' "+shellQuote(p)); res.Stdout != "0 444\n" {
			t.Fatalf("%s is root's and read-only: %q", p, res.Stdout)
		}
	}
	if err := inst.WriteFile(ctx, "/tmp/.plane-publish.py", []byte("evil"), 0o644, core.OwnerAgent); !errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("the agent cannot replace root's runner: %v", err)
	}
	noTemporary(t, ctx, inst, "/tmp")

	sh(t, ctx, inst, "", "ln -s /opt/side /skills/evil && ln -s /opt/side/code.py /tmp/.plane-publish-args.json")
	if err := inst.WriteFile(ctx, "/skills/evil/x.py", []byte("evil"), 0o444, core.OwnerSystem); !errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("a symlinked parent: want ErrForbiddenPath, got %v", err)
	}
	if err := inst.WriteFile(ctx, "/tmp/.plane-publish-args.json", []byte("{}"), 0o444, core.OwnerSystem); err != nil {
		t.Fatalf("a linked final component: %v", err)
	}
	if res := sh(t, ctx, inst, "", "ls -A /opt/side; cat /opt/side/code.py; stat -c %F /tmp/.plane-publish-args.json"); res.Stdout != "code.py\nprint('shipped')\nregular file\n" {
		t.Fatalf("nothing landed through a link: %q", res.Stdout)
	}
}

// The checkpoint a sleeping sandbox keeps is the archive of /work: an agent's files survive the
// archive, the sandbox's end and a restore into a new one, still the agent's to edit.
func TestSocketAgentWriteArchiveRestoreRoundTrip(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	first := frameSandbox(t, ctx, b, "sbtest-ckpt")
	blob := bytes.Repeat([]byte{0, 1, 2, 0xff}, 1<<18)
	if err := first.WriteFile(ctx, "/work/a.txt", []byte("kept"), 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := first.WriteFile(ctx, "/work/sub/b.bin", blob, 0o600, core.OwnerAgent); err != nil {
		t.Fatalf("write: %v", err)
	}
	var snap bytes.Buffer
	if err := first.Archive(ctx, []string{"/work"}, &snap); err != nil {
		t.Fatalf("archive: %v", err)
	}
	if err := first.Destroy(ctx); err != nil {
		t.Fatalf("destroy: %v", err)
	}

	second := frameSandbox(t, ctx, b, "sbtest-ckpt")
	if err := second.Extract(ctx, &snap); err != nil {
		t.Fatalf("restore: %v", err)
	}
	if got, err := second.ReadFile(ctx, "/work/a.txt"); err != nil || string(got) != "kept" {
		t.Fatalf("a.txt: %q %v", got, err)
	}
	if got, err := second.ReadFile(ctx, "/work/sub/b.bin"); err != nil || !bytes.Equal(got, blob) {
		t.Fatalf("b.bin: %d bytes, %v", len(got), err)
	}
	// A directory's mode is the exec's umask (the Engine's to choose); its owner is the agent.
	if res := sh(t, ctx, second, "", "stat -c '%u %a' /work/a.txt /work/sub/b.bin; stat -c %u /work/sub"); strings.Join(strings.Fields(res.Stdout), " ") != "1000 644 1000 600 1000" {
		t.Fatalf("restored as the agent's: %q", res.Stdout)
	}
	if err := second.WriteFile(ctx, "/work/a.txt", []byte("edited"), 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("still the agent's to edit: %v", err)
	}
}

// noTemporary fails the test when an agent write left its fresh name (.sandbar-*) in dir.
func noTemporary(t *testing.T, ctx context.Context, inst core.Instance, dir string) {
	t.Helper()
	if res := sh(t, ctx, inst, "", "ls -A "+shellQuote(dir)); strings.Contains(res.Stdout, ".sandbar-") {
		t.Fatalf("a temporary was left in %s: %q", dir, res.Stdout)
	}
}

// An agent write aimed at a directory is refused - a failure, not a forbidden path - and leaves
// the directory as it was, with no temporary beside it.
func TestSocketAgentWriteOntoADirectoryLeavesNoTemporary(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-ontodir")
	sh(t, ctx, inst, "", "mkdir /work/d && echo kept > /work/d/f")
	if err := inst.WriteFile(ctx, "/work/d", []byte("x"), 0o644, core.OwnerAgent); err == nil || errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("a write onto a directory: want a failure, got %v", err)
	}
	noTemporary(t, ctx, inst, "/work")
	if got, err := inst.ReadFile(ctx, "/work/d/f"); err != nil || string(got) != "kept\n" {
		t.Fatalf("the directory is as it was: %q %v", got, err)
	}
}

// Every command root runs finds it in the system's own folders, not in one the image's PATH puts
// first: a stat the workload planted there, which would call a linked parent a directory, is never
// the one root runs, so the link is still refused and nothing lands through it.
func TestSocketRootIgnoresTheImagesPATH(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	spec := core.Spec{Image: "alpine:3.20", Env: map[string]string{"PATH": "/work/bin:/usr/sbin:/usr/bin:/sbin:/bin"}}
	inst, err := b.Create(ctx, "sbtest-path-"+randToken()[:6], spec.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	t.Cleanup(func() { _ = inst.Destroy(context.Background()) })
	layOut(t, ctx, inst, frameLayout)
	sh(t, ctx, inst, "", `mkdir /work/bin && printf '#!/bin/sh\nshift 3\nfor a do echo directory; done\n' > /work/bin/stat && chmod +x /work/bin/stat && ln -s /opt/side /skills/evil`)
	if err := inst.WriteFile(ctx, "/skills/evil/x.py", []byte("evil"), 0o444, core.OwnerSystem); !errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("a planted stat must not vouch for a link: want ErrForbiddenPath, got %v", err)
	}
	if res := sh(t, ctx, inst, "", "ls -A /opt/side"); res.Stdout != "code.py\n" {
		t.Fatalf("nothing landed through the link: %q", res.Stdout)
	}
}

// On a real Engine a command that cannot start is told on stdout (exit 127), not stderr: execAs
// keeps it, so the failure names the missing command instead of an output limit alone.
func TestSocketAStartFailureSaysWhy(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-start").(*instance)
	_, _, err := inst.execAs(ctx, core.AgentUID, []string{"/nonexistent"}, nil, io.Discard, 0)
	if !errors.Is(err, errOverLimit) || !strings.Contains(err.Error(), "/nonexistent") {
		t.Fatalf("a limit of none: want the Engine's reason, got %v", err)
	}
	exit, stderr, err := inst.execAs(ctx, core.AgentUID, []string{"/nonexistent"}, nil, io.Discard, 4<<10)
	if err != nil || exit != 127 || !strings.Contains(stderr, "/nonexistent") {
		t.Fatalf("under the limit: want exit 127 with the Engine's reason, got %d %q %v", exit, stderr, err)
	}
}

// ── A read on a real Engine: what the agent may read is the kernel's to decide ────────────────

// The browser user's closed profile, a file only root may read, and a link the agent plants into
// either are refused (403), and a refusal never carries their bytes. What the agent may read - the
// image's code, a link to its own folder - it still reads.
func TestSocketAgentCannotReadBrowserProfile(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-readprofile")
	sh(t, ctx, inst, "browser", "echo cookie-token > /browser/profile/Cookies")
	sh(t, ctx, inst, "", "ln -s /browser/profile/Cookies /work/c && ln -s /browser /work/b && ln -s /artifacts /work/out && echo mine > /artifacts/note.md")
	for _, p := range []string{"/browser/profile/Cookies", "/browser/profile", "/work/c", "/work/b/profile/Cookies", "/etc/shadow"} {
		got, err := inst.ReadFile(ctx, p)
		if !errors.Is(err, core.ErrForbiddenPath) || got != nil || strings.Contains(err.Error(), "cookie-token") {
			t.Fatalf("agent read %s: want ErrForbiddenPath, got %q %v", p, got, err)
		}
	}
	for p, want := range map[string]string{"/opt/side/code.py": "print('shipped')\n", "/work/out/note.md": "mine\n"} {
		if got, err := inst.ReadFile(ctx, p); err != nil || string(got) != want {
			t.Fatalf("agent read %s: %q %v", p, got, err)
		}
	}
	for _, p := range []string{"/work", "/work/none", "/work/out/none/x"} {
		if _, err := inst.ReadFile(ctx, p); !errors.Is(err, core.ErrNoSuchPath) {
			t.Fatalf("agent read %s: want ErrNoSuchPath, got %v", p, err)
		}
	}
}

// heapGrowth runs f and reports how far the heap rose above where it began while f ran.
func heapGrowth(f func() error) (uint64, error) {
	runtime.GC()
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	base, peak := ms.HeapAlloc, ms.HeapAlloc
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		tick := time.NewTicker(5 * time.Millisecond)
		defer tick.Stop()
		for {
			var ms runtime.MemStats
			runtime.ReadMemStats(&ms)
			peak = max(peak, ms.HeapAlloc)
			select {
			case <-done:
				return
			case <-tick.C:
			}
		}
	}()
	err := f()
	close(done)
	wg.Wait()
	return peak - base, err
}

// A file over the bound is refused before a byte of it is read, so the daemon never holds it: a
// 600 MiB (sparse) file is refused at the look. A file whose size the kernel does not know (a /proc
// file that streams without end, its size 0) is read to the size it says - nothing - never to the
// end of its stream.
func TestSocketReadOverBoundRefused(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-readbig")
	sh(t, ctx, inst, "", "truncate -s 600M /work/big")
	grew, err := heapGrowth(func() error { _, err := inst.ReadFile(ctx, "/work/big"); return err })
	if err == nil || errors.Is(err, core.ErrNoSuchPath) || !strings.Contains(err.Error(), "629145600 bytes, over the 536870912-byte bound") {
		t.Fatalf("a file over the bound: want the bound's refusal, got %v", err)
	}
	if grew > 16<<20 {
		t.Fatalf("refused before it was read, yet the heap grew %d MiB", grew>>20)
	}

	var got []byte
	grew, err = heapGrowth(func() (err error) { got, err = inst.ReadFile(ctx, "/proc/self/pagemap"); return err })
	if err != nil || len(got) != 0 {
		t.Fatalf("a stream without end: want the nothing its size says, got %d bytes, %v", len(got), err)
	}
	if grew > 16<<20 {
		t.Fatalf("a stream without end was read past its size: the heap grew %d MiB", grew>>20)
	}
}

// Fifty MiB round trip through the agent's own write and read, byte for byte, and the read holds
// the file once: one buffer made for the size the look saw.
func TestSocketFiftyMiBRoundTrip(t *testing.T) {
	b := dockerOrSkip(t)
	ctx := socketCtx(t)
	inst := frameSandbox(t, ctx, b, "sbtest-fifty")
	blob := make([]byte, 50<<20)
	_, _ = rand.Read(blob)
	if err := inst.WriteFile(ctx, "/work/blob.bin", blob, 0o600, core.OwnerAgent); err != nil {
		t.Fatalf("write: %v", err)
	}
	var got []byte
	grew, err := heapGrowth(func() (err error) { got, err = inst.ReadFile(ctx, "/work/blob.bin"); return err })
	if err != nil || !bytes.Equal(got, blob) {
		t.Fatalf("read: %d bytes, %v", len(got), err)
	}
	if grew > uint64(len(blob))+16<<20 {
		t.Fatalf("a %d MiB read grew the heap %d MiB: it must hold the file once", len(blob)>>20, grew>>20)
	}
}
