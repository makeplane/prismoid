// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/egress"
)

// These tests drive a REAL Docker daemon - the docker backend's whole job is talking to
// it, so a mock would test nothing. Skipped when the socket is absent (CI without Docker,
// or a reviewer's laptop), so `go test ./...` stays green everywhere.
func dockerSockOrSkip(t *testing.T) string {
	t.Helper()
	sock := "/var/run/docker.sock"
	if s := os.Getenv("SANDBAR_DOCKER_SOCK"); s != "" {
		sock = s
	}
	if _, err := os.Stat(sock); err != nil {
		t.Skipf("no docker socket at %s; skipping", sock)
	}
	return sock
}

func dockerOrSkip(t *testing.T) *Backend {
	t.Helper()
	return New(dockerSockOrSkip(t))
}

func TestDockerRoundTrip(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	inst, err := b.Create(ctx, "sbtest-roundtrip", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())
	layOut(t, ctx, inst, workshop) // an agent's write runs as the agent: /work must be its own

	// exec: exit codes are results, not errors.
	res, err := inst.Exec(ctx, core.ExecRequest{Command: "echo hello && exit 3"})
	if err != nil {
		t.Fatalf("exec: %v", err)
	}
	if res.ExitCode != 3 || res.Stdout != "hello\n" {
		t.Fatalf("exec result wrong: %+v", res)
	}

	// write a system file read-only, prove ownership + mode, then read it back binary-exact.
	payload := []byte("source bytes\x00\xff") // includes non-UTF-8: read_bytes must not mangle
	if err := inst.WriteFile(ctx, "/task/inputs/src.bin", payload, 0o444, core.OwnerSystem); err != nil {
		t.Fatalf("write: %v", err)
	}
	got, err := inst.ReadFile(ctx, "/task/inputs/src.bin")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("read mismatch: %q != %q", got, payload)
	}
	own, _ := inst.Exec(ctx, core.ExecRequest{Command: "stat -c '%U %a' /task/inputs/src.bin"})
	if own.Stdout != "root 444\n" {
		t.Fatalf("system file should be root 444, got %q", own.Stdout)
	}

	// agent file is the agent's to edit.
	if err := inst.WriteFile(ctx, "/work/draft.txt", []byte("mine"), 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("write agent: %v", err)
	}
	agentOwn, _ := inst.Exec(ctx, core.ExecRequest{Command: "stat -c '%u' /work/draft.txt"})
	if agentOwn.Stdout != "1000\n" {
		t.Fatalf("agent file should be uid 1000, got %q", agentOwn.Stdout)
	}

	// list_dir marks directories with a trailing slash.
	entries, err := inst.ListDir(ctx, "/task")
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if !contains(entries, "inputs/") {
		t.Fatalf("expected inputs/ in %v", entries)
	}

	// a name the URL would mangle raw round-trips exactly (escaped on the way to the Engine).
	odd := "/work/c++ #1 a&b 100%.md"
	if err := inst.WriteFile(ctx, odd, []byte("odd"), 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("write %s: %v", odd, err)
	}
	if got, err := inst.ReadFile(ctx, odd); err != nil || string(got) != "odd" {
		t.Fatalf("read %s: %q %v", odd, got, err)
	}

	// a path that is not there is ErrNoSuchPath on both reads (what the played Engine in
	// Test…TellsAMissingPath… answers), and a permission refusal is not.
	for _, p := range []string{"/task/context.md", "/etc/hostname/x", "/root"} {
		if _, err := inst.ReadFile(ctx, p); !errors.Is(err, core.ErrNoSuchPath) {
			t.Fatalf("read %s: want ErrNoSuchPath, got %v", p, err)
		}
	}
	for _, p := range []string{"/nope", "/etc/hostname/x"} {
		if _, err := inst.ListDir(ctx, p); !errors.Is(err, core.ErrNoSuchPath) {
			t.Fatalf("list %s: want ErrNoSuchPath, got %v", p, err)
		}
	}
	if _, err := inst.ListDir(ctx, "/root"); !errors.Is(err, core.ErrForbiddenPath) {
		t.Fatalf("list /root as the agent: want ErrForbiddenPath, got %v", err)
	}

	// archive → extract round-trip: snapshot /work, wipe it, restore, file returns.
	var snap bytes.Buffer
	if err := inst.Archive(ctx, []string{"/work"}, &snap); err != nil {
		t.Fatalf("archive: %v", err)
	}
	if _, err := inst.Exec(ctx, core.ExecRequest{Command: "rm -rf /work/draft.txt"}); err != nil {
		t.Fatalf("wipe: %v", err)
	}
	if err := inst.Extract(ctx, &snap); err != nil {
		t.Fatalf("extract: %v", err)
	}
	restored, err := inst.ReadFile(ctx, "/work/draft.txt")
	if err != nil || string(restored) != "mine" {
		t.Fatalf("restore failed: %q %v", restored, err)
	}

	// an absent directory archives as a complete empty tar (what a real Engine's not-found for a
	// path in a live container becomes - TestArchiveOfAnAbsentPath… plays the same answer).
	var none bytes.Buffer
	if err := inst.Archive(ctx, []string{"/browser"}, &none); err != nil {
		t.Fatalf("archive of an absent path: %v", err)
	}
	if !bytes.Equal(none.Bytes(), make([]byte, 1024)) {
		t.Fatalf("an absent path must be a complete empty tar, got %d bytes", none.Len())
	}
}

func TestDockerNoNetwork(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	inst, err := b.Create(ctx, "sbtest-nonet", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())

	// Deny-by-default: with NetworkMode=none there is no route out at all.
	res, _ := inst.Exec(ctx, core.ExecRequest{
		Command: "ip route 2>/dev/null | wc -l", TimeoutS: 20,
	})
	if got := res.Stdout; got != "0\n" {
		t.Fatalf("sandbox must have no default route, got %q routes", got)
	}
}

// TestNoExecRunsTheAgentsStartupFile: a frame's HOME is the agent's workshop (Plane's image sets
// HOME=/work), so a login shell would run whatever `.profile` the agent left there - as root, on
// the next privileged mkdir. The agent's own write is enough to plant one, so no exec may read it.
func TestNoExecRunsTheAgentsStartupFile(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	spec := core.Spec{Image: "alpine:3.20", Env: map[string]string{"HOME": "/work"}}.WithDefaults()
	inst, err := b.Create(ctx, "sbtest-profile", spec)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())
	layOut(t, ctx, inst, workshop)

	plant := []byte("id -u > /tmp/profile-ran-$(id -u)\n")
	if err := inst.WriteFile(ctx, "/work/.profile", plant, 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("plant: %v", err)
	}
	// A system file's write runs the root mkdir; an agent exec follows it.
	if err := inst.WriteFile(ctx, "/task/inputs/src.md", []byte("source"), 0o444, core.OwnerSystem); err != nil {
		t.Fatalf("write: %v", err)
	}
	ran, err := inst.Exec(ctx, core.ExecRequest{Command: "cat /tmp/profile-ran-* 2>/dev/null; true"})
	if err != nil {
		t.Fatalf("exec: %v", err)
	}
	if ran.Stdout != "" {
		t.Errorf("the agent's .profile ran, as uid(s) %q", ran.Stdout)
	}

	home, err := inst.(*instance).execAsRoot(ctx, "printenv", "HOME")
	if err != nil || home.Stdout != "/root\n" {
		t.Errorf("a root command must run with root's HOME, got %q (%v)", home.Stdout, err)
	}
}

