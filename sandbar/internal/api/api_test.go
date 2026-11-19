// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// A trivial in-memory backend so the API layer is tested without any isolation tech -
// the API's job is routing, auth, and error mapping, all of which are backend-agnostic.
type memBackend struct{ files map[string][]byte }

func (m *memBackend) Name() string { return "mem" }
func (m *memBackend) Create(_ context.Context, _ string, spec core.Spec) (core.Instance, error) {
	return &memInstance{files: m.files, noEgress: len(spec.AllowDomains) == 0}, nil
}

type memInstance struct {
	files    map[string][]byte
	noEgress bool
	allow    []string
}

func (i *memInstance) Exec(_ context.Context, r core.ExecRequest) (core.ExecResult, error) {
	if _, err := r.UID(); err != nil {
		return core.ExecResult{}, err
	}
	return core.ExecResult{ExitCode: 0, Stdout: "ran: " + r.Command}, nil
}
func (i *memInstance) WriteFile(_ context.Context, p string, d []byte, _ fs.FileMode, _ core.Owner) error {
	i.files[p] = append([]byte(nil), d...)
	return nil
}

// ReadFile and ListDir play a backend's two failures by path: "/broken" is the backend failing
// (a 502), and a path the sandbox does not hold is core.ErrNoSuchPath (a 404 no_such_path).
func (i *memInstance) ReadFile(_ context.Context, p string) ([]byte, error) {
	if p == "/broken" {
		return nil, fmt.Errorf("docker 500: engine unavailable")
	}
	if p == "/forbidden" {
		return nil, fmt.Errorf("read %s: %w", p, core.ErrForbiddenPath)
	}
	data, ok := i.files[p]
	if !ok {
		return nil, fmt.Errorf("%w: %s", core.ErrNoSuchPath, p)
	}
	return data, nil
}
func (i *memInstance) ListDir(_ context.Context, p string) ([]string, error) {
	switch p {
	case "/broken":
		return nil, fmt.Errorf("list /broken: ls: can't open '/broken': Permission denied")
	case "/nope":
		return nil, fmt.Errorf("%w: list /nope: ls: /nope: No such file or directory", core.ErrNoSuchPath)
	case "/empty":
		return nil, nil // the docker backend's empty directory: a nil slice
	}
	return []string{"a", "b/"}, nil
}
func (i *memInstance) Extract(context.Context, io.Reader) error { return nil }

// Archive plays the backend's two failures by path: "/refused" fails before a byte is written,
// "/broken" after enough bytes that the 200 and part of the body are already on the wire.
func (i *memInstance) Archive(_ context.Context, paths []string, w io.Writer) error {
	switch paths[0] {
	case "/refused":
		return fmt.Errorf("docker 500: engine unavailable")
	case "/broken":
		_, _ = w.Write(bytes.Repeat([]byte("x"), 64<<10))
		return fmt.Errorf("engine read failed")
	}
	return nil
}
func (i *memInstance) Denials(context.Context) ([]core.Denial, error) { return nil, nil }
func (i *memInstance) Destroy(context.Context) error                  { return nil }
func (i *memInstance) Egress(_ context.Context, domains []string) error {
	if i.noEgress {
		return fmt.Errorf("%w: this sandbox has no egress", core.ErrBadSpec)
	}
	i.allow = append([]string(nil), domains...)
	return nil
}

func newServer() http.Handler {
	reg := core.NewRegistry("mem", &memBackend{files: map[string][]byte{}})
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(reg, "secret", log).Handler()
}

func do(t *testing.T, h http.Handler, method, path, token string, body io.Reader) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, body)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestHealthzIsUnauthenticated(t *testing.T) {
	rec := do(t, newServer(), "GET", "/healthz", "", nil)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "mem") {
		t.Fatalf("healthz should be open and list backends: %d %s", rec.Code, rec.Body)
	}
}

