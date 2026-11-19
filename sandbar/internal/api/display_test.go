// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package api

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/backend/docker"
	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/ticket"
)

// attachBackend is the mem backend whose instances can also stream an exec (core.Attacher): an
// echo, so bytes sent up the display pipe come straight back down it. It records each attach.
type attachBackend struct {
	memBackend
	hold     time.Duration // each Attach takes this long, so concurrent requests overlap in it
	mu       sync.Mutex
	attached []core.ExecRequest
}

func (b *attachBackend) Create(ctx context.Context, id string, spec core.Spec) (core.Instance, error) {
	inst, err := b.memBackend.Create(ctx, id, spec)
	if err != nil {
		return nil, err
	}
	return &attachInstance{memInstance: inst.(*memInstance), b: b}, nil
}

func (b *attachBackend) requests() []core.ExecRequest {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]core.ExecRequest(nil), b.attached...)
}

type attachInstance struct {
	*memInstance
	b *attachBackend
}

func (i *attachInstance) Attach(_ context.Context, r core.ExecRequest) (io.ReadWriteCloser, error) {
	if _, err := r.UID(); err != nil {
		return nil, err
	}
	i.b.mu.Lock()
	i.b.attached = append(i.b.attached, r)
	i.b.mu.Unlock()
	time.Sleep(i.b.hold)
	pr, pw := io.Pipe()
	return &echo{r: pr, w: pw}, nil
}

// echo is a loop: what is written is what is read.
type echo struct {
	r *io.PipeReader
	w *io.PipeWriter
}

func (e *echo) Read(p []byte) (int, error)  { return e.r.Read(p) }
func (e *echo) Write(p []byte) (int, error) { return e.w.Write(p) }
func (e *echo) Close() error {
	_ = e.w.Close()
	return e.r.Close()
}

const displayKid = "k1"