// TestBrowserSandboxRoundTrip drives the sidecar through sandbar's Docker backend end to end:
// a fixture page served on loopback, opened by the sidecar as the browser user (proving its
// SIDECAR_CMD env reaches it and only it - the env-isolation assertion the browser test always
// made), then snapshotted back through the same exec route.
func TestBrowserSandboxRoundTrip(t *testing.T) {
	b := dockerOrSkip(t)
	image := os.Getenv("SANDBAR_BROWSER_IMAGE")
	if image == "" {
		t.Skip("SANDBAR_BROWSER_IMAGE not set; run `just sandbar-browser-image` and export the tag")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	inst, err := b.Create(ctx, "sb-browser-"+randToken()[:6], core.Spec{Image: image})
	if err != nil {
		t.Fatal(err)
	}
	// Background, not ctx: a timed-out test must still delete its container, the same pattern
	// every sibling test in this file uses (ctx's own 2-minute deadline is exactly what would
	// have fired).
	defer inst.Destroy(context.Background())
	page := []byte(`<!doctype html><title>Orders</title><h1>Orders</h1><button>Save changes</button>`)
	if err := inst.WriteFile(ctx, "/work/site/index.html", page, 0o644, core.OwnerAgent); err != nil {
		t.Fatal(err)
	}
	if _, err := inst.Exec(ctx, core.ExecRequest{Command: "cd /work/site && (python3 -m http.server 8088 >/dev/null 2>&1 &) && sleep 0.5"}); err != nil {
		t.Fatal(err)
	}
	sidecar := func(cmd string) map[string]any {
		res, err := inst.Exec(ctx, core.ExecRequest{
			Command: "python3 -m sidecar.ctl", User: "browser", TimeoutS: 60,
			Env: map[string]string{"SIDECAR_CMD": cmd, "PYTHONPATH": "/opt/plane/browser", "HOME": "/home/browser"},
		})
		if err != nil {
			t.Fatal(err)
		}
		lines := strings.Split(strings.TrimSpace(res.Stdout), "\n")
		var out map[string]any
		if err := json.Unmarshal([]byte(lines[len(lines)-1]), &out); err != nil {
			t.Fatalf("sidecar reply is not JSON: %q (stderr %q)", res.Stdout, res.Stderr)
		}
		return out
	}
	opened := sidecar(`{"verb":"open","url":"http://127.0.0.1:8088/index.html"}`)
	if opened["ok"] != true || opened["title"] != "Orders" {
		t.Fatalf("open: %v", opened)
	}
	snap := sidecar(`{"verb":"snapshot"}`)
	if snap["ok"] != true || !strings.Contains(snap["text"].(string), `heading "Orders"`) {
		t.Fatalf("snapshot: %v", snap)
	}

	// isolation: the agent (uid 1000) must never read what the browser user (uid 1001) owns -
	// cookies are tokens (image_browser/Dockerfile chmods /browser/profile 0700 browser:browser) -
	// and root is not a principal ExecRequest.UID() ever grants.
	agentPeek, err := inst.Exec(ctx, core.ExecRequest{Command: "ls /browser/profile"})
	if err != nil {
		t.Fatal(err)
	}
	if agentPeek.ExitCode == 0 {
		t.Fatalf("agent must not read the browser profile: %+v", agentPeek)
	}
	if _, err := inst.Exec(ctx, core.ExecRequest{Command: "id", User: "root"}); err == nil {
		t.Fatal("unknown user must be refused")
	}
}

// TestBrowserSandboxThroughEgressProxy proves the sidecar's proxy-credential answer end to end: with the
// container on an egress allowlist, Chromium's ONLY route out is sandbar's proxy, so an "open"
// that comes back ok can only mean the sidecar answered the proxy's CONNECT credential challenge
// over CDP Fetch (never argv - see image_browser/sidecar/browser.py's _proxy_arg docstring). It
// wires a real *egress.Proxy exactly like TestDockerEgressLiveUpdate, points a browser-image
// sandbox at it with one allowed host, and shows the allowed host opens while a second,
// non-allowlisted host comes back "egress refused".
func TestBrowserSandboxThroughEgressProxy(t *testing.T) {
	image := os.Getenv("SANDBAR_BROWSER_IMAGE")
	if image == "" {
		t.Skip("SANDBAR_BROWSER_IMAGE not set; run `just sandbar-browser-image` and export the tag")
	}
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	eg, port := liveProxy(t, ctx, sock)

	b := egressBackend(t, sock, eg, relayOn(t, sock, port))
	inst, err := b.Create(ctx, "sb-browser-egress-"+randToken()[:6], core.Spec{
		Image: image, AllowDomains: []string{"example.com"},
	}.WithDefaults())
	if err != nil {
		t.Fatal(err)
	}
	// Background, not ctx: a timed-out test must still delete its container, the same pattern
	// every sibling test in this file uses (ctx's own 2-minute deadline is exactly what would
	// have fired).
	defer inst.Destroy(context.Background())

	sidecar := func(cmd string) map[string]any {
		res, err := inst.Exec(ctx, core.ExecRequest{
			Command: "python3 -m sidecar.ctl", User: "browser", TimeoutS: 60,
			Env: map[string]string{"SIDECAR_CMD": cmd, "PYTHONPATH": "/opt/plane/browser", "HOME": "/home/browser"},
		})
		if err != nil {
			t.Fatal(err)
		}
		lines := strings.Split(strings.TrimSpace(res.Stdout), "\n")
		var out map[string]any
		if err := json.Unmarshal([]byte(lines[len(lines)-1]), &out); err != nil {
			t.Fatalf("sidecar reply is not JSON: %q (stderr %q)", res.Stdout, res.Stderr)
		}
		return out
	}

	allowed := sidecar(`{"verb":"open","url":"https://example.com/"}`)
	t.Logf("open (allowed host): %v", allowed)
	if allowed["ok"] != true {
		// "navigation failed" can never actually appear here: offline, the Go proxy's own dial
		// to example.com fails, which the proxy answers with a 502 - Chromium turns THAT into
		// ERR_TUNNEL_CONNECTION_FAILED, the exact same PROXY_ERRORS code a real "egress refused"
		// uses (browser.py's _navigation_error), so a dead network and a real credential/403
		// failure are indistinguishable from the sidecar reply alone. Ask the host directly.
		if _, dnsErr := net.LookupHost("example.com"); dnsErr != nil {
			t.Skipf("no internet on this box: %v", dnsErr)
		}
		t.Fatalf("open (allowed host): %v", allowed)
	}
	if allowed["title"] != "Example Domain" {
		t.Fatalf("open (allowed host) title: %v", allowed)
	}

	denied := sidecar(`{"verb":"open","url":"https://iana.org/"}`)
	t.Logf("open (denied host): %v", denied)
	if denied["ok"] != false || denied["error"] != "egress refused" {
		t.Fatalf("open (denied host): %v", denied)
	}
}

// spyEgress is the "tests can supply a spy" the Egress interface promises - no Docker socket
// needed, since instance.Denials only touches b.eg and i.token.
type spyEgress struct{ denials []egress.Denial }

func (s *spyEgress) Allow(string, []string)         {}
func (s *spyEgress) Revoke(string)                  {}
func (s *spyEgress) Denials(string) []egress.Denial { return s.denials }

// The bug this pins: a field-by-field copy from egress.Denial to core.Denial once dropped
// Reason silently, so a caller reading core.Denial (what the API and any consumer of
// Instance.Denials sees) never learned WHY - only that something was refused.
func TestInstanceDenialsCarryReason(t *testing.T) {
	spy := &spyEgress{denials: []egress.Denial{
		{Host: "10.0.0.5", Method: "CONNECT", Reason: "private address", At: time.Now()},
	}}
	inst := &instance{b: &Backend{eg: spy}, token: "tok-1"}

	got, err := inst.Denials(context.Background())
	if err != nil {
		t.Fatalf("Denials: %v", err)
	}
	if len(got) != 1 || got[0].Reason != "private address" {
		t.Fatalf("expected core.Denial to carry Reason, got %+v", got)
	}
}

// TestEgressRefusedOnceClosed pins the destroy race directly: a `PUT /egress` that lands in the
// window between Registry.Destroy revoking the token and the (slow) Docker DELETE completing
// must never re-register the allowlist through a now-revoked token - that would resurrect it
// forever, since nothing ever revokes it again. instance.closed is set before Revoke for exactly
// this reason; no Docker socket is needed to prove Egress honours it, the same way
// TestInstanceDenialsCarryReason drives `instance` directly with a spy.
func TestEgressRefusedOnceClosed(t *testing.T) {
	spy := &spyEgress{}
	inst := &instance{b: &Backend{eg: spy}, token: "tok-1", closed: true}

	err := inst.Egress(context.Background(), []string{"example.com"})
	if err == nil {
		t.Fatal("expected Egress to refuse once the instance is closed")
	}
	if !errors.Is(err, core.ErrBadSpec) {
		t.Fatalf("expected core.ErrBadSpec, got %v", err)
	}
	if !strings.Contains(err.Error(), "being destroyed") {
		t.Fatalf("expected the error to name the reason, got %v", err)
	}
}

// TestANoDomainSandboxStaysAirtightEvenWithTheProxyOn pins the posture a principal would break:
// a principal on an airtight sandbox would be a way to widen it (the proxy is only an env var), so a
// sandbox born with no allow_domains must stay network=none - no route at all - and refuse a
// widen (ErrBadSpec, the API's 400), even on a daemon that runs the proxy. A sandbox born WITH
// domains has a principal and widens.
func TestANoDomainSandboxStaysAirtightEvenWithTheProxyOn(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	b := egressBackend(t, sock, &spyEgress{}, standInRelay(t, sock))

	airtight, err := b.Create(ctx, "sbtest-egress-empty", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer airtight.Destroy(context.Background())
	res, err := airtight.Exec(ctx, core.ExecRequest{Command: "ip route 2>/dev/null | wc -l", TimeoutS: 20})
	if err != nil {
		t.Fatalf("exec: %v", err)
	}
	if res.Stdout != "0\n" {
		t.Fatalf("a sandbox born with no domains must have no route out, got %q routes", res.Stdout)
	}
	if err := airtight.Egress(ctx, []string{"a.example"}); !errors.Is(err, core.ErrBadSpec) ||
		!strings.Contains(err.Error(), "no egress") {
		t.Fatalf("widening an airtight sandbox must be refused as no egress, got %v", err)
	}

	widenable, err := b.Create(ctx, "sbtest-egress-born", core.Spec{
		Image: "alpine:3.20", AllowDomains: []string{"a.example"},
	}.WithDefaults())
	if err != nil {
		t.Fatalf("create with domains: %v", err)
	}
	defer widenable.Destroy(context.Background())
	if err := widenable.Egress(ctx, []string{"a.example", "b.example"}); err != nil {
		t.Fatalf("a sandbox born with domains must be widenable: %v", err)
	}
}

// TestDockerEgressLiveUpdate boots a real container behind a real egress proxy, proves a
// non-allowlisted host is denied, then calls Egress to widen the allowlist while the sandbox
// keeps running, and proves the same host stops being denied - without destroying and
// recreating the sandbox, and without the token (the proxy principal) ever changing.
func TestDockerEgressLiveUpdate(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	eg, port := liveProxy(t, ctx, sock)

	b := egressBackend(t, sock, eg, relayOn(t, sock, port))
	inst, err := b.Create(ctx, "sbtest-egress-live", core.Spec{
		Image: "alpine:3.20", AllowDomains: []string{"example.com"},
	}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())

	const deniedHost = "denied.invalid.example"
	fetch := func() {
		// Plain HTTP through the container's HTTP_PROXY env - the proxy decides allow/deny
		// before it ever resolves or dials the target, so this needs no real DNS for
		// deniedHost to exercise the allowlist check both before and after widening.
		if _, err := inst.Exec(ctx, core.ExecRequest{
			Command:  "wget -qO- --timeout=10 http://" + deniedHost + "/ >/dev/null 2>&1; true",
			TimeoutS: 20,
		}); err != nil {
			t.Fatalf("exec: %v", err)
		}
	}
	countDenials := func() int {
		denials, err := inst.Denials(ctx)
		if err != nil {
			t.Fatalf("denials: %v", err)
		}
		n := 0
		for _, d := range denials {
			if d.Host == deniedHost {
				n++
			}
		}
		return n
	}

	fetch()
	before := countDenials()
	if before == 0 {
		t.Fatalf("expected a denial for %s before widening the allowlist, got none", deniedHost)
	}

	if err := inst.Egress(ctx, []string{"example.com", deniedHost}); err != nil {
		t.Fatalf("egress update: %v", err)
	}

	fetch()
	after := countDenials()
	if after != before {
		t.Fatalf("widened allowlist must not record a new denial for %s: before=%d after=%d", deniedHost, before, after)
	}
}

// TestDestroyThenEgressIsRefused drives the actual destroy race end to end (TestEgressRefusedOnceClosed
// pins the guard in isolation): destroy a real container, then call Egress on the same *instance -
// exactly what a PUT /egress landing in Registry.Destroy's window would do - and prove it is refused
// instead of re-registering a token whose container is gone (and which nothing would ever revoke again).
func TestDestroyThenEgressIsRefused(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	eg, _ := liveProxy(t, ctx, sock)

	b := egressBackend(t, sock, eg, standInRelay(t, sock))
	inst, err := b.Create(ctx, "sbtest-egress-destroy-race", core.Spec{
		Image: "alpine:3.20", AllowDomains: []string{"example.com"},
	}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}

	if err := inst.Destroy(ctx); err != nil {
		t.Fatalf("destroy: %v", err)
	}

	if err := inst.Egress(ctx, []string{"example.com", "evil.invalid.example"}); err == nil {
		t.Fatal("expected Egress on a destroyed instance to be refused")
	} else if !errors.Is(err, core.ErrBadSpec) {
		t.Fatalf("expected core.ErrBadSpec, got %v", err)
	}
}

// ── Archive's error paths, against a played Engine (no socket needed) ────────────────────

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// failingReader is an Engine body the connection drops under.
type failingReader struct{ err error }

func (f failingReader) Read([]byte) (int, error) { return 0, f.err }

// playedEngine answers GET .../archive with `archive` and GET .../json with `inspect`.
func playedEngine(archive func() (int, io.Reader), inspect int) *instance {
	b := &Backend{http: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		code, body := inspect, io.Reader(strings.NewReader(`{"message":"No such container: c1"}`))
		if strings.HasSuffix(r.URL.Path, "/archive") {
			code, body = archive()
		} else if inspect == http.StatusOK {
			body = strings.NewReader(`{"Id":"c1"}`)
		}
		return &http.Response{StatusCode: code, Body: io.NopCloser(body), Header: http.Header{}, Request: r}, nil
	})}}
	return &instance{b: b, cid: "c1"}
}