func TestAuthGuards(t *testing.T) {
	h := newServer()
	if rec := do(t, h, "GET", "/v1/sandboxes", "", nil); rec.Code != 401 {
		t.Fatalf("missing token should 401, got %d", rec.Code)
	}
	if rec := do(t, h, "GET", "/v1/sandboxes", "wrong", nil); rec.Code != 401 {
		t.Fatalf("wrong token should 401, got %d", rec.Code)
	}
	if rec := do(t, h, "GET", "/v1/sandboxes", "secret", nil); rec.Code != 200 {
		t.Fatalf("right token should 200, got %d", rec.Code)
	}
}

func TestCreateExecReadFlow(t *testing.T) {
	h := newServer()

	// create with an explicit id
	body := `{"id":"t1","spec":{"image":"img"}}`
	rec := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body))
	if rec.Code != 201 {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}

	// exec
	rec = do(t, h, "POST", "/v1/sandboxes/t1/exec", "secret", strings.NewReader(`{"command":"echo hi"}`))
	var res core.ExecResult
	json.Unmarshal(rec.Body.Bytes(), &res)
	if res.Stdout != "ran: echo hi" {
		t.Fatalf("exec: %s", rec.Body)
	}

	// an unknown exec user is the caller's mistake (core.ErrBadSpec), not a backend failure - 400
	rec = do(t, h, "POST", "/v1/sandboxes/t1/exec", "secret", strings.NewReader(`{"command":"id","user":"root"}`))
	if rec.Code != 400 {
		t.Fatalf("exec with user=root should 400, got %d %s", rec.Code, rec.Body)
	}

	// write then read a file (query params carry path/mode/owner)
	rec = do(t, h, "PUT", "/v1/sandboxes/t1/files?path=/work/x&owner=agent", "secret", bytes.NewReader([]byte("data")))
	if rec.Code != 200 {
		t.Fatalf("write: %d %s", rec.Code, rec.Body)
	}
	rec = do(t, h, "GET", "/v1/sandboxes/t1/files?path=/work/x", "secret", nil)
	if rec.Body.String() != "data" {
		t.Fatalf("read: %q", rec.Body)
	}

	// missing sandbox → 404
	if rec := do(t, h, "POST", "/v1/sandboxes/ghost/exec", "secret", strings.NewReader(`{}`)); rec.Code != 404 {
		t.Fatalf("ghost exec should 404, got %d", rec.Code)
	}

	// destroy
	if rec := do(t, h, "DELETE", "/v1/sandboxes/t1", "secret", nil); rec.Code != 200 {
		t.Fatalf("destroy: %d", rec.Code)
	}
	if rec := do(t, h, "GET", "/v1/sandboxes/t1", "secret", nil); rec.Code != 404 {
		t.Fatalf("get after destroy should 404, got %d", rec.Code)
	}
}

// RR-missing-path: a path the sandbox does not hold is a 404 a client can tell from a gone
// sandbox by its code (no_such_path vs not_found); a backend failure stays a 502 with neither.
func TestAMissingPathIsA404NoSuchPathAndAMissingSandboxIsNotFound(t *testing.T) {
	h := newServer()
	if rec := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"p1","spec":{"image":"x"}}`)); rec.Code != 201 {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	cases := []struct {
		name, path string
		status     int
		code       string
	}{
		{"read a missing path", "/v1/sandboxes/p1/files?path=/task/context.md", 404, "no_such_path"},
		{"list a missing path", "/v1/sandboxes/p1/dir?path=/nope", 404, "no_such_path"},
		{"read on a missing sandbox", "/v1/sandboxes/ghost/files?path=/task/context.md", 404, "not_found"},
		{"list on a missing sandbox", "/v1/sandboxes/ghost/dir?path=/nope", 404, "not_found"},
		{"read when the backend fails", "/v1/sandboxes/p1/files?path=/broken", 502, ""},
		{"list when the backend fails", "/v1/sandboxes/p1/dir?path=/broken", 502, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := do(t, h, "GET", c.path, "secret", nil)
			var body struct {
				Error struct {
					Message string `json:"message"`
					Code    string `json:"code"`
				} `json:"error"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("an error body is JSON: %v %q", err, rec.Body)
			}
			if rec.Code != c.status || body.Error.Code != c.code || body.Error.Message == "" {
				t.Fatalf("want %d code %q, got %d %s", c.status, c.code, rec.Code, rec.Body)
			}
		})
	}
}