// displayServer is a daemon with the display route on, one attachable sandbox "d1", and the
// private key whose public half it trusts.
func displayServer(t *testing.T) (http.Handler, ed25519.PrivateKey, *attachBackend) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	b := &attachBackend{memBackend: memBackend{files: map[string][]byte{}}}
	reg := core.NewRegistry("mem", b)
	v := ticket.NewVerifier(map[string]ed25519.PublicKey{displayKid: pub}, time.Now().Add(-time.Hour))
	h := New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil))).WithDisplay(v).Handler()
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"d1","spec":{"image":"x"}}`)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	return h, priv, b
}

// mintDisplay signs a ticket the way the Plane API's minter does (computer/operations/tickets.py).
func mintDisplay(t *testing.T, priv ed25519.PrivateKey, sandbox, jti string, iat time.Time) string {
	t.Helper()
	raw, err := json.Marshal(ticket.Claims{Sandbox: sandbox, Iat: iat.Unix(), Exp: iat.Unix() + 30, Jti: jti, Kid: displayKid})
	if err != nil {
		t.Fatal(err)
	}
	sig := ed25519.Sign(priv, append([]byte(ticket.Domain), raw...))
	return ticket.Prefix + base64.RawURLEncoding.EncodeToString(raw) + "." + base64.RawURLEncoding.EncodeToString(sig)
}

// displayReq is a display request with the given Authorization and, when upgrade is set, the
// upgrade headers.
func displayReq(method, path, auth string, upgrade bool) *http.Request {
	req := httptest.NewRequest(method, path, nil)
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	if upgrade {
		req.Header.Set("Connection", "Upgrade")
		req.Header.Set("Upgrade", "sandbar-display")
	}
	return req
}

func serve(h http.Handler, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// upgrade dials a live server and sends a display request with tk, returning the raw connection,
// its reader (which may hold bytes past the response) and the response.
func upgrade(t *testing.T, srv *httptest.Server, path, tk string) (net.Conn, *bufio.Reader, *http.Response) {
	t.Helper()
	conn, err := net.Dial("tcp", srv.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
	req, _ := http.NewRequest("GET", srv.URL+path, nil)
	req.Header.Set("Authorization", "Ticket "+tk)
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "sandbar-display")
	if err := req.Write(conn); err != nil {
		t.Fatal(err)
	}
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, req)
	if err != nil {
		t.Fatal(err)
	}
	return conn, br, resp
}

func TestADisplayTicketUpgradesToTheBridgeAndBytesFlowBothWays(t *testing.T) {
	h, priv, b := displayServer(t)
	srv := httptest.NewServer(h)
	defer srv.Close()
	conn, br, resp := upgrade(t, srv, "/v1/sandboxes/d1/display", mintDisplay(t, priv, "d1", "j-echo", time.Now()))
	defer conn.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols || !strings.EqualFold(resp.Header.Get("Upgrade"), "sandbar-display") {
		t.Fatalf("want 101 Upgrade: sandbar-display, got %d %v", resp.StatusCode, resp.Header)
	}
	if _, err := conn.Write([]byte("\x00\x00\x00\x0d{\"t\":\"hello\"}")); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, 17)
	if _, err := io.ReadFull(br, got); err != nil || string(got) != "\x00\x00\x00\x0d{\"t\":\"hello\"}" {
		t.Fatalf("the pipe must carry bytes unchanged: %q %v", got, err)
	}
	reqs := b.requests()
	if len(reqs) != 1 || reqs[0].User != "browser" || reqs[0].Command != "cd /home/browser && exec python3 -m sidecar.screen" ||
		reqs[0].Env["PYTHONPATH"] != "/opt/plane/browser" || reqs[0].Env["HOME"] != "/home/browser" {
		t.Fatalf("the bridge must be attached as the browser user: %+v", reqs)
	}
}

func TestADisplayTicketIsSingleUse(t *testing.T) {
	h, priv, _ := displayServer(t)
	srv := httptest.NewServer(h)
	defer srv.Close()
	tk := mintDisplay(t, priv, "d1", "j-once", time.Now())
	conn, _, resp := upgrade(t, srv, "/v1/sandboxes/d1/display", tk)
	conn.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("first use: %d", resp.StatusCode)
	}
	again, _, resp := upgrade(t, srv, "/v1/sandboxes/d1/display", tk)
	defer again.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusUnauthorized || !strings.Contains(string(body), "replayed") {
		t.Fatalf("a replay must be 401 replayed, got %d %s", resp.StatusCode, body)
	}
}

// One ticket starts at most one exec, however many requests race with it: the ticket is spent
// before the bridge is attached, so a loser never opens a screen session on the daemon (a session
// can start a stopped Chromium) - and never sees a 101.
func TestARacingTicketStartsOneExec(t *testing.T) {
	h, priv, b := displayServer(t)
	b.hold = 100 * time.Millisecond
	srv := httptest.NewServer(h)
	defer srv.Close()
	tk := mintDisplay(t, priv, "d1", "j-race", time.Now())
	codes := make(chan int, 8)
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			conn, _, resp := upgrade(t, srv, "/v1/sandboxes/d1/display", tk)
			codes <- resp.StatusCode
			conn.Close()
		}()
	}
	wg.Wait()
	close(codes)
	upgraded := 0
	for c := range codes {
		if c == http.StatusSwitchingProtocols {
			upgraded++
		} else if c != http.StatusUnauthorized {
			t.Fatalf("a loser must be 401, got %d", c)
		}
	}
	if upgraded != 1 || len(b.requests()) != 1 {
		t.Fatalf("one ticket: want one 101 and one exec, got %d and %d", upgraded, len(b.requests()))
	}
}

// A caller that stops reading cannot pin the exec forever: every write to the taken-over
// connection has a deadline.
func TestAWriteToAStalledCallerTimesOut(t *testing.T) {
	near, far := net.Pipe() // nobody ever reads far
	defer far.Close()
	w := writeBound{Conn: near, d: 50 * time.Millisecond}
	done := make(chan error, 1)
	go func() {
		_, err := w.Write([]byte("a frame nobody reads"))
		done <- err
	}()
	select {
	case err := <-done:
		if !errors.Is(err, os.ErrDeadlineExceeded) {
			t.Fatalf("want a deadline error, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("a write to a caller that never reads must time out")
	}
}

func TestTheDisplayRouteRefuses(t *testing.T) {
	h, priv, _ := displayServer(t)
	now := time.Now()
	good := "Ticket " + mintDisplay(t, priv, "d1", "j-good", now)
	cases := []struct {
		name string
		req  *http.Request
		code int
		says string
	}{
		{"the master token", displayReq("GET", "/v1/sandboxes/d1/display", "Bearer secret", true), 401, "ticket is required"},
		{"no credential", displayReq("GET", "/v1/sandboxes/d1/display", "", true), 401, "ticket is required"},
		{"an expired ticket", displayReq("GET", "/v1/sandboxes/d1/display", "Ticket "+mintDisplay(t, priv, "d1", "j-old", now.Add(-time.Minute)), true), 401, "expired"},
		{"another sandbox's ticket", displayReq("GET", "/v1/sandboxes/d1/display", "Ticket "+mintDisplay(t, priv, "d2", "j-d2", now), true), 401, "mismatch"},
		{"a forged ticket", displayReq("GET", "/v1/sandboxes/d1/display", good[:len(good)-4]+"AAAA", true), 401, "refused"},
		{"no upgrade headers", displayReq("GET", "/v1/sandboxes/d1/display", good, false), 400, "Upgrade: sandbar-display"},
		{"an unknown sandbox", displayReq("GET", "/v1/sandboxes/ghost/display", "Ticket "+mintDisplay(t, priv, "ghost", "j-ghost", now), true), 404, "no such sandbox"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := serve(h, tc.req)
			if rec.Code != tc.code || !strings.Contains(rec.Body.String(), tc.says) {
				t.Fatalf("want %d naming %q, got %d %s", tc.code, tc.says, rec.Code, rec.Body)
			}
		})
	}
}

// Every other route refuses a display ticket - also on a tokenless dev daemon, where any other
// request passes.
func TestADisplayTicketOpensNothingElse(t *testing.T) {
	h, priv, _ := displayServer(t)
	tk := "Ticket " + mintDisplay(t, priv, "d1", "j-exec", time.Now())
	for _, path := range []string{"/v1/sandboxes/d1/exec", "/v1/sandboxes"} {
		req := displayReq("POST", path, tk, false)
		req.Body = io.NopCloser(strings.NewReader(`{"command":"id"}`))
		if rec := serve(h, req); rec.Code != http.StatusUnauthorized {
			t.Fatalf("POST %s with a ticket: want 401, got %d %s", path, rec.Code, rec.Body)
		}
	}
	open := New(core.NewRegistry("mem", &memBackend{files: map[string][]byte{}}), "", slog.New(slog.NewTextHandler(io.Discard, nil))).Handler()
	if rec := serve(open, displayReq("GET", "/v1/sandboxes", tk, false)); rec.Code != http.StatusUnauthorized {
		t.Fatalf("a tokenless daemon must still refuse a ticket: %d", rec.Code)
	}
	if rec := serve(open, displayReq("GET", "/v1/sandboxes", "", false)); rec.Code != http.StatusOK {
		t.Fatalf("a tokenless daemon still serves an unauthenticated list: %d", rec.Code)
	}
}

func TestTheDisplayRouteIsOffWithoutKeysAndNeedsAnAttacher(t *testing.T) {
	off := newServer() // no WithDisplay: no SANDBAR_DISPLAY_KEYS
	if rec := serve(off, displayReq("GET", "/v1/sandboxes/d1/display", "Ticket dt_x.y", true)); rec.Code != http.StatusServiceUnavailable ||
		!strings.Contains(rec.Body.String(), "display disabled") {
		t.Fatalf("no keys: want 503 display disabled, got %d %s", rec.Code, rec.Body)
	}

	pub, priv, _ := ed25519.GenerateKey(nil)
	reg := core.NewRegistry("mem", &memBackend{files: map[string][]byte{}}) // memInstance is no Attacher
	v := ticket.NewVerifier(map[string]ed25519.PublicKey{displayKid: pub}, time.Now().Add(-time.Hour))
	h := New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil))).WithDisplay(v).Handler()
	do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"m1","spec":{"image":"x"}}`))
	rec := serve(h, displayReq("GET", "/v1/sandboxes/m1/display", "Ticket "+mintDisplay(t, priv, "m1", "j-m1", time.Now()), true))
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("a backend that cannot stream: want 501, got %d %s", rec.Code, rec.Body)
	}
}