// oneEntryTar is a complete tar of one 600-byte file, and the same stream with its trailer cut.
func oneEntryTar(t *testing.T) (full, cut []byte) {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := tw.WriteHeader(&tar.Header{Name: "work/a.txt", Mode: 0o644, Size: 600, Typeflag: tar.TypeReg}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(bytes.Repeat([]byte("a"), 600)); err != nil {
		t.Fatal(err)
	}
	if err := tw.Flush(); err != nil {
		t.Fatal(err)
	}
	cut = append([]byte(nil), buf.Bytes()...) // header + padded content, no trailer yet
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes(), cut
}

var trailer = make([]byte, 1024) // a tar's end: two zero blocks

// The bug this pins: a deferred tw.Close() once wrote the trailer on EVERY return, so an Engine
// failure became a complete-looking tar - an empty or cut-short checkpoint the caller took for
// the whole of /work. On failure the stream must end without a trailer.
func TestArchiveThatFailsWritesNoTrailer(t *testing.T) {
	_, cut := oneEntryTar(t)
	cases := map[string]func() (int, io.Reader){
		"engine refuses": func() (int, io.Reader) {
			return http.StatusInternalServerError, strings.NewReader(`{"message":"boom"}`)
		},
		"cut between entries": func() (int, io.Reader) {
			return http.StatusOK, io.MultiReader(bytes.NewReader(cut), failingReader{io.ErrUnexpectedEOF})
		},
		"cut mid-entry": func() (int, io.Reader) {
			return http.StatusOK, io.MultiReader(bytes.NewReader(cut[:700]), failingReader{io.ErrUnexpectedEOF})
		},
	}
	for name, archive := range cases {
		t.Run(name, func(t *testing.T) {
			var out bytes.Buffer
			if err := playedEngine(archive, http.StatusOK).Archive(context.Background(), []string{"/work"}, &out); err == nil {
				t.Fatal("a failed Engine read must be an error")
			}
			if bytes.HasSuffix(out.Bytes(), trailer) {
				t.Fatalf("a failed archive must not end in a tar trailer (%d bytes written)", out.Len())
			}
		})
	}
}

// An ABSENT directory is the one not-found that is not a failure (the Python driver's contract,
// the Docker driver's behaviour): the container answers, the path does not, so the archive is a
// complete empty tar. A vanished CONTAINER is a failure like any other.
func TestArchiveOfAnAbsentPathIsEmptyButAVanishedContainerFails(t *testing.T) {
	missing := func() (int, io.Reader) {
		return http.StatusNotFound, strings.NewReader(`{"message":"Could not find the file /browser in container c1"}`)
	}
	var out bytes.Buffer
	if err := playedEngine(missing, http.StatusOK).Archive(context.Background(), []string{"/browser"}, &out); err != nil {
		t.Fatalf("an absent path must archive as empty: %v", err)
	}
	if !bytes.Equal(out.Bytes(), trailer) {
		t.Fatalf("an absent path must be a complete empty tar, got %d bytes", out.Len())
	}

	out.Reset()
	if err := playedEngine(missing, http.StatusNotFound).Archive(context.Background(), []string{"/work"}, &out); err == nil {
		t.Fatal("a vanished container must be an error, not an empty archive")
	}
	if out.Len() != 0 {
		t.Fatalf("a vanished container must write nothing, got %d bytes", out.Len())
	}
}

// A complete archive still ends in its trailer.
func TestArchiveOfAPresentPathIsComplete(t *testing.T) {
	full, _ := oneEntryTar(t)
	var out bytes.Buffer
	ok := func() (int, io.Reader) { return http.StatusOK, bytes.NewReader(full) }
	if err := playedEngine(ok, http.StatusOK).Archive(context.Background(), []string{"/work"}, &out); err != nil {
		t.Fatalf("archive: %v", err)
	}
	tr := tar.NewReader(&out)
	hdr, err := tr.Next()
	if err != nil || hdr.Name != "work/a.txt" {
		t.Fatalf("first entry: %v %v", hdr, err)
	}
	if _, err := tr.Next(); err != io.EOF {
		t.Fatalf("a complete archive ends cleanly, got %v", err)
	}
}

// ── A missing path is the caller's answer, never a failure (RR-missing-path) ────────────────
// (a read's cases: TestReadMissingIsNoSuchPath and its neighbours in files_test.go)

// execEngine plays the three Engine calls an exec makes: create (createCode; 404 is a vanished
// container), start (stdout and stderr in Docker's multiplexed framing), inspect (exit). With
// hangFirst, the first exec's start never ends until its request is cancelled: a command that
// outlives its timeout. With then, the execs after the first answer then's exit, stdout and stderr
// in turn, the last for every one past it. play keeps every exec's create body.
type execEngine struct {
	createCode, exit int
	stdout, stderr   string
	hangFirst        bool
	then             []execEngine
}

// answer is what the k-th exec (from 0) answers.
func (e execEngine) answer(k int) execEngine {
	if k == 0 || len(e.then) == 0 {
		return e
	}
	return e.then[min(k, len(e.then))-1]
}

type execCreate struct {
	User string   `json:"User"`
	Env  []string `json:"Env"`
	Cmd  []string `json:"Cmd"`
}

func (e execEngine) play() (*instance, func() []execCreate) {
	var mu sync.Mutex
	var creates []execCreate
	starts := 0
	frame := func(stream byte, s string) []byte {
		if s == "" {
			return nil
		}
		hdr := []byte{stream, 0, 0, 0, 0, 0, 0, 0}
		binary.BigEndian.PutUint32(hdr[4:], uint32(len(s)))
		return append(hdr, s...)
	}
	b := &Backend{http: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		code, body := http.StatusOK, ""
		switch {
		case strings.HasSuffix(r.URL.Path, "/containers/c1/exec"):
			var c execCreate
			_ = json.NewDecoder(r.Body).Decode(&c)
			mu.Lock()
			creates = append(creates, c)
			mu.Unlock()
			code, body = e.createCode, `{"Id":"e1"}`
			if e.createCode == http.StatusNotFound {
				body = `{"message":"No such container: c1"}`
			}
		case strings.HasSuffix(r.URL.Path, "/exec/e1/start"):
			mu.Lock()
			starts++
			hang := e.hangFirst && starts == 1
			a := e.answer(len(creates) - 1)
			mu.Unlock()
			if hang {
				<-r.Context().Done()
				return nil, r.Context().Err()
			}
			body = string(append(frame(1, a.stdout), frame(2, a.stderr)...))
		case strings.HasSuffix(r.URL.Path, "/exec/e1/json"):
			mu.Lock()
			a := e.answer(len(creates) - 1)
			mu.Unlock()
			body = fmt.Sprintf(`{"ExitCode":%d,"Running":false}`, a.exit)
		}
		return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}, Request: r}, nil
	})}}
	return &instance{b: b, cid: "c1"}, func() []execCreate {
		mu.Lock()
		defer mu.Unlock()
		return append([]execCreate(nil), creates...)
	}
}

// lsEngine is the played exec engine ListDir's tests need: one exec, its output and exit code.
func lsEngine(createCode, exit int, stdout, stderr string) *instance {
	inst, _ := execEngine{createCode: createCode, exit: exit, stdout: stdout, stderr: stderr}.play()
	return inst
}

func TestListDirTellsAMissingPathFromAVanishedContainerAndAFailure(t *testing.T) {
	cases := []struct {
		name      string
		inst      *instance
		missing   bool
		forbidden bool
	}{
		{"missing path (busybox)", lsEngine(201, 1, "", "ls: /nope: No such file or directory\n"), true, false},
		{"missing path (coreutils)", lsEngine(201, 2, "", "ls: cannot access '/nope': No such file or directory\n"), true, false},
		{"a parent is a file", lsEngine(201, 1, "", "ls: /etc/hostname/x: Not a directory\n"), true, false},
		// The agent's refusal, as firecracker answers it: 403 forbidden_path on the wire, never a 502.
		{"permission denied (busybox)", lsEngine(201, 1, "", "ls: can't open '/root': Permission denied\n"), false, true},
		{"permission denied (coreutils)", lsEngine(201, 2, "", "ls: cannot open directory '/root': Permission denied\n"), false, true},
		{"a failure", lsEngine(201, 1, "", "ls: reading directory '/nope': Input/output error\n"), false, false},
		{"vanished container", lsEngine(http.StatusNotFound, 0, "", ""), false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := c.inst.ListDir(context.Background(), "/nope")
			if err == nil {
				t.Fatal("want an error")
			}
			if got := errors.Is(err, core.ErrNoSuchPath); got != c.missing {
				t.Fatalf("errors.Is(ErrNoSuchPath) = %v, want %v (err: %v)", got, c.missing, err)
			}
			if got := errors.Is(err, core.ErrForbiddenPath); got != c.forbidden {
				t.Fatalf("errors.Is(ErrForbiddenPath) = %v, want %v (err: %v)", got, c.forbidden, err)
			}
		})
	}

	inst, creates := execEngine{createCode: 201, stdout: "a.txt\nsub/\n"}.play()
	entries, err := inst.ListDir(context.Background(), "/work")
	if err != nil || len(entries) != 2 || entries[1] != "sub/" {
		t.Fatalf("a present directory lists: %v %v", entries, err)
	}
	// ls's errno words are what tell a missing path from a refusal: pinned to the C locale, so a
	// frame image with another LANG cannot turn a missing path back into a failure.
	if c := creates(); len(c) != 1 || len(c[0].Cmd) != 3 || !strings.HasPrefix(c[0].Cmd[2], "LC_ALL=C ls ") {
		t.Fatalf("ls must run in the C locale, ran %+v", c)
	}
}

// pathEngine plays every Engine call a file verb makes and keeps the `path` each archive request
// carried, decoded the way the Engine decodes it. Every exec answers that each of three components
// is a directory: the look before a system write a level below /work/<name>.
func pathEngine(t *testing.T, full []byte) (*instance, *[]string) {
	t.Helper()
	var seen []string
	b := &Backend{http: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		body := io.Reader(strings.NewReader(`{"Id":"e1"}`))
		switch {
		case strings.HasSuffix(r.URL.Path, "/archive"):
			seen = append(seen, r.URL.Query().Get("path"))
			body = bytes.NewReader(full)
		case strings.HasSuffix(r.URL.Path, "/exec/e1/start"):
			body = bytes.NewReader(frame(1, strings.Repeat("directory\n", 3)))
		}
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(body), Header: http.Header{}, Request: r}, nil
	})}}
	return &instance{b: b, cid: "c1"}, &seen
}

// Fix round 1: a path went into the Engine URL raw, so `c++.md` arrived as `c  .md`, `a#b.md` and
// `a&b.md` as `a`, `100%.md` not at all - and with no_such_path the model was told a file it had
// just written does not exist. Every verb that names a path must hand the Engine that exact path.
func TestEveryPathReachesTheEngineExactlyAsGiven(t *testing.T) {
	full, _ := oneEntryTar(t)
	for _, name := range []string{"c++.md", "a#b.md", "a&b.md", "100%.md", "two words.md"} {
		t.Run(name, func(t *testing.T) {
			p := "/work/" + name
			inst, seen := pathEngine(t, full)
			if err := inst.Archive(context.Background(), []string{p}, io.Discard); err != nil {
				t.Fatalf("archive: %v", err)
			}
			// A system write is the one file verb that still names a path to the Engine (a read and
			// an agent's write ride as exec arguments: TestReadRunsAsAgentUID, TestAgentWriteRunsAsAgentUID).
			dir := "/work/" + name + "/sub"
			if err := inst.WriteFile(context.Background(), dir+"/x.md", []byte("x"), 0o644, core.OwnerSystem); err != nil {
				t.Fatalf("write: %v", err)
			}
			want := []string{p, dir}
			if len(*seen) != len(want) {
				t.Fatalf("archive requests: %q, want %q", *seen, want)
			}
			for i := range want {
				if (*seen)[i] != want[i] {
					t.Fatalf("request %d carried path %q, want %q", i, (*seen)[i], want[i])
				}
			}
		})
	}
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

var _ core.Stater = (*instance)(nil)

// inspectEngine plays GET /containers/c1/json with one answer, or a transport failure.
func inspectEngine(code int, body string, fail error) *instance {
	b := &Backend{http: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if fail != nil {
			return nil, fail
		}
		return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}, Request: r}, nil
	})}}
	return &instance{b: b, cid: "c1"}
}

// Gate 2: the record says whether the container runs. A read that failed is unknown,
// never stopped - a caller must not judge a sandbox on a failed read.
func TestStateReadsTheContainer(t *testing.T) {
	cases := []struct {
		name string
		inst *instance
		want core.State
	}{
		{"running", inspectEngine(200, `{"State":{"Running":true,"Status":"running"}}`, nil), core.StateRunning},
		{"stopped", inspectEngine(200, `{"State":{"Running":false,"Status":"exited"}}`, nil), core.StateStopped},
		{"missing", inspectEngine(404, `{"message":"No such container: c1"}`, nil), core.StateMissing},
		{"engine failing", inspectEngine(500, `{"message":"boom"}`, nil), core.StateUnknown},
		{"engine unreachable", inspectEngine(0, "", errors.New("dial unix: no such file")), core.StateUnknown},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.inst.State(context.Background()); got != c.want {
				t.Fatalf("want %q, got %q", c.want, got)
			}
		})
	}
}

// startEngine plays POST /containers/c1/start with one status, and keeps what it was asked.
func startEngine(code int) (*instance, *[]string) {
	var asked []string
	b := &Backend{http: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		asked = append(asked, r.Method+" "+r.URL.Path)
		return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(`{"message":"m"}`)), Header: http.Header{}, Request: r}, nil
	})}}
	return &instance{b: b, cid: "c1"}, &asked
}