// An empty directory is `"entries": []`, the documented array - never `null`, which a client
// that iterates the list crashes on (the step boundary lists a fresh Machine's empty inputs).
func TestAnEmptyDirectoryListsAsAnEmptyArray(t *testing.T) {
	h := newServer()
	if rec := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"d1","spec":{"image":"x"}}`)); rec.Code != 201 {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	rec := do(t, h, "GET", "/v1/sandboxes/d1/dir?path=/empty", "secret", nil)
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != `{"entries":[]}` {
		t.Fatalf("want 200 {\"entries\":[]}, got %d %s", rec.Code, rec.Body)
	}
}

func TestBadSpecMapsTo400(t *testing.T) {
	rec := do(t, newServer(), "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"spec":{}}`))
	if rec.Code != 400 {
		t.Fatalf("empty image should 400, got %d %s", rec.Code, rec.Body)
	}
}

func TestEgressUpdateReplacesTheAllowlist(t *testing.T) {
	h := newServer()
	created := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"e1","spec":{"image":"x","allow_domains":["a.example"]}}`))
	if created.Code != 201 && created.Code != 200 {
		t.Fatalf("create: %d %s", created.Code, created.Body)
	}
	rec := do(t, h, "PUT", "/v1/sandboxes/e1/egress", "secret", strings.NewReader(`{"allow_domains":["b.example","c.example"]}`))
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"b.example"`) {
		t.Fatalf("egress update: %d %s", rec.Code, rec.Body)
	}
	empty := do(t, h, "PUT", "/v1/sandboxes/e1/egress", "secret", strings.NewReader(`{"allow_domains":[]}`))
	if empty.Code != 200 || !strings.Contains(empty.Body.String(), `"allow_domains":[]`) {
		t.Fatalf("empty replacement must be accepted and explicit: %d %s", empty.Code, empty.Body)
	}
}

func TestEgressUpdateOnAnAirtightSandboxIsRefused(t *testing.T) {
	h := newServer()
	do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"e2","spec":{"image":"x"}}`))
	rec := do(t, h, "PUT", "/v1/sandboxes/e2/egress", "secret", strings.NewReader(`{"allow_domains":["a.example"]}`))
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "no egress") {
		t.Fatalf("expected 400 naming the missing egress, got %d %s", rec.Code, rec.Body)
	}
}

func TestEgressUpdateUnknownSandboxIs404AndBadJSONIs400(t *testing.T) {
	h := newServer()
	if rec := do(t, h, "PUT", "/v1/sandboxes/nope/egress", "secret", strings.NewReader(`{}`)); rec.Code != 404 {
		t.Fatalf("unknown id: %d", rec.Code)
	}
	do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"e3","spec":{"image":"x","allow_domains":["a.example"]}}`))
	if rec := do(t, h, "PUT", "/v1/sandboxes/e3/egress", "secret", strings.NewReader(`{"allow_domains":"a.example"}`)); rec.Code != 400 {
		t.Fatalf("bad json: %d", rec.Code)
	}
	if rec := do(t, h, "PUT", "/v1/sandboxes/e3/egress", "", strings.NewReader(`{}`)); rec.Code != 401 {
		t.Fatalf("auth: %d", rec.Code)
	}
}

func TestLabelsRoundTripThroughCreateAndList(t *testing.T) {
	h := newServer()
	body := `{"id":"l1","spec":{"image":"x","allow_domains":["a.example"],"labels":{"plane.kind":"machine","plane.epoch":"3"}}}`
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	rr := do(t, h, "GET", "/v1/sandboxes", "secret", nil)
	if !strings.Contains(rr.Body.String(), `"plane.kind":"machine"`) || !strings.Contains(rr.Body.String(), `"plane.epoch":"3"`) {
		t.Fatalf("labels missing from the list: %s", rr.Body)
	}
}