// TestTheDisplayRouteReachesARealBrowserBridge pipes one framed message through the whole route on
// the Docker backend: a browser sandbox, its sidecar daemon started by one verb, the
// display upgrade, then hello up and the daemon's hello v1 down. Skipped without a Docker socket
// (SANDBAR_DOCKER_SOCK) and the browser image (SANDBAR_BROWSER_IMAGE, `just sandbar-browser-image`).
func TestTheDisplayRouteReachesARealBrowserBridge(t *testing.T) {
	sock := os.Getenv("SANDBAR_DOCKER_SOCK")
	if sock == "" {
		sock = "/var/run/docker.sock"
	}
	image := os.Getenv("SANDBAR_BROWSER_IMAGE")
	if _, err := os.Stat(sock); err != nil || image == "" {
		t.Skip("needs a Docker socket and SANDBAR_BROWSER_IMAGE")
	}
	pub, priv, _ := ed25519.GenerateKey(nil)
	reg := core.NewRegistry("docker", docker.New(sock))
	v := ticket.NewVerifier(map[string]ed25519.PublicKey{displayKid: pub}, time.Now().Add(-time.Hour))
	srv := httptest.NewServer(New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil))).WithDisplay(v).Handler())
	defer srv.Close()
	id := "sb-route-" + strings.ReplaceAll(time.Now().Format("150405.000"), ".", "")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	call := func(method, path, body string) map[string]any {
		req, _ := http.NewRequestWithContext(ctx, method, srv.URL+path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer secret")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		if resp.StatusCode >= 300 {
			t.Fatalf("%s %s: %d %v", method, path, resp.StatusCode, out)
		}
		return out
	}
	call("POST", "/v1/sandboxes", `{"id":"`+id+`","spec":{"image":"`+image+`"}}`)
	defer func() { _ = reg.Destroy(context.Background(), id) }()
	opened := call("POST", "/v1/sandboxes/"+id+"/exec", `{"command":"cd /home/browser && python3 -m sidecar.ctl","user":"browser","timeout_s":60,`+
		`"env":{"SIDECAR_CMD":"{\"verb\":\"open\",\"url\":\"about:blank\"}","PYTHONPATH":"/opt/plane/browser","HOME":"/home/browser"}}`)
	if !strings.Contains(opened["stdout"].(string), `"ok": true`) && !strings.Contains(opened["stdout"].(string), `"ok":true`) {
		t.Fatalf("open about:blank: %v", opened)
	}

	conn, br, resp := upgrade(t, srv, "/v1/sandboxes/"+id+"/display", mintDisplay(t, priv, id, "j-real", time.Now()))
	defer conn.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("want 101, got %d %s", resp.StatusCode, body)
	}
	hello := []byte(`{"n":0,"t":"hello","v":1}`)
	if _, err := conn.Write(append(binary.BigEndian.AppendUint32(nil, uint32(len(hello))), hello...)); err != nil {
		t.Fatal(err)
	}
	var size [4]byte
	if _, err := io.ReadFull(br, size[:]); err != nil {
		t.Fatalf("no framed message through the route: %v", err)
	}
	head := make([]byte, binary.BigEndian.Uint32(size[:]))
	if len(head) == 0 || len(head) > 4096 {
		t.Fatalf("header length %d is outside the wire's bounds", len(head))
	}
	if _, err := io.ReadFull(br, head); err != nil {
		t.Fatal(err)
	}
	var h map[string]any
	if err := json.Unmarshal(head, &h); err != nil || h["t"] != "hello" || h["v"] != float64(1) {
		t.Fatalf("the first message through the route must be hello v1: %s (%v)", head, err)
	}
}