// Gate 2: a stopped container starts again. One that already runs is left as it is
// (the Engine's 304); one the Engine no longer knows is gone; any other answer is the backend's.
func TestStartRunsTheContainerAgain(t *testing.T) {
	cases := []struct {
		name string
		code int
		want error
	}{
		{"stopped", http.StatusNoContent, nil},
		{"already running", http.StatusNotModified, nil},
		{"gone", http.StatusNotFound, core.ErrNotFound},
		{"engine failing", http.StatusInternalServerError, core.ErrBackend},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			inst, asked := startEngine(c.code)
			err := inst.Start(context.Background())
			if (c.want == nil) != (err == nil) || (c.want != nil && !errors.Is(err, c.want)) {
				t.Fatalf("want %v, got %v", c.want, err)
			}
			if len(*asked) != 1 || (*asked)[0] != "POST /v1.44/containers/c1/start" {
				t.Fatalf("one start of the container, got %v", *asked)
			}
		})
	}
}

// The premise gate 2 rests on, against a real Engine: `docker stop` keeps the container's writable
// layer, so /work is still there after a start, and the sandbox runs commands again.
func TestAStoppedSandboxKeepsWorkAndStartsAgain(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	inst, err := b.Create(ctx, "sbtest-stopstart", core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())
	layOut(t, ctx, inst, workshop)
	if err := inst.WriteFile(ctx, "/work/notes.txt", []byte("kept"), 0o644, core.OwnerAgent); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := b.do(ctx, http.MethodPost, "/containers/"+inst.(*instance).cid+"/stop?t=0", "", nil, nil); err != nil {
		t.Fatalf("docker stop: %v", err)
	}
	st := inst.(core.Stater)
	if got := st.State(ctx); got != core.StateStopped {
		t.Fatalf("after docker stop: want stopped, got %q", got)
	}
	for range 2 { // the second start finds it running: a no-op
		if err := st.Start(ctx); err != nil {
			t.Fatalf("start: %v", err)
		}
	}
	if got := st.State(ctx); got != core.StateRunning {
		t.Fatalf("after start: want running, got %q", got)
	}
	if got, err := inst.ReadFile(ctx, "/work/notes.txt"); err != nil || string(got) != "kept" {
		t.Fatalf("/work survives a stop: %q %v", got, err)
	}
	if res, err := inst.Exec(ctx, core.ExecRequest{Command: "cat /work/notes.txt"}); err != nil || res.Stdout != "kept" {
		t.Fatalf("a started sandbox runs commands: %+v %v", res, err)
	}
}

// ── Root takes back what the system gave ─────────────────────────────

// removeEngine plays the Engine for one exec: it keeps the create body and answers the exec's
// exit code and stderr.
func removeEngine(exit int, stderr string) (*instance, *[][]byte) {
	var creates [][]byte
	b := New("/nonexistent.sock")
	b.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		code, body := http.StatusOK, "{}"
		switch {
		case strings.HasSuffix(r.URL.Path, "/exec"):
			raw, _ := io.ReadAll(r.Body)
			creates = append(creates, raw)
			body = `{"Id":"e1"}`
		case strings.HasSuffix(r.URL.Path, "/start"):
			var frame bytes.Buffer
			if stderr != "" {
				frame.Write([]byte{2, 0, 0, 0})
				_ = binary.Write(&frame, binary.BigEndian, uint32(len(stderr)))
				frame.WriteString(stderr)
			}
			body = frame.String()
		case strings.HasSuffix(r.URL.Path, "/exec/e1/json"):
			body = fmt.Sprintf(`{"ExitCode":%d}`, exit)
		}
		return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}, Request: r}, nil
	})}
	return &instance{b: b, cid: "c1"}, &creates
}

// The removal is no exec a caller names: one fixed script, run as root with root's own HOME, the
// paths its arguments - never part of a shell string. A path outside /task never reaches the
// Engine, and a script that fails is the backend's error, with its reason.
func TestRemoveRunsOneFixedScriptAsRootWithThePathsAsArguments(t *testing.T) {
	inst, creates := removeEngine(0, "")
	if err := inst.Remove(context.Background(), []string{"/task/inputs/a b.md", "/task/context.md"}); err != nil {
		t.Fatalf("remove: %v", err)
	}
	var ex struct {
		User string   `json:"User"`
		Env  []string `json:"Env"`
		Cmd  []string `json:"Cmd"`
	}
	if len(*creates) != 1 || json.Unmarshal((*creates)[0], &ex) != nil {
		t.Fatalf("one exec: %s", *creates)
	}
	want := []string{"/bin/sh", "-c", removeScript, "sh", "/task/inputs/a b.md", "/task/context.md"}
	if ex.User != "0" || strings.Join(ex.Env, " ") != "HOME=/root "+systemPATH || strings.Join(ex.Cmd, "\x00") != strings.Join(want, "\x00") {
		t.Fatalf("the exec: %+v", ex)
	}

	inst, creates = removeEngine(0, "")
	if err := inst.Remove(context.Background(), []string{"/task/x", "/etc/passwd"}); !errors.Is(err, core.ErrBadSpec) || len(*creates) != 0 {
		t.Fatalf("a path outside /task never reaches the Engine: %v %d", err, len(*creates))
	}
	inst, _ = removeEngine(1, "rm: can't remove '/task/x': Permission denied")
	if err := inst.Remove(context.Background(), []string{"/task/x"}); !errors.Is(err, core.ErrBackend) || !strings.Contains(err.Error(), "Permission denied") {
		t.Fatalf("a failed removal is the backend's, with its reason: %v", err)
	}
}

// On a real Engine: a warm sandbox's /task holding a prior turn's files and directories - root's,
// read-only, under /task and /task/inputs locked 0555 as the frame image makes them - is emptied of
// exactly what was named: files and whole directories go, the step marker stays, and /task and
// /task/inputs keep 0555 and root, so the workload still cannot write there. Root holds no
// capability here; it removes as the owner.
func TestRemoveTakesBackWhatTheSystemGaveAndKeepsTheLock(t *testing.T) {
	b := dockerOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	inst, err := b.Create(ctx, "sbtest-remove-"+randToken()[:6], core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())
	for p, data := range map[string]string{
		"/task/.step-start": "", "/task/context.md": "a private memory", "/task/inputs/01-a-final.md": "turn one",
		"/task/inputs/figs/fig-1.png": "png", "/task/inputs/figs/deep/x.md": "deep",
	} {
		if err := inst.WriteFile(ctx, p, []byte(data), 0o444, core.OwnerSystem); err != nil {
			t.Fatalf("write %s: %v", p, err)
		}
	}
	i := inst.(*instance)
	if _, err := i.execAsRoot(ctx, "chmod", "0555", "/task/inputs/figs/deep", "/task/inputs/figs", "/task/inputs", "/task"); err != nil {
		t.Fatalf("lock: %v", err)
	}
	run := func(cmd string) core.ExecResult {
		res, err := inst.Exec(ctx, core.ExecRequest{Command: cmd, TimeoutS: 20})
		if err != nil {
			t.Fatalf("exec %q: %v", cmd, err)
		}
		return res
	}
	if err := inst.(core.Remover).Remove(ctx, []string{"/task/inputs/01-a-final.md", "/task/inputs/figs", "/task/context.md", "/task/answers.json"}); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if res := run("ls -A /task/inputs; ls -A /task"); strings.Join(strings.Fields(res.Stdout), " ") != ".step-start inputs" {
		t.Fatalf("want /task/inputs empty and only the step marker beside it, got %q", res.Stdout)
	}
	if res := run("stat -c '%a %u' /task /task/inputs"); strings.Join(strings.Fields(res.Stdout), " ") != "555 0 555 0" {
		t.Fatalf("/task and /task/inputs keep 0555 and root: %q", res.Stdout)
	}
	if res := run("touch /task/inputs/planted 2>&1; echo exit=$?"); !strings.Contains(res.Stdout, "exit=1") {
		t.Fatalf("the workload still cannot write a source: %q", res.Stdout)
	}
	if err := inst.(core.Remover).Remove(ctx, []string{"/task/inputs/gone.md"}); err != nil {
		t.Fatalf("a path that is not there is already removed: %v", err)
	}
	if err := inst.(core.Remover).Remove(ctx, []string{"/etc/hostname"}); !errors.Is(err, core.ErrBadSpec) {
		t.Fatalf("nothing outside /task can be named: %v", err)
	}
	if res := run("test -e /etc/hostname && echo kept"); !strings.Contains(res.Stdout, "kept") {
		t.Fatalf("/etc/hostname must be untouched: %q", res.Stdout)
	}
}

// ── A network per sandbox, against a played Engine (no socket needed) ──────────

// engineSpy plays the Engine for the egress path. Every call is recorded as "METHOD /path" (the
// API version prefix cut), the bodies of network and container creates are kept, and each route
// answers from its hook - a nil hook answers as a healthy Engine would.
type engineSpy struct {
	calls      []string
	netCreates []netCreateBody
	connects   []connectBody
	createBody []byte

	netCreate  func(n int) (int, string)       // the n-th POST /networks/create, from 0
	network    func(name string) (int, string) // GET /networks/<name>
	connect    func() (int, string)            // POST /networks/<name>/connect
	relay      func() (int, string)            // GET /containers/<relay>/json
	containers string                          // GET /containers/json (Adopt)
	networks   string                          // GET /networks (Adopt)
	env        map[string][]string             // GET /containers/<id>/json: a sandbox's Config.Env
}

type netCreateBody struct {
	Name     string            `json:"Name"`
	Internal bool              `json:"Internal"`
	IPv6     bool              `json:"EnableIPv6"`
	Options  map[string]string `json:"Options"`
	Labels   map[string]string `json:"Labels"`
	IPAM     struct {
		Config []struct {
			Subnet string `json:"Subnet"`
		} `json:"Config"`
	} `json:"IPAM"`
}

type connectBody struct {
	Container      string `json:"Container"`
	EndpointConfig struct {
		Aliases []string `json:"Aliases"`
	} `json:"EndpointConfig"`
}

func (e *engineSpy) backend(opts ...Option) *Backend {
	b := New("/nonexistent.sock", opts...)
	b.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		p := strings.TrimPrefix(r.URL.Path, "/v1.44")
		e.calls = append(e.calls, r.Method+" "+p)
		code, body := http.StatusOK, "{}"
		answer := func(hook func() (int, string)) {
			if hook != nil {
				code, body = hook()
			}
		}
		switch {
		case r.Method == http.MethodPost && p == "/networks/create":
			var nc netCreateBody
			_ = json.NewDecoder(r.Body).Decode(&nc)
			n := len(e.netCreates)
			e.netCreates = append(e.netCreates, nc)
			code, body = http.StatusCreated, `{"Id":"n1"}`
			if e.netCreate != nil {
				code, body = e.netCreate(n)
			}
		case r.Method == http.MethodPost && strings.HasSuffix(p, "/connect"):
			var cb connectBody
			_ = json.NewDecoder(r.Body).Decode(&cb)
			e.connects = append(e.connects, cb)
			answer(e.connect)
		case r.Method == http.MethodGet && p == "/networks":
			body = e.networks
			if body == "" {
				body = "[]"
			}
		case r.Method == http.MethodGet && strings.HasPrefix(p, "/networks/"):
			code, body = internalNet(strings.TrimPrefix(p, "/networks/"))
			if e.network != nil {
				code, body = e.network(strings.TrimPrefix(p, "/networks/"))
			}
		case r.Method == http.MethodDelete && strings.HasPrefix(p, "/networks/"):
			code, body = http.StatusNoContent, ""
		case r.Method == http.MethodGet && p == "/containers/json":
			body = e.containers
			if body == "" {
				body = "[]"
			}
		case r.Method == http.MethodGet && p == "/containers/sandbar-egress/json":
			answer(e.relay)
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/json") && strings.HasPrefix(p, "/containers/"):
			cid := strings.TrimSuffix(strings.TrimPrefix(p, "/containers/"), "/json")
			env, _ := json.Marshal(e.env[cid])
			body = `{"Config":{"Env":` + string(env) + `}}`
		case strings.HasSuffix(p, "/containers/create"):
			e.createBody, _ = io.ReadAll(r.Body)
			body = `{"Id":"c1"}`
		case strings.HasSuffix(p, "/start"), r.Method == http.MethodDelete:
			code, body = http.StatusNoContent, ""
		}
		return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}, Request: r}, nil
	})}
	return b
}