func TestAStaleEgressVersionIs409(t *testing.T) {
	h := newServer()
	body := `{"id":"v1","spec":{"image":"x","allow_domains":["a.example"]},"egress_version":3}`
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	stale := do(t, h, "PUT", "/v1/sandboxes/v1/egress", "secret", strings.NewReader(`{"allow_domains":["b.example"],"version":2}`))
	if stale.Code != http.StatusConflict {
		t.Fatalf("stale version: want 409, got %d %s", stale.Code, stale.Body)
	}
	fresh := do(t, h, "PUT", "/v1/sandboxes/v1/egress", "secret", strings.NewReader(`{"allow_domains":["b.example"],"version":4}`))
	if fresh.Code != http.StatusOK || !strings.Contains(fresh.Body.String(), `"version":4`) {
		t.Fatalf("fresh version: %d %s", fresh.Code, fresh.Body)
	}
}

// A backend failure after the 200 went out must reach the client as a BROKEN body, never a clean
// end: a cut-short tar that ends cleanly is indistinguishable from a small checkpoint.
func TestAnArchiveThatFailsMidStreamIsAborted(t *testing.T) {
	h := newServer()
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"a1","spec":{"image":"x"}}`)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	srv := httptest.NewServer(h)
	defer srv.Close()
	req, _ := http.NewRequest("GET", srv.URL+"/v1/sandboxes/a1/archive?path=/broken", nil)
	req.Header.Set("Authorization", "Bearer secret")
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatalf("the 200 was sent before the failure: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status: %d", resp.StatusCode)
	}
	if _, err := io.ReadAll(resp.Body); err == nil {
		t.Fatal("a failed archive must abort the body, not end it cleanly")
	}
}

// A backend failure before any byte is still an ordinary error response, carrying the reason.
func TestAnArchiveThatFailsBeforeAnyByteIsA502(t *testing.T) {
	h := newServer()
	do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"a2","spec":{"image":"x"}}`))
	rr := do(t, h, "GET", "/v1/sandboxes/a2/archive?path=/refused", "secret", nil)
	if rr.Code != http.StatusBadGateway || !strings.Contains(rr.Body.String(), "engine unavailable") {
		t.Fatalf("want 502 naming the reason, got %d %s", rr.Code, rr.Body)
	}
}

// Gate 3: the version sandbar holds rides the 409 as JSON, so the driver never parses
// prose; the message keeps its old wording for a driver from before the field.
func TestAStaleEgressPushNamesTheHeldVersionAsJSON(t *testing.T) {
	h := newServer()
	body := `{"id":"v2","spec":{"image":"x","allow_domains":["a.example"]},"egress_version":3}`
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	stale := do(t, h, "PUT", "/v1/sandboxes/v2/egress", "secret", strings.NewReader(`{"allow_domains":["b.example"],"version":2}`))
	if stale.Code != http.StatusConflict {
		t.Fatalf("want 409, got %d %s", stale.Code, stale.Body)
	}
	var got struct {
		Error struct {
			Message string `json:"message"`
			Code    string `json:"code"`
			Version int64  `json:"version"`
		} `json:"error"`
	}
	if err := json.Unmarshal(stale.Body.Bytes(), &got); err != nil {
		t.Fatalf("409 body is not JSON: %v %s", err, stale.Body)
	}
	if got.Error.Code != "stale" || got.Error.Version != 3 || !strings.Contains(got.Error.Message, "is not newer than 3") {
		t.Fatalf("the 409 must carry code=stale and the held version 3, keeping its message: %s", stale.Body)
	}
}

// stateInstance is a memInstance whose backend reports a State and starts a stopped one
// (core.Stater). Start fails with startErr when one is set.
type stateInstance struct {
	*memInstance
	state    core.State
	startErr error
}

func (i *stateInstance) State(context.Context) core.State { return i.state }
func (i *stateInstance) Start(context.Context) error {
	if i.startErr != nil {
		return i.startErr
	}
	i.state = core.StateRunning
	return nil
}

type stateBackend struct {
	state    core.State
	startErr error
}

func (b *stateBackend) Name() string { return "st" }
func (b *stateBackend) Create(_ context.Context, _ string, _ core.Spec) (core.Instance, error) {
	return &stateInstance{memInstance: &memInstance{files: map[string][]byte{}}, state: b.state, startErr: b.startErr}, nil
}