// internalNet is what a real Engine answers for the network sandbard makes: internal, made with
// inhibit_ipv4, its subnet, and no gateway (the host holds no address on it).
func internalNet(name string) (int, string) {
	return http.StatusOK, `{"Name":"` + name + `","Internal":true,` +
		`"Options":{"com.docker.network.bridge.inhibit_ipv4":"true"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28"}]}}`
}

// calledBefore reports whether a call matching first was recorded before one matching then.
func (e *engineSpy) calledBefore(first, then string) bool {
	i, j := -1, -1
	for n, c := range e.calls {
		if i < 0 && strings.HasPrefix(c, first) {
			i = n
		}
		if j < 0 && strings.HasPrefix(c, then) {
			j = n
		}
	}
	return i >= 0 && j >= 0 && i < j
}

func (e *engineSpy) called(prefix string) bool {
	for _, c := range e.calls {
		if strings.HasPrefix(c, prefix) {
			return true
		}
	}
	return false
}

type createdBody struct {
	Env        []string          `json:"Env"`
	Labels     map[string]string `json:"Labels"`
	HostConfig map[string]any    `json:"HostConfig"`
}

func (e *engineSpy) created(t *testing.T) createdBody {
	t.Helper()
	var c createdBody
	if err := json.Unmarshal(e.createBody, &c); err != nil {
		t.Fatalf("create body: %v %s", err, e.createBody)
	}
	return c
}

func withDomains() core.Spec {
	return core.Spec{Image: "x", AllowDomains: []string{"example.com"}, Labels: map[string]string{"plane.org": "o1"}}.WithDefaults()
}

// A sandbox with domains gets an INTERNAL network of its own - a /28 of the
// operator's pool, made with inhibit_ipv4 (no host address) and checked before use - and the relay
// joins it under the alias the proxy URL names. No other sandbox is ever on it, so no sandbox
// reaches another, of any org. The container carries its id and spec as labels, so a restarted
// daemon finds it again.
func TestASandboxWithDomainsGetsANetworkOfItsOwn(t *testing.T) {
	spy := &engineSpy{}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	b.ipv6 = true // the kernel has IPv6 (the other branch: ...OnAKernelWithoutIPv6...)
	inst, err := b.Create(context.Background(), "sb1", withDomains())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	nc := spy.netCreates[0]
	if nc.Name != "sandbar-net-sb1" || !nc.Internal || nc.IPv6 || nc.Options[inhibitIPv4] != "true" ||
		len(nc.IPAM.Config) != 1 || nc.IPAM.Config[0].Subnet != "10.213.0.0/28" || nc.Labels[labelID] != "sb1" {
		t.Fatalf("the sandbox's own network: %+v", nc)
	}
	if len(spy.connects) != 1 || spy.connects[0].Container != DefaultRelay ||
		len(spy.connects[0].EndpointConfig.Aliases) != 1 || spy.connects[0].EndpointConfig.Aliases[0] != "sandbar-egress" {
		t.Fatalf("the relay joins the sandbox's network under the proxy URL's name: %+v", spy.connects)
	}
	if !spy.calledBefore("POST /networks/create", "GET /networks/sandbar-net-sb1") ||
		!spy.calledBefore("GET /networks/sandbar-net-sb1", "POST /networks/sandbar-net-sb1/connect") ||
		!spy.calledBefore("POST /networks/sandbar-net-sb1/connect", "POST /containers/create") {
		t.Fatalf("make, check, join the relay, then the container: %v", spy.calls)
	}
	c := spy.created(t)
	if c.HostConfig["NetworkMode"] != "sandbar-net-sb1" {
		t.Fatalf("want NetworkMode sandbar-net-sb1, got %v", c.HostConfig["NetworkMode"])
	}
	if _, ok := c.HostConfig["ExtraHosts"]; ok {
		t.Fatalf("a sandbox on its internal network gets no host-gateway entry: %v", c.HostConfig["ExtraHosts"])
	}
	// IPv6 off in the sandbox: inhibit_ipv4 keeps the host's IPv4 address off the
	// network, but a bridge's fe80:: link-local would still reach host services bound to ::.
	sysctls, _ := c.HostConfig["Sysctls"].(map[string]any)
	if sysctls["net.ipv6.conf.all.disable_ipv6"] != "1" || sysctls["net.ipv6.conf.default.disable_ipv6"] != "1" || len(sysctls) != 2 {
		t.Fatalf("a sandbox with domains must have IPv6 disabled, got Sysctls %v", c.HostConfig["Sysctls"])
	}
	proxies := 0
	for _, kv := range c.Env {
		k, v, _ := strings.Cut(kv, "=")
		if k == "HTTP_PROXY" || k == "HTTPS_PROXY" || k == "http_proxy" || k == "https_proxy" {
			proxies++
			if !strings.HasPrefix(v, "http://sb1:") || !strings.HasSuffix(v, "@sandbar-egress:7080") {
				t.Fatalf("%s must carry the sandbox's credential to the relay, got %q", k, v)
			}
		}
	}
	if proxies != 4 || inst.(*instance).token == "" {
		t.Fatalf("want 4 proxy vars and a principal, got %d %q", proxies, inst.(*instance).token)
	}
	var spec core.Spec
	if c.Labels[labelID] != "sb1" || json.Unmarshal([]byte(c.Labels[labelSpec]), &spec) != nil ||
		spec.AllowDomains[0] != "example.com" || spec.Labels["plane.org"] != "o1" || spec.Env != nil {
		t.Fatalf("the container carries its id and spec (never its env) for a restart: %v", c.Labels)
	}

	if _, err := b.Create(context.Background(), "sb2", withDomains()); err != nil {
		t.Fatalf("create sb2: %v", err)
	}
	if got := spy.netCreates[1]; got.Name != "sandbar-net-sb2" || got.IPAM.Config[0].Subnet != "10.213.0.16/28" {
		t.Fatalf("the next sandbox takes the next /28: %+v", got)
	}
}

// A block another network holds (the Engine's "Pool overlaps") is skipped, never fatal.
func TestABlockAnotherNetworkHoldsIsSkipped(t *testing.T) {
	spy := &engineSpy{netCreate: func(n int) (int, string) {
		if n == 0 {
			return http.StatusForbidden, `{"message":"invalid pool request: Pool overlaps with other one on this address space"}`
		}
		return http.StatusCreated, `{"Id":"n2"}`
	}}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	if _, err := b.Create(context.Background(), "sb1", withDomains()); err != nil {
		t.Fatalf("create: %v", err)
	}
	if len(spy.netCreates) != 2 || spy.netCreates[1].IPAM.Config[0].Subnet != "10.213.0.16/28" {
		t.Fatalf("want the overlapping block skipped: %+v", spy.netCreates)
	}
}

// A network of the sandbox's name that outlived its sandbox (a sandbard that died mid-create) is
// removed and the create goes on: no live sandbox holds it, since the registry refuses a taken id.
func TestANetworkLeftByADeadCreateIsRemoved(t *testing.T) {
	spy := &engineSpy{netCreate: func(n int) (int, string) {
		if n == 0 {
			return http.StatusConflict, `{"message":"network with name sandbar-net-sb1 already exists"}`
		}
		return http.StatusCreated, `{"Id":"n2"}`
	}}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	if _, err := b.Create(context.Background(), "sb1", withDomains()); err != nil {
		t.Fatalf("create: %v", err)
	}
	if !spy.calledBefore("DELETE /networks/sandbar-net-sb1", "GET /networks/sandbar-net-sb1") ||
		len(spy.netCreates) != 2 || spy.netCreates[1].IPAM.Config[0].Subnet != "10.213.0.0/28" {
		t.Fatalf("the stale network is removed and the block taken again: %v %+v", spy.calls, spy.netCreates)
	}
}

// A pool with no free block refuses the sandbox and says which setting widens it.
func TestASpentPoolRefusesTheSandbox(t *testing.T) {
	spy := &engineSpy{}
	_, tiny, _ := net.ParseCIDR("10.213.0.0/28") // one block
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"), WithEgressPool(tiny))
	if _, err := b.Create(context.Background(), "sb1", withDomains()); err != nil {
		t.Fatalf("create: %v", err)
	}
	_, err := b.Create(context.Background(), "sb2", withDomains())
	if !errors.Is(err, core.ErrBackend) || !strings.Contains(err.Error(), "SANDBAR_DOCKER_EGRESS_POOL") {
		t.Fatalf("want a refusal naming the pool, got %v", err)
	}
}

// Destroy removes the sandbox's network after its container (the relay leaves it first) and gives
// its block back.
func TestDestroyRemovesTheSandboxesNetworkAndFreesItsBlock(t *testing.T) {
	spy := &engineSpy{}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	inst, err := b.Create(context.Background(), "sb1", withDomains())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := inst.Destroy(context.Background()); err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if !spy.calledBefore("DELETE /containers/c1", "POST /networks/sandbar-net-sb1/disconnect") ||
		!spy.calledBefore("POST /networks/sandbar-net-sb1/disconnect", "DELETE /networks/sandbar-net-sb1") {
		t.Fatalf("the container, then the relay, then the network: %v", spy.calls)
	}
	if _, err := b.Create(context.Background(), "sb2", withDomains()); err != nil {
		t.Fatalf("create sb2: %v", err)
	}
	if got := spy.netCreates[1].IPAM.Config[0].Subnet; got != "10.213.0.0/28" {
		t.Fatalf("the freed block is taken again, got %s", got)
	}
}

// A kernel booted with ipv6.disable=1 has no IPv6 sysctls, and runc refuses one it cannot find:
// the belt would fail every sandbox with domains. There is no IPv6 to disable, so none is set.
func TestASandboxWithDomainsOnAKernelWithoutIPv6GetsNoSysctls(t *testing.T) {
	spy := &engineSpy{}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	b.ipv6 = false
	if _, err := b.Create(context.Background(), "sb4", withDomains()); err != nil {
		t.Fatalf("create: %v", err)
	}
	c := spy.created(t)
	if _, ok := c.HostConfig["Sysctls"]; ok {
		t.Fatalf("a kernel without IPv6 gets no IPv6 sysctls, got %v", c.HostConfig["Sysctls"])
	}
	if c.HostConfig["NetworkMode"] != "sandbar-net-sb4" {
		t.Fatalf("the rest of the posture is unchanged, got NetworkMode %v", c.HostConfig["NetworkMode"])
	}
}