// Gate 2: GET one and GET list carry the state a backend reports, and say nothing for
// a backend that cannot report one.
func TestRecordsCarryTheStateTheirBackendReports(t *testing.T) {
	reg := core.NewRegistry("mem", &memBackend{files: map[string][]byte{}}, &stateBackend{state: core.StateStopped})
	h := New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil))).Handler()
	for _, body := range []string{
		`{"id":"s1","spec":{"image":"x","backend":"st"}}`,
		`{"id":"m1","spec":{"image":"x"}}`,
	} {
		if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body)); rr.Code != http.StatusCreated {
			t.Fatalf("create: %d %s", rr.Code, rr.Body)
		}
	}
	if rr := do(t, h, "GET", "/v1/sandboxes/s1", "secret", nil); !strings.Contains(rr.Body.String(), `"state":"stopped"`) {
		t.Fatalf("a stopped sandbox must say so: %s", rr.Body)
	}
	if rr := do(t, h, "GET", "/v1/sandboxes/m1", "secret", nil); strings.Contains(rr.Body.String(), `"state"`) {
		t.Fatalf("a backend with no state reports none: %s", rr.Body)
	}
	rr := do(t, h, "GET", "/v1/sandboxes", "secret", nil)
	var list struct {
		Items []struct {
			ID    string `json:"id"`
			State string `json:"state"`
		} `json:"items"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, it := range list.Items {
		got[it.ID] = it.State
	}
	if got["s1"] != "stopped" || got["m1"] != "" || len(got) != 2 {
		t.Fatalf("list states: %v", got)
	}
}

// Gate 2: a stopped sandbox still holds its files, so start runs it again and answers
// with its record, now running. A sandbox or container that is gone is 404 not_found (the driver
// reads it as gone); an Engine failure is the backend's; a backend that reports no state never
// says stopped, and cannot be asked to start.
func TestStartRunsAStoppedSandboxAgain(t *testing.T) {
	cases := []struct {
		name, id string
		startErr error
		status   int
		code     string
	}{
		{"a stopped sandbox", "s1", nil, 200, ""},
		{"its container is gone", "s1", fmt.Errorf("%w: its container is gone", core.ErrNotFound), 404, "not_found"},
		{"the engine fails", "s1", fmt.Errorf("%w: start: docker 500", core.ErrBackend), 503, ""},
		{"no such sandbox", "ghost", nil, 404, "not_found"},
		{"a backend with no state", "m1", nil, 400, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			reg := core.NewRegistry("mem", &memBackend{files: map[string][]byte{}},
				&stateBackend{state: core.StateStopped, startErr: c.startErr})
			h := New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil))).Handler()
			for _, body := range []string{
				`{"id":"s1","spec":{"image":"x","backend":"st"}}`,
				`{"id":"m1","spec":{"image":"x"}}`,
			} {
				if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body)); rr.Code != http.StatusCreated {
					t.Fatalf("create: %d %s", rr.Code, rr.Body)
				}
			}
			rr := do(t, h, "POST", "/v1/sandboxes/"+c.id+"/start", "secret", nil)
			var body struct {
				ID    string `json:"id"`
				State string `json:"state"`
				Error struct {
					Code string `json:"code"`
				} `json:"error"`
			}
			if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
				t.Fatalf("the answer is JSON: %v %q", err, rr.Body)
			}
			if rr.Code != c.status || body.Error.Code != c.code {
				t.Fatalf("want %d code %q, got %d %s", c.status, c.code, rr.Code, rr.Body)
			}
			if c.status == 200 && (body.ID != "s1" || body.State != "running") {
				t.Fatalf("a started sandbox answers its record, running: %s", rr.Body)
			}
		})
	}
}

// removingBackend's instances take back system paths, recording what they were asked.
type removingBackend struct{ removed *[]string }

func (b *removingBackend) Name() string { return "rm" }
func (b *removingBackend) Create(_ context.Context, _ string, _ core.Spec) (core.Instance, error) {
	return &removingInstance{memInstance: &memInstance{files: map[string][]byte{}}, removed: b.removed}, nil
}

type removingInstance struct {
	*memInstance
	removed *[]string
}

func (i *removingInstance) Remove(_ context.Context, paths []string) error {
	*i.removed = append(*i.removed, paths...)
	return nil
}

// The system takes back what it gave - paths below /task, removed as root -
// and nothing else. A path outside it is refused before the backend is asked (400), and a backend
// that cannot remove (firecracker) says so.
func TestRemoveTakesBackOnlySystemPaths(t *testing.T) {
	var removed []string
	reg := core.NewRegistry("rm", &removingBackend{removed: &removed}, &memBackend{files: map[string][]byte{}})
	h := New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil))).Handler()
	for _, body := range []string{`{"id":"s1","spec":{"image":"x"}}`, `{"id":"m1","spec":{"image":"x","backend":"mem"}}`} {
		if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(body)); rr.Code != http.StatusCreated {
			t.Fatalf("create: %d %s", rr.Code, rr.Body)
		}
	}
	rr := do(t, h, "POST", "/v1/sandboxes/s1/remove", "secret", strings.NewReader(`{"paths":["/task/inputs/a.md","/task/inputs/sub"]}`))
	if rr.Code != http.StatusOK || !strings.Contains(rr.Body.String(), `"removed":2`) ||
		strings.Join(removed, ",") != "/task/inputs/a.md,/task/inputs/sub" {
		t.Fatalf("system paths reach the backend: %d %s %v", rr.Code, rr.Body, removed)
	}
	for _, bad := range []string{`{"paths":["/etc/passwd"]}`, `{"paths":["/task"]}`, `{"paths":["/task/../work/x"]}`, `{"paths":["/task/ok","/tasks/x"]}`, `not json`} {
		removed = nil
		if rr := do(t, h, "POST", "/v1/sandboxes/s1/remove", "secret", strings.NewReader(bad)); rr.Code != http.StatusBadRequest || len(removed) != 0 {
			t.Fatalf("%s must be refused before the backend: %d %s %v", bad, rr.Code, rr.Body, removed)
		}
	}
	if rr := do(t, h, "POST", "/v1/sandboxes/m1/remove", "secret", strings.NewReader(`{"paths":["/task/a"]}`)); rr.Code != http.StatusBadRequest {
		t.Fatalf("a backend that cannot remove answers 400: %d %s", rr.Code, rr.Body)
	}
	if rr := do(t, h, "POST", "/v1/sandboxes/ghost/remove", "secret", strings.NewReader(`{"paths":["/task/a"]}`)); rr.Code != http.StatusNotFound {
		t.Fatalf("no such sandbox: %d %s", rr.Code, rr.Body)
	}
}

func TestFileRoutesRefuseBadInput(t *testing.T) {
	h := newServer()
	if rec := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"c1","spec":{"image":"x"}}`)); rec.Code/100 != 2 {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	for _, c := range []struct{ name, method, url string }{
		{"write unclean", "PUT", "/v1/sandboxes/c1/files?path=/work/../etc/x"},
		{"write relative", "PUT", "/v1/sandboxes/c1/files?path=work/x"},
		{"write unknown owner", "PUT", "/v1/sandboxes/c1/files?path=/work/x&owner=root"},
		{"read unclean", "GET", "/v1/sandboxes/c1/files?path=/work//x"},
		{"list unclean", "GET", "/v1/sandboxes/c1/dir?path=/work/.."},
	} {
		rec := do(t, h, c.method, c.url, "secret", strings.NewReader("x"))
		if rec.Code != 400 {
			t.Errorf("%s: %d %s, want 400", c.name, rec.Code, rec.Body)
		}
	}
}

func TestForbiddenPathIs403(t *testing.T) {
	h := newServer()
	do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"f1","spec":{"image":"x"}}`))
	rec := do(t, h, "GET", "/v1/sandboxes/f1/files?path=/forbidden", "secret", nil)
	var body struct{ Error struct{ Code string } }
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if rec.Code != 403 || body.Error.Code != "forbidden_path" {
		t.Fatalf("got %d %s, want 403 forbidden_path", rec.Code, rec.Body)
	}
}