// The probe reads sandbard's own view of the kernel it shares with the Engine. A Linux view with
// no ipv6 tree means a kernel without IPv6; no Linux view at all (macOS dev against a VM's Engine)
// keeps the belt, since a wrong guess there fails the create loudly instead of leaving IPv6 on.
func TestTheKernelProbeReadsProcSysNet(t *testing.T) {
	withV6 := t.TempDir()
	if err := os.MkdirAll(withV6+"/ipv6/conf/all", 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(withV6+"/ipv6/conf/all/disable_ipv6", []byte("0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	withoutV6 := t.TempDir() // a /proc/sys/net with ipv4 only
	if err := os.MkdirAll(withoutV6+"/ipv4", 0o755); err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string]struct {
		sysNet string
		want   bool
	}{
		"a kernel with IPv6":      {withV6, true},
		"a kernel without IPv6":   {withoutV6, false},
		"no Linux kernel in view": {t.TempDir() + "/absent", true},
	} {
		if got := kernelHasIPv6(c.sysNet); got != c.want {
			t.Errorf("%s: kernelHasIPv6(%s) = %v, want %v", name, c.sysNet, got, c.want)
		}
	}
}

func TestASandboxWithNoDomainsHasNoNetworkAndNoProxy(t *testing.T) {
	spy := &engineSpy{}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	if _, err := b.Create(context.Background(), "sb2", core.Spec{Image: "x"}.WithDefaults()); err != nil {
		t.Fatalf("create: %v", err)
	}
	if spy.called("POST /networks") || spy.called("GET /networks") {
		t.Fatalf("an airtight sandbox makes no network: %v", spy.calls)
	}
	c := spy.created(t)
	if c.HostConfig["NetworkMode"] != "none" {
		t.Fatalf("an airtight sandbox has network none, got %v", c.HostConfig["NetworkMode"])
	}
	for _, kv := range c.Env {
		if strings.HasPrefix(strings.ToLower(kv), "http_proxy=") || strings.HasPrefix(strings.ToLower(kv), "https_proxy=") {
			t.Fatalf("an airtight sandbox gets no proxy: %q", kv)
		}
	}
	if c.Labels[labelID] != "sb2" {
		t.Fatalf("an airtight sandbox is labelled for a restart too: %v", c.Labels)
	}
}

// Fail closed on the network sandbard just made: one the Engine left without --internal, or with
// any IPAM gateway (IPv4 or IPv6), refuses the sandbox and is removed, and its block
// goes back - sandbard never runs a sandbox on a network it cannot vouch for. An engine that took
// inhibit_ipv4 and reserved an IPv4 gateway anyway is below the floor, so that
// refusal says to upgrade it.
func TestACreateWithDomainsRefusesANetworkTheEngineLeftOpen(t *testing.T) {
	cases := map[string]struct {
		network func(string) (int, string)
		says    string
		not     string // and must not say this
	}{
		"gone": {func(string) (int, string) { return http.StatusNotFound, `{"message":"network not found"}` }, "is gone", ""},
		"not internal": {func(n string) (int, string) {
			return http.StatusOK, `{"Name":"` + n + `","Internal":false}`
		}, "without --internal", ""},
		"a gateway without inhibit_ipv4": {func(n string) (int, string) {
			return http.StatusOK, `{"Name":"` + n + `","Internal":true,"IPAM":{"Config":[{"Subnet":"10.213.0.0/28","Gateway":"10.213.0.1"}]}}`
		}, "gives the host an address (gateway 10.213.0.1)", "upgrade the engine"},
		"an IPv6 gateway": {func(n string) (int, string) {
			return http.StatusOK, `{"Name":"` + n + `","Internal":true,"Options":{"com.docker.network.bridge.inhibit_ipv4":"true"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28"},{"Subnet":"fd00:5b::/64","Gateway":"fd00:5b::1"}]}}`
		}, "gives the host an address (gateway fd00:5b::1)", "upgrade the engine"},
		"an engine below the floor": {func(n string) (int, string) {
			return http.StatusOK, `{"Name":"` + n + `","Internal":true,"Options":{"com.docker.network.bridge.inhibit_ipv4":"true"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28","Gateway":"10.213.0.1"}]}}`
		}, "this Docker Engine reserves a gateway despite inhibit_ipv4 - upgrade the engine (verified on 29.4)", ""},
		"engine down": {func(string) (int, string) { return http.StatusInternalServerError, `{"message":"boom"}` }, "inspect the network", ""},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			spy := &engineSpy{network: c.network}
			b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
			_, err := b.Create(context.Background(), "sb3", withDomains())
			if !errors.Is(err, core.ErrBackend) || !strings.Contains(err.Error(), c.says) {
				t.Fatalf("want ErrBackend saying %q, got %v", c.says, err)
			}
			if c.not != "" && strings.Contains(err.Error(), c.not) {
				t.Fatalf("the refusal must not say %q: %v", c.not, err)
			}
			if spy.createBody != nil || len(spy.connects) != 0 {
				t.Fatal("no relay and no container on a refused network")
			}
			if !spy.called("DELETE /networks/sandbar-net-sb3") {
				t.Fatalf("the refused network is removed: %v", spy.calls)
			}
			spy.network = nil
			if _, err := b.Create(context.Background(), "sb4", withDomains()); err != nil {
				t.Fatalf("create after a refusal: %v", err)
			}
			if got := spy.netCreates[len(spy.netCreates)-1].IPAM.Config[0].Subnet; got != "10.213.0.0/28" {
				t.Fatalf("a refused network's block goes back, got %s", got)
			}
		})
	}
}

// A relay that cannot join the sandbox's network leaves the sandbox no way out: refused, and the
// network removed.
func TestARelayThatCannotJoinRefusesTheSandbox(t *testing.T) {
	spy := &engineSpy{connect: func() (int, string) { return http.StatusNotFound, `{"message":"No such container: sandbar-egress"}` }}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	_, err := b.Create(context.Background(), "sb5", withDomains())
	if !errors.Is(err, core.ErrBackend) || !strings.Contains(err.Error(), `the relay "sandbar-egress" could not join`) {
		t.Fatalf("want a refusal naming the relay, got %v", err)
	}
	if spy.createBody != nil || !spy.called("DELETE /networks/sandbar-net-sb5") {
		t.Fatalf("no container, and the network removed: %v", spy.calls)
	}
}

// The boot check: sandbard with egress on refuses to start without its relay, or on an Engine that
// gives an internal inhibit_ipv4 network a gateway (proved on a throwaway network from the pool,
// which it removes).
func TestTheBootCheckNeedsTheRelayAndTheFloor(t *testing.T) {
	spy := &engineSpy{relay: func() (int, string) { return http.StatusNotFound, `{"message":"No such container: sandbar-egress"}` }}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	if err := b.CheckEgress(context.Background()); !errors.Is(err, core.ErrBackend) ||
		!strings.Contains(err.Error(), `the relay container "sandbar-egress" does not exist`) {
		t.Fatalf("want a refusal naming the missing relay, got %v", err)
	}

	spy = &engineSpy{network: func(n string) (int, string) {
		return http.StatusOK, `{"Name":"` + n + `","Internal":true,"Options":{"com.docker.network.bridge.inhibit_ipv4":"true"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28","Gateway":"10.213.0.1"}]}}`
	}}
	b = spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	if err := b.CheckEgress(context.Background()); !errors.Is(err, core.ErrBackend) || !strings.Contains(err.Error(), "upgrade the engine") {
		t.Fatalf("want the floor refusal, got %v", err)
	}

	spy = &engineSpy{}
	b = spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	if err := b.CheckEgress(context.Background()); err != nil {
		t.Fatalf("a healthy engine and relay pass: %v", err)
	}
	if len(spy.netCreates) != 1 || !strings.HasPrefix(spy.netCreates[0].Name, "sandbar-net-probe-") ||
		len(spy.connects) != 0 || !spy.called("DELETE /networks/"+spy.netCreates[0].Name) {
		t.Fatalf("the floor is proved on a throwaway network, removed after: %v", spy.calls)
	}
}

// recordingEgress remembers the last allowlist registered per principal.
type recordingEgress struct {
	spyEgress
	allowed map[string][]string
}

func (r *recordingEgress) Allow(p string, d []string) { r.allowed[p] = d }

// A restarted sandbard finds the sandboxes it made - by their labels - and keeps them: the
// record (id, spec), the network (checked again, the relay joined again - it may have been
// recreated), the proxy credential (read back from the container's env) registered with an EMPTY
// allowlist until the caller's next versioned push, and the network's block held. A network left
// with no sandbox (a create that died between the two) is removed.
func TestAdoptKeepsWhatTheEngineStillHolds(t *testing.T) {
	spec, _ := json.Marshal(withDomains())
	airtight, _ := json.Marshal(core.Spec{Image: "x"}.WithDefaults())
	spy := &engineSpy{
		containers: `[{"Id":"c1","Created":1760000000,"Labels":{"sandbar.id":"m1","sandbar.spec":` + strconv.Quote(string(spec)) + `}},` +
			`{"Id":"c2","Created":1760000000,"Labels":{"sandbar.id":"m2","sandbar.spec":` + strconv.Quote(string(airtight)) + `}}]`,
		networks: `[{"Name":"sandbar-net-m1","Labels":{"sandbar.id":"m1"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28"}]}},` +
			`{"Name":"sandbar-net-x9","Labels":{"sandbar.id":"x9"},"IPAM":{"Config":[{"Subnet":"10.213.0.16/28"}]}}]`,
		env: map[string][]string{"c1": {"HTTPS_PROXY=http://m1:tok1@sandbar-egress:7080", "PATH=/bin"}},
		connect: func() (int, string) {
			return http.StatusForbidden, `{"message":"endpoint with name sandbar-egress already exists in network sandbar-net-m1"}`
		},
	}
	eg := &recordingEgress{allowed: map[string][]string{}}
	b := spy.backend(WithEgress(eg, "http://sandbar-egress:7080"))
	found, err := b.Adopt(context.Background())
	if err != nil || len(found) != 2 {
		t.Fatalf("adopt: %+v %v", found, err)
	}
	byID := map[string]core.Adopted{}
	for _, f := range found {
		byID[f.ID] = f
	}
	m1, m2 := byID["m1"], byID["m2"]
	if m1.Spec.AllowDomains[0] != "example.com" || m1.Spec.Labels["plane.org"] != "o1" || m1.CreatedAt.Unix() != 1760000000 {
		t.Fatalf("m1's record: %+v", m1)
	}
	if i := m1.Instance.(*instance); i.cid != "c1" || i.token != "tok1" || i.net != "sandbar-net-m1" || i.subnet != "10.213.0.0/28" {
		t.Fatalf("m1's instance: %+v", i)
	}
	if d, ok := eg.allowed[egress.TokenKey("tok1")]; !ok || len(d) != 0 {
		t.Fatalf("an adopted credential comes back with an empty allowlist: %v", eg.allowed)
	}
	if len(spy.connects) != 1 || spy.connects[0].Container != DefaultRelay {
		t.Fatalf("the relay joins the adopted network again (already there is fine): %+v", spy.connects)
	}
	if i := m2.Instance.(*instance); i.cid != "c2" || i.token != "" || i.net != "" {
		t.Fatalf("m2 is airtight: %+v", i)
	}
	if !spy.called("DELETE /networks/sandbar-net-x9") || spy.called("DELETE /networks/sandbar-net-m1") {
		t.Fatalf("only the orphan network is removed: %v", spy.calls)
	}
	if _, err := b.Create(context.Background(), "sb1", withDomains()); err != nil {
		t.Fatalf("create: %v", err)
	}
	if got := spy.netCreates[0].IPAM.Config[0].Subnet; got != "10.213.0.16/28" {
		t.Fatalf("an adopted network's block stays held (the orphan's is free again), got %s", got)
	}
}

// A daemon restarted with egress off still adopts a sandbox born with domains, without joining a
// relay it may not run: the sandbox reaches nothing, and a push to it is refused (no egress).
func TestAdoptWithEgressOffJoinsNoRelay(t *testing.T) {
	spec, _ := json.Marshal(withDomains())
	spy := &engineSpy{
		containers: `[{"Id":"c1","Created":1760000000,"Labels":{"sandbar.id":"m1","sandbar.spec":` + strconv.Quote(string(spec)) + `}}]`,
		networks:   `[{"Name":"sandbar-net-m1","Labels":{"sandbar.id":"m1"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28"}]}}]`,
		connect:    func() (int, string) { return http.StatusNotFound, `{"message":"No such container: sandbar-egress"}` },
	}
	found, err := spy.backend().Adopt(context.Background())
	if err != nil || len(found) != 1 || len(spy.connects) != 0 {
		t.Fatalf("adopted without a relay: %+v %v %+v", found, err, spy.connects)
	}
	if err := found[0].Instance.Egress(context.Background(), []string{"example.com"}); !errors.Is(err, core.ErrBadSpec) {
		t.Fatalf("a push to it is refused, got %v", err)
	}
}

// Fail closed at adoption too: a sandbox whose network no longer passes the check is removed, not
// kept on a network sandbard cannot vouch for.
func TestAdoptRemovesASandboxWhoseNetworkFailsTheCheck(t *testing.T) {
	spec, _ := json.Marshal(withDomains())
	spy := &engineSpy{
		containers: `[{"Id":"c1","Created":1760000000,"Labels":{"sandbar.id":"m1","sandbar.spec":` + strconv.Quote(string(spec)) + `}}]`,
		networks:   `[{"Name":"sandbar-net-m1","Labels":{"sandbar.id":"m1"},"IPAM":{"Config":[{"Subnet":"10.213.0.0/28"}]}}]`,
		network:    func(n string) (int, string) { return http.StatusOK, `{"Name":"` + n + `","Internal":false}` },
	}
	b := spy.backend(WithEgress(&spyEgress{}, "http://sandbar-egress:7080"))
	found, err := b.Adopt(context.Background())
	if err != nil || len(found) != 0 {
		t.Fatalf("want nothing adopted, got %+v %v", found, err)
	}
	if !spy.called("DELETE /containers/c1") || !spy.called("DELETE /networks/sandbar-net-m1") {
		t.Fatalf("the sandbox and its network are removed: %v", spy.calls)
	}
}

// The pool is the operator's: an IPv4 range of at least one /28.
func TestTheEgressPoolIsAnIPv4RangeOfWholeBlocks(t *testing.T) {
	for raw, ok := range map[string]bool{
		"10.213.0.0/16": true, "192.168.200.0/28": true,
		"10.213.0.0/29": false, "fd00::/64": false, "not-a-cidr": false,
	} {
		_, err := ParsePool(raw)
		if (err == nil) != ok {
			t.Errorf("ParsePool(%q): err=%v, want ok=%v", raw, err, ok)
		}
	}
}

// ── On a real Engine: sandbard's own networks and a real relay ──────────────────

// netInspect is the part of GET /networks/<name> these tests read.
type netInspect struct {
	Internal bool `json:"Internal"`
	IPAM     struct {
		Config []struct {
			Subnet  string `json:"Subnet"`
			Gateway string `json:"Gateway"`
		} `json:"Config"`
	} `json:"IPAM"`
}

func inspectNet(t *testing.T, b *Backend, name string) netInspect {
	t.Helper()
	var nw netInspect
	if err := b.do(context.Background(), http.MethodGet, "/networks/"+name, "", nil, &nw); err != nil {
		t.Fatalf("inspect the network %s: %v", name, err)
	}
	return nw
}

// gatewayOf is a network's first IPAM gateway, "" when it has none.
func gatewayOf(nw netInspect) string {
	for _, c := range nw.IPAM.Config {
		if c.Gateway != "" {
			return c.Gateway
		}
	}
	return ""
}

// testPool is a /24 of 10.214.0.0/16 for this test's sandboxes, so a test never draws on the
// default pool a live sandbard on the same Engine uses.
func testPool(t *testing.T) *net.IPNet {
	t.Helper()
	n, _ := strconv.ParseUint(randToken()[:2], 16, 8)
	pool, err := ParsePool(fmt.Sprintf("10.214.%d.0/24", n))
	if err != nil {
		t.Fatal(err)
	}
	return pool
}

// startContainer runs a container on the default bridge and removes it at cleanup.
func startContainer(t *testing.T, sock, name string, cfg map[string]any) string {
	t.Helper()
	b := New(sock)
	body, _ := json.Marshal(cfg)
	if err := b.do(context.Background(), http.MethodPost, "/containers/create?name="+name, "application/json", bytes.NewReader(body), nil); err != nil {
		t.Fatalf("create %s: %v", name, err)
	}
	t.Cleanup(func() {
		if err := b.do(context.Background(), http.MethodDelete, "/containers/"+name+"?force=true", "", nil, nil); err != nil {
			t.Logf("remove %s: %v", name, err)
		}
	})
	if err := b.do(context.Background(), http.MethodPost, "/containers/"+name+"/start", "", nil, nil); err != nil {
		t.Fatalf("start %s: %v", name, err)
	}
	return name
}

// standInRelay is a container sandbard can join to a sandbox's network for a test that needs the
// posture but no egress: it relays nothing.
func standInRelay(t *testing.T, sock string) string {
	t.Helper()
	return startContainer(t, sock, "sbtest-standin-"+randToken()[:8], map[string]any{
		"Image": "alpine:3.20", "Cmd": []string{"sleep", "600"}, "HostConfig": map[string]any{"NetworkMode": "bridge"},
	})
}

// relayOn starts `sandbard relay` from SANDBAR_RELAY_IMAGE on the default bridge, piping to this
// host's port, and returns its name: sandbard joins it to each sandbox's network. It listens on
// 0.0.0.0, so a network joined later is served at once. Skipped without the image: `just
// sandbar-image` builds it.
func relayOn(t *testing.T, sock, hostPort string) string {
	t.Helper()
	image := os.Getenv("SANDBAR_RELAY_IMAGE")
	if image == "" {
		t.Skip("SANDBAR_RELAY_IMAGE not set; run `just sandbar-image` and export the tag")
	}
	return startContainer(t, sock, "sbtest-relay-"+randToken()[:8], map[string]any{
		"Image": image, "Cmd": []string{"relay"},
		"Env": []string{"SANDBAR_RELAY_TO=host.docker.internal:" + hostPort},
		"HostConfig": map[string]any{
			"NetworkMode": "bridge", "ExtraHosts": []string{"host.docker.internal:host-gateway"},
		},
	})
}

// egressBackend is a Docker backend with egress on: eg, the given relay, a test pool.
func egressBackend(t *testing.T, sock string, eg Egress, relay string) *Backend {
	t.Helper()
	return New(sock, WithEgress(eg, "http://sandbar-egress:7080"), WithRelay(relay), WithEgressPool(testPool(t)))
}

// liveProxy serves a real egress proxy on this host, where the relay - a container that reaches
// this host as host.docker.internal (host-gateway) - can dial it, and returns it with its port.
func liveProxy(t *testing.T, ctx context.Context, sock string) (*egress.Proxy, string) {
	t.Helper()
	eg := egress.New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	addr, closeProxy, err := eg.Serve(ctx, proxyListenAddr(t, sock))
	if err != nil {
		t.Fatalf("start proxy: %v", err)
	}
	t.Cleanup(func() { _ = closeProxy() })
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("proxy addr %q: %v", addr, err)
	}
	return eg, port
}

// proxyListenAddr is where a test's proxy listens for the relay. Docker Desktop and OrbStack forward
// host-gateway to this machine's loopback; a Linux Engine's host-gateway is its default bridge's
// gateway, an address of this host that loopback is not - so on Linux the proxy listens there.
func proxyListenAddr(t *testing.T, sock string) string {
	t.Helper()
	if runtime.GOOS != "linux" {
		return "127.0.0.1:0"
	}
	gw := gatewayOf(inspectNet(t, New(sock), "bridge"))
	if gw == "" {
		t.Skip("the default bridge reports no gateway: a container cannot reach this host's proxy")
	}
	return net.JoinHostPort(gw, "0")
}

// The boot check on a real Engine: with the relay present, the floor holds (a throwaway internal
// inhibit_ipv4 network gets no gateway) and the probe network is gone after; with no relay,
// sandbard refuses to boot.
func TestTheBootCheckOnThisEngine(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	b := egressBackend(t, sock, &spyEgress{}, standInRelay(t, sock))
	if err := b.CheckEgress(ctx); err != nil {
		t.Fatalf("the boot check on this engine: %v", err)
	}
	var left []struct{ Name string }
	if err := b.do(ctx, http.MethodGet, "/networks?filters="+url.QueryEscape(`{"name":["sandbar-net-probe-"]}`), "", nil, &left); err != nil || len(left) != 0 {
		t.Fatalf("the probe network must be removed: %v %v", left, err)
	}
	none := egressBackend(t, sock, &spyEgress{}, "sbtest-no-such-relay-"+randToken()[:6])
	if err := none.CheckEgress(ctx); !errors.Is(err, core.ErrBackend) || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("no relay must refuse the boot, got %v", err)
	}
}

// End to end: a sandbox with domains reaches its allowlist through the relay and
// nothing directly - not the internet (probes 2, 3), not sandbard by name (probe 4). The host's
// own services (probe 5, and probe 4's host-side API) are TestADomainSandboxCannotReachAHostService.
func TestADomainSandboxReachesOnlyTheRelay(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	eg, port := liveProxy(t, ctx, sock)
	b := egressBackend(t, sock, eg, relayOn(t, sock, port))
	inst, err := b.Create(ctx, "sbtest-s2-"+randToken()[:6], core.Spec{
		Image: "alpine:3.20", AllowDomains: []string{"example.com"},
	}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())
	run := func(cmd string) core.ExecResult {
		res, err := inst.Exec(ctx, core.ExecRequest{Command: cmd, TimeoutS: 30})
		if err != nil {
			t.Fatalf("exec %q: %v", cmd, err)
		}
		return res
	}
	if _, dnsErr := net.LookupHost("example.com"); dnsErr == nil {
		if res := run("wget -q -T 10 -O /dev/null http://example.com/ && echo ok"); !strings.Contains(res.Stdout, "ok") {
			t.Fatalf("probe 1: the allowlisted host must be reachable through the relay: %+v", res)
		}
	}
	for name, cmd := range map[string]string{
		"probe 2: the allowlisted host, bypassing the proxy": noProxy + "wget -q -T 5 -O /dev/null http://example.com/",
		"probe 3: a raw address":                             noProxy + "wget -q -T 5 -O /dev/null http://1.1.1.1/",
		// busybox nc (no -z on every build): connect, send nothing, exit 0 only if it connected
		"probe 4: sandbard's API, by name": noProxy + "nc -w 3 sandbard 7070 </dev/null",
	} {
		res := run(cmd + " && echo LEAK")
		t.Logf("%s -> exit=%d stdout=%q stderr=%q", name, res.ExitCode, res.Stdout, res.Stderr)
		if strings.Contains(res.Stdout, "LEAK") {
			t.Fatalf("%s: a sandbox with domains has a direct route out", name)
		}
	}
}

// On a real Engine: two sandboxes with domains - two orgs' Machines - are on networks of
// their own, so one cannot reach a server the other runs on all interfaces: not directly, and not
// through the proxy (the neighbour is a private address, and not on the allowlist). Each still
// reaches its allowlisted host through the relay.
func TestTwoDomainSandboxesCannotReachEachOther(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	eg, port := liveProxy(t, ctx, sock)
	b := egressBackend(t, sock, eg, relayOn(t, sock, port))
	mk := func(org string) core.Instance {
		inst, err := b.Create(ctx, "sbtest-pair-"+org+"-"+randToken()[:6], core.Spec{
			Image: "alpine:3.20", AllowDomains: []string{"example.com"}, Labels: map[string]string{"plane.org": org},
		}.WithDefaults())
		if err != nil {
			t.Fatalf("create %s: %v", org, err)
		}
		t.Cleanup(func() { _ = inst.Destroy(context.Background()) })
		return inst
	}
	a, other := mk("acme"), mk("globex")
	run := func(inst core.Instance, cmd string) core.ExecResult {
		res, err := inst.Exec(ctx, core.ExecRequest{Command: cmd, TimeoutS: 30})
		if err != nil {
			t.Fatalf("exec %q: %v", cmd, err)
		}
		return res
	}
	// The neighbour serves on every interface, as `python -m http.server` does by default.
	run(other, "(while true; do echo globex-private | nc -l -p 8000; done >/dev/null 2>&1 &)")
	ip := strings.TrimSpace(run(other, "ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1").Stdout)
	if net.ParseIP(ip) == nil {
		t.Fatalf("the neighbour's address: %q", ip)
	}
	if res := run(other, "for i in $(seq 20); do nc -w 2 "+ip+" 8000 </dev/null && exit 0; sleep 0.2; done; exit 1"); !strings.Contains(res.Stdout, "globex-private") {
		t.Fatalf("positive control: the neighbour serves on its own address: %+v", res)
	}
	for name, cmd := range map[string]string{
		"directly":          noProxy + "nc -w 3 " + ip + " 8000 </dev/null",
		"through the proxy": "wget -q -T 5 -O - http://" + ip + ":8000/",
	} {
		res := run(a, cmd+" && echo LEAK")
		t.Logf("acme -> globex %s (%s): exit=%d stdout=%q stderr=%q", name, ip, res.ExitCode, res.Stdout, res.Stderr)
		if strings.Contains(res.Stdout, "globex-private") || strings.Contains(res.Stdout, "LEAK") {
			t.Fatalf("one org's sandbox reached another's %s", name)
		}
	}
	if _, dnsErr := net.LookupHost("example.com"); dnsErr == nil {
		for org, inst := range map[string]core.Instance{"acme": a, "globex": other} {
			if res := run(inst, "wget -q -T 10 -O /dev/null http://example.com/ && echo ok"); !strings.Contains(res.Stdout, "ok") {
				t.Fatalf("%s's allowlisted host must stay reachable through the relay: %+v", org, res)
			}
		}
	}
}

// On a real Engine: a restarted daemon - a new Backend over the same Engine - adopts what
// the old one made, with their files: an airtight sandbox as it was, and a sandbox with domains on
// its own network, the relay joined again, its credential denied everything until its caller's
// next push, and reaching its allowlist after it.
func TestARestartedDaemonKeepsItsSandboxes(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	eg, port := liveProxy(t, ctx, sock)
	relay, pool := relayOn(t, sock, port), testPool(t)
	before := New(sock, WithEgress(eg, "http://sandbar-egress:7080"), WithRelay(relay), WithEgressPool(pool))
	ids := map[string]core.Spec{
		"sbtest-keep-air-" + randToken()[:6]: core.Spec{Image: "alpine:3.20"}.WithDefaults(),
		"sbtest-keep-dom-" + randToken()[:6]: core.Spec{Image: "alpine:3.20", AllowDomains: []string{"example.com"}}.WithDefaults(),
	}
	for id, spec := range ids {
		inst, err := before.Create(ctx, id, spec)
		if err != nil {
			t.Fatalf("create %s: %v", id, err)
		}
		layOut(t, ctx, inst, workshop)
		if err := inst.WriteFile(ctx, "/work/kept.txt", []byte(id), 0o644, core.OwnerAgent); err != nil {
			t.Fatalf("write: %v", err)
		}
	}

	after := New(sock, WithEgress(eg, "http://sandbar-egress:7080"), WithRelay(relay), WithEgressPool(pool))
	found, err := after.Adopt(ctx)
	if err != nil {
		t.Fatalf("adopt: %v", err)
	}
	mine := map[string]core.Adopted{}
	for _, f := range found {
		if _, ok := ids[f.ID]; ok {
			mine[f.ID] = f
			t.Cleanup(func() { _ = f.Instance.Destroy(context.Background()) })
		}
	}
	if len(mine) != len(ids) {
		t.Fatalf("want both sandboxes adopted, got %v", mine)
	}
	for id, f := range mine {
		if got, err := f.Instance.ReadFile(ctx, "/work/kept.txt"); err != nil || string(got) != id {
			t.Fatalf("%s keeps /work across the restart: %q %v", id, got, err)
		}
		if f.Spec.Image != "alpine:3.20" || len(f.Spec.AllowDomains) != len(ids[id].AllowDomains) {
			t.Fatalf("%s keeps its spec: %+v", id, f.Spec)
		}
		if len(f.Spec.AllowDomains) == 0 {
			continue
		}
		fetch := func() string {
			res, err := f.Instance.Exec(ctx, core.ExecRequest{Command: "wget -q -T 10 -O /dev/null http://example.com/ && echo ok", TimeoutS: 30})
			if err != nil {
				t.Fatalf("exec: %v", err)
			}
			return res.Stdout
		}
		if strings.Contains(fetch(), "ok") {
			t.Fatal("an adopted sandbox reaches nothing until its caller pushes its list")
		}
		if err := f.Instance.Egress(ctx, []string{"example.com"}); err != nil {
			t.Fatalf("the caller's push: %v", err)
		}
		if _, dnsErr := net.LookupHost("example.com"); dnsErr == nil && !strings.Contains(fetch(), "ok") {
			t.Fatal("after the push the adopted sandbox reaches its allowlist through the relay")
		}
	}
}

// noProxy strips the proxy env, so a probe is the process that ignores HTTP_PROXY.
const noProxy = "env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy "

// hostService starts a listener in the HOST's network namespace (a `--network host` container) on
// a random high port, answering every connection with a marker. It stands in for any service on
// the host - sandbard's own API port included (probe 4's host side) - and is removed at cleanup.
func hostService(t *testing.T, sock string) (port, marker string) {
	t.Helper()
	n, _ := strconv.ParseUint(randToken()[:4], 16, 32)
	port, marker = strconv.Itoa(20000+int(n%40000)), "SBTEST-HOST-"+randToken()[:8]
	svc, err := New(sock, WithNetwork("host")).Create(context.Background(), "sbtest-hostsvc-"+randToken()[:6], core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("start the host-side listener: %v", err)
	}
	t.Cleanup(func() {
		if err := svc.Destroy(context.Background()); err != nil {
			t.Logf("remove the host-side listener: %v", err)
		}
	})
	loop := "(while true; do echo " + marker + " | nc -l -p " + port + " || sleep 0.2; done >/dev/null 2>&1 &)"
	if _, err := svc.Exec(context.Background(), core.ExecRequest{Command: loop, TimeoutS: 10}); err != nil {
		t.Fatalf("start the host-side listener loop: %v", err)
	}
	return port, marker
}

// Probe 5, with a positive control: a service listening on the host is reachable from
// a container on the default bridge at that bridge's gateway - proof the probe can see one - and
// NOT from a sandbox with domains, at any address the host could hold. The sandbox's only link is
// its own /28 with no default route, so the one address that could be the host's is an in-subnet
// gateway; inhibit_ipv4 leaves none.
func TestADomainSandboxCannotReachAHostService(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	port, marker := hostService(t, sock)
	reach := func(inst core.Instance, target string, tries int) core.ExecResult {
		cmd := fmt.Sprintf("for i in $(seq %d); do %snc -w 3 %s %s </dev/null && exit 0; sleep 0.3; done; exit 1", tries, noProxy, target, port)
		res, err := inst.Exec(ctx, core.ExecRequest{Command: cmd, TimeoutS: 60})
		if err != nil {
			t.Fatalf("exec %q: %v", cmd, err)
		}
		return res
	}

	bridgeGW := gatewayOf(inspectNet(t, New(sock), "bridge"))
	if bridgeGW == "" {
		t.Skip("the default bridge reports no gateway: this platform cannot run the host probe")
	}
	control, err := New(sock, WithNetwork("bridge")).Create(ctx, "sbtest-control-"+randToken()[:6], core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create the control: %v", err)
	}
	defer control.Destroy(context.Background())
	res := reach(control, bridgeGW, 10)
	t.Logf("control: bridge -> %s:%s -> exit=%d stdout=%q stderr=%q", bridgeGW, port, res.ExitCode, res.Stdout, res.Stderr)
	if !strings.Contains(res.Stdout, marker) {
		t.Skipf("positive control failed: a container on the default bridge cannot reach a host listener at %s:%s, so this platform cannot run the probe", bridgeGW, port)
	}

	b := egressBackend(t, sock, &spyEgress{}, standInRelay(t, sock))
	inst, err := b.Create(ctx, "sbtest-s2host-"+randToken()[:6], core.Spec{Image: "alpine:3.20", AllowDomains: []string{"example.com"}}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())
	nw := inspectNet(t, b, inst.(*instance).net)
	if gw := gatewayOf(nw); !nw.Internal || gw != "" {
		t.Fatalf("the sandbox's network must be internal with no gateway, got internal=%v gateway=%q", nw.Internal, gw)
	}
	_, subnet, err := net.ParseCIDR(nw.IPAM.Config[0].Subnet)
	if err != nil || subnet.IP.To4() == nil {
		t.Fatalf("the sandbox network's IPv4 subnet: %v %v", subnet, err)
	}
	dot1 := append(net.IP(nil), subnet.IP.To4()...)
	dot1[3]++
	for what, target := range map[string]string{
		"its subnet's .1":              dot1.String(),
		"the default bridge's gateway": bridgeGW,
		"host.docker.internal":         "host.docker.internal",
	} {
		res := reach(inst, target, 1)
		t.Logf("probe 5: %s (%s:%s) -> exit=%d stdout=%q stderr=%q", what, target, port, res.ExitCode, res.Stdout, res.Stderr)
		if strings.Contains(res.Stdout, marker) {
			t.Errorf("probe 5: a sandbox with domains reaches a host service at %s (%s:%s) - report to the owner", what, target, port)
		}
	}
}

// Past IPv4, on a real Engine: a sandbox with domains has no IPv6
// address but loopback (the Sysctls belt - a bridge's fe80:: link-local would otherwise reach host
// services bound to ::), no default route, and no name resolution of its own. Docker's embedded
// resolver answering an outside name for a container on an internal network would be a way out
// past the proxy (DNS exfiltration), so a resolution here is a leak to report, never to weaken. A
// control on the default bridge proves the probe can see a resolution at all.
func TestADomainSandboxHasNoIPv6NoDefaultRouteAndNoOutsideDNS(t *testing.T) {
	sock := dockerSockOrSkip(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	run := func(inst core.Instance, cmd string) core.ExecResult {
		res, err := inst.Exec(ctx, core.ExecRequest{Command: cmd, TimeoutS: 60})
		if err != nil {
			t.Fatalf("exec %q: %v", cmd, err)
		}
		return res
	}
	inst, err := egressBackend(t, sock, &spyEgress{}, standInRelay(t, sock)).
		Create(ctx, "sbtest-s2v6-"+randToken()[:6], core.Spec{Image: "alpine:3.20", AllowDomains: []string{"example.com"}}.WithDefaults())
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer inst.Destroy(context.Background())

	v6 := run(inst, "cat /proc/net/if_inet6 2>/dev/null; true")
	t.Logf("/proc/net/if_inet6: %q", v6.Stdout)
	for _, line := range strings.Split(strings.TrimSpace(v6.Stdout), "\n") {
		if f := strings.Fields(line); len(f) > 0 && f[len(f)-1] != "lo" {
			t.Errorf("a sandbox with domains has an IPv6 address on %s: %q", f[len(f)-1], line)
		}
	}
	routes := run(inst, "ip route; ip -6 route 2>/dev/null; true")
	t.Logf("routes: %q", routes.Stdout)
	if strings.Contains(routes.Stdout, "default") {
		t.Errorf("a sandbox with domains has a default route: %q", routes.Stdout)
	}

	const resolve = noProxy + "getent hosts example.com && echo RESOLVED; " + noProxy + "nslookup example.com 2>&1; true"
	control, err := New(sock, WithNetwork("bridge")).Create(ctx, "sbtest-dnsctl-"+randToken()[:6], core.Spec{Image: "alpine:3.20"}.WithDefaults())
	if err != nil {
		t.Fatalf("create the control: %v", err)
	}
	defer control.Destroy(context.Background())
	ctl := run(control, resolve)
	t.Logf("control: bridge resolves example.com -> %q", ctl.Stdout)
	if !strings.Contains(ctl.Stdout, "RESOLVED") {
		t.Logf("positive control failed (no outside DNS from the default bridge either): the DNS probe would be vacuous, skipped")
		return
	}
	dns := run(inst, resolve)
	t.Logf("probe: the sandbox's network resolves example.com -> %q", dns.Stdout)
	if strings.Contains(dns.Stdout, "RESOLVED") {
		t.Errorf("DNS leak - report to the owner: a sandbox with domains resolved an outside name through "+
			"Docker's resolver with the proxy env unset: %q", dns.Stdout)
	}
}
