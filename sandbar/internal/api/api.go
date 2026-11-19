// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package api is sandbar's HTTP surface: the one interface consumers in any language
// speak. It hides Go, it hides which backend isolated the work, and it maps core's
// sentinel errors onto status codes. Auth is a single bearer token (the daemon runs
// on a private network beside its caller - this is a tripwire against a misrouted
// request, not a public authentication system).
//
// The routes, top to bottom, ARE the API:
//
//	GET    /healthz                     liveness + which backends are available
//	POST   /v1/sandboxes                create; body = core.Spec (+ optional "id", "egress_version")
//	GET    /v1/sandboxes                list (each with its state)
//	GET    /v1/sandboxes/{id}           one record, with its state
//	DELETE /v1/sandboxes/{id}           destroy
//	POST   /v1/sandboxes/{id}/start     run a stopped sandbox again, its files kept; answers the record
//	POST   /v1/sandboxes/{id}/exec      run a command
//	POST   /v1/sandboxes/{id}/remove    take back system paths below /task, as root; body {"paths": [...]}
//	PUT    /v1/sandboxes/{id}/egress    replace the egress allowlist, versioned (409 {error.version} when stale)
//	PUT    /v1/sandboxes/{id}/files     write one file (query: path, mode, owner)
//	GET    /v1/sandboxes/{id}/files     read one file (query: path) - raw bytes; 404 {error.code: no_such_path} when absent
//	GET    /v1/sandboxes/{id}/dir       list a directory (query: path); 404 {error.code: no_such_path} when absent
//	GET    /v1/sandboxes/{id}/archive   tar of query: path (repeatable) - stream; aborted on failure
//	POST   /v1/sandboxes/{id}/archive   extract a tar at / - stream
//	GET    /v1/sandboxes/{id}/display   upgrade to the browser's screen bridge - a display ticket
//	                                    (Authorization: Ticket dt_...), never the token (display.go)
package api

import (
	"bufio"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/ticket"
)

// Server wires a registry and a token into an http.Handler.
type Server struct {
	reg     *core.Registry
	token   string
	log     *slog.Logger
	tickets *ticket.Verifier // display tickets; nil = the display route is off (WithDisplay)
	// largeWrites holds one slot per large file write being read (writeSlot).
	largeWrites chan struct{}
}

func New(reg *core.Registry, token string, log *slog.Logger) *Server {
	return &Server{reg: reg, token: token, log: log, largeWrites: make(chan struct{}, maxLargeWrites)}
}

// Handler returns the routed, authenticated handler. Uses net/http's 1.22 method+
// pattern router - no third-party mux, in keeping with sandbar's stdlib-only rule.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.healthz) // unauthenticated by design
	mux.HandleFunc("POST /v1/sandboxes", s.auth(s.create))
	mux.HandleFunc("GET /v1/sandboxes", s.auth(s.list))
	mux.HandleFunc("GET /v1/sandboxes/{id}", s.auth(s.get))
	mux.HandleFunc("DELETE /v1/sandboxes/{id}", s.auth(s.destroy))
	mux.HandleFunc("POST /v1/sandboxes/{id}/start", s.auth(s.start))
	mux.HandleFunc("POST /v1/sandboxes/{id}/exec", s.auth(s.exec))
	mux.HandleFunc("POST /v1/sandboxes/{id}/remove", s.auth(s.remove))
	mux.HandleFunc("PUT /v1/sandboxes/{id}/egress", s.auth(s.egress))
	mux.HandleFunc("PUT /v1/sandboxes/{id}/files", s.auth(s.writeFile))
	mux.HandleFunc("GET /v1/sandboxes/{id}/files", s.auth(s.readFile))
	mux.HandleFunc("GET /v1/sandboxes/{id}/dir", s.auth(s.listDir))
	mux.HandleFunc("GET /v1/sandboxes/{id}/archive", s.auth(s.archive))
	mux.HandleFunc("POST /v1/sandboxes/{id}/archive", s.auth(s.extract))
	mux.HandleFunc("GET /v1/sandboxes/{id}/denials", s.auth(s.denials))
	mux.HandleFunc("GET /v1/sandboxes/{id}/display", s.display) // a display ticket, never s.auth
	return logging(s.log, mux)
}

// ── middleware ────────────────────────────────────────────────────────────────

func (s *Server) auth(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		got := r.Header.Get("Authorization")
		// A display ticket opens the display route and nothing else - even on a tokenless dev
		// daemon, where every other request passes. The token compares in constant time, so its
		// bytes cannot be guessed one at a time from response timings.
		if strings.HasPrefix(got, "Ticket ") || (s.token != "" && !tokenMatches(got, s.token)) {
			writeErr(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		h(w, r)
	}
}

// tokenMatches reports whether an Authorization header carries the bearer token.
func tokenMatches(header, token string) bool {
	return subtle.ConstantTimeCompare([]byte(header), []byte("Bearer "+token)) == 1
}

func logging(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(sw, r)
		log.Info("request",
			"method", r.Method, "path", r.URL.Path,
			"status", sw.status, "ms", time.Since(start).Milliseconds())
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// Flush lets streaming handlers (archive) push bytes as they go.
func (s *statusWriter) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Hijack lets the display route take its connection over (http.Hijacker) through this wrapper.
// The request is logged as 101: what happened on the connection afterwards, display.go logs. The
// access log prints method, path, status and duration only, so a ticket (a header) never reaches it.
func (s *statusWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	h, ok := s.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, http.ErrNotSupported
	}
	conn, rw, err := h.Hijack()
	if err == nil {
		s.status = http.StatusSwitchingProtocols
	}
	return conn, rw, err
}

// ── handlers ──────────────────────────────────────────────────────────────────

func (s *Server) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok", "backends": s.reg.Backends(),
	})
}

type createBody struct {
	ID   string    `json:"id,omitempty"`
	Spec core.Spec `json:"spec"`
	// EgressVersion is the version spec.allow_domains carries (0 = unversioned): a later
	// PUT .../egress must be newer, or it is refused as stale.
	EgressVersion int64 `json:"egress_version,omitempty"`
}

func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	var body createBody
	// Allow a bare spec OR {id, spec}. Peek by trying the wrapper first.
	if !decodeJSON(w, r, &body) {
		return
	}
	id := body.ID
	if id == "" {
		id = core.NewID()
	}
	if !core.ValidID(id) {
		writeErr(w, http.StatusBadRequest, "invalid id")
		return
	}
	box, err := s.reg.Create(r.Context(), id, body.Spec)
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	box.InitEgressVersion(body.EgressVersion)
	writeJSON(w, http.StatusCreated, box)
}

// sandboxOut is a record plus the state its backend reports at read time,
// omitted when the backend cannot say.
type sandboxOut struct {
	*core.Sandbox
	State core.State `json:"state,omitempty"`
}

const (
	stateProbeTimeout = 2 * time.Second // a probe that takes longer is unknown
	stateProbeLimit   = 8               // list probes at most this many sandboxes at once
)

func stateOf(ctx context.Context, box *core.Sandbox) core.State {
	st, ok := box.Instance().(core.Stater)
	if !ok {
		return ""
	}
	ctx, cancel := context.WithTimeout(ctx, stateProbeTimeout)
	defer cancel()
	return st.State(ctx)
}

func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	boxes := s.reg.List()
	items := make([]sandboxOut, len(boxes))
	slots := make(chan struct{}, stateProbeLimit)
	var wg sync.WaitGroup
	for i, box := range boxes {
		wg.Add(1)
		slots <- struct{}{}
		go func() {
			defer func() { <-slots; wg.Done() }()
			items[i] = sandboxOut{Sandbox: box, State: stateOf(r.Context(), box)}
		}()
	}
	wg.Wait()
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, sandboxOut{Sandbox: box, State: stateOf(r.Context(), box)})
}

func (s *Server) destroy(w http.ResponseWriter, r *http.Request) {
	if err := s.reg.Destroy(r.Context(), r.PathValue("id")); err != nil {
		writeCoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"destroyed": r.PathValue("id")})
}

// start runs a stopped sandbox again and answers its record with the state it
// now has. A backend that reports no state never says stopped, so it is never asked: 400.
func (s *Server) start(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	st, ok := box.Instance().(core.Stater)
	if !ok {
		writeCoreErr(w, fmt.Errorf("%w: backend %q cannot start a sandbox", core.ErrBadSpec, box.Backend))
		return
	}
	if err := st.Start(r.Context()); err != nil {
		writeCoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, sandboxOut{Sandbox: box, State: stateOf(r.Context(), box)})
}

func (s *Server) exec(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	var req core.ExecRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	res, err := box.Instance().Exec(r.Context(), req)
	if err != nil {
		// A bad exec user (core.ExecRequest.UID's ErrBadSpec) is the caller's mistake, not the
		// backend's - writeCoreErr maps it to 400, same as every other bad-spec rejection,
		// instead of the blanket 502 a real backend failure gets.
		writeCoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// remove takes back what the system gave a sandbox: every path must be
// below core.SystemRoot, checked here before the backend is asked, and the backend removes them as
// root. It runs no command a caller names. A backend that cannot remove answers 400.
func (s *Server) remove(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	var req struct {
		Paths []string `json:"paths"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	for _, p := range req.Paths {
		if err := core.SystemPath(p); err != nil {
			writeCoreErr(w, err)
			return
		}
	}
	rm, ok := box.Instance().(core.Remover)
	if !ok {
		writeCoreErr(w, fmt.Errorf("%w: backend %q cannot remove system paths", core.ErrBadSpec, box.Backend))
		return
	}
	if err := rm.Remove(r.Context(), req.Paths); err != nil {
		writeCoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]int{"removed": len(req.Paths)})
}

func (s *Server) egress(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	// Version 0 (or absent) is an unversioned push: it always applies and keeps the recorded
	// version. Any other version must be newer than the last one applied, or it is a 409.
	var req struct {
		AllowDomains []string `json:"allow_domains"`
		Version      int64    `json:"version,omitempty"`
	}
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.AllowDomains == nil {
		req.AllowDomains = []string{}
	}
	applied, err := box.SetEgress(r.Context(), req.AllowDomains, req.Version)
	if errors.Is(err, core.ErrStale) {
		// The version sandbar holds rides as JSON: the driver adopts it to push
		// once past it. The message keeps its old wording, so a driver from before still reads it.
		writeJSON(w, http.StatusConflict, map[string]any{"error": map[string]any{
			"message": err.Error(), "code": "stale", "version": applied,
		}})
		return
	}
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "allow_domains": req.AllowDomains, "version": applied})
}

func (s *Server) writeFile(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	path := r.URL.Query().Get("path")
	if err := core.CleanPath(path); err != nil {
		writeCoreErr(w, err)
		return
	}
	mode := parseMode(r.URL.Query().Get("mode"), 0o644)
	owner := core.Owner(r.URL.Query().Get("owner"))
	switch owner {
	case "":
		owner = core.OwnerAgent
	case core.OwnerAgent, core.OwnerSystem:
	default:
		writeErr(w, http.StatusBadRequest, fmt.Sprintf("unknown owner %q (agent or system)", owner))
		return
	}
	if !s.writeSlot(w, r) {
		return
	}
	defer s.releaseWriteSlot(r)
	data, err := readAllLimit(r.Body, core.MaxFileBytes)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := box.Instance().WriteFile(r.Context(), path, data, mode, owner); err != nil {
		writePathErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"path": path, "bytes": len(data)})
}

func (s *Server) readFile(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	path := r.URL.Query().Get("path")
	if err := core.CleanPath(path); err != nil {
		writeCoreErr(w, err)
		return
	}
	data, err := box.Instance().ReadFile(r.Context(), path)
	if err != nil {
		writePathErr(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	_, _ = w.Write(data)
}

func (s *Server) listDir(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	dir := r.URL.Query().Get("path")
	if err := core.CleanPath(dir); err != nil {
		writeCoreErr(w, err)
		return
	}
	entries, err := box.Instance().ListDir(r.Context(), dir)
	if err != nil {
		writePathErr(w, err)
		return
	}
	if entries == nil {
		entries = []string{} // an empty directory is the documented array, never JSON null
	}
	writeJSON(w, http.StatusOK, map[string]any{"entries": entries})
}

func (s *Server) archive(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	paths := r.URL.Query()["path"]
	if len(paths) == 0 {
		writeErr(w, http.StatusBadRequest, "at least one path is required")
		return
	}
	w.Header().Set("Content-Type", "application/x-tar")
	sent := &countingWriter{w: w}
	if err := box.Instance().Archive(r.Context(), paths, sent); err != nil {
		s.log.Error("archive stream failed", "id", box.ID, "err", err)
		if sent.n == 0 {
			writeCoreErr(w, err) // nothing on the wire yet: an ordinary error response
			return
		}
		// The 200 and part of the tar may already be on the wire. Abort the connection so the
		// client reads a BROKEN body: a cut-short tar that ends cleanly is indistinguishable
		// from a small archive, and a checkpoint taken from it silently loses the rest.
		panic(http.ErrAbortHandler)
	}
}

// countingWriter tells the archive handler whether any byte has gone out.
type countingWriter struct {
	w io.Writer
	n int64
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	return n, err
}

func (s *Server) extract(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	if err := box.Instance().Extract(r.Context(), r.Body); err != nil {
		writeErr(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"extracted": box.ID})
}

func (s *Server) denials(w http.ResponseWriter, r *http.Request) {
	box, err := s.reg.Get(r.PathValue("id"))
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	items, err := box.Instance().Denials(r.Context())
	if err != nil {
		writeErr(w, http.StatusBadGateway, err.Error())
		return
	}
	if items == nil {
		items = []core.Denial{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"denials": items})
}

// ── helpers ───────────────────────────────────────────────────────────────────

// maxJSONBytes bounds a JSON request body (a spec, an exec, an allowlist), so a runaway body
// cannot exhaust the daemon's memory. An exec's command rides in it, hence room for a long one.
const maxJSONBytes = 16 << 20

// decodeJSON reads a bounded JSON body into v, answering 400 itself when it cannot.
func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxJSONBytes)).Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "bad json: "+err.Error())
		return false
	}
	return true
}

// A file write holds its whole body in memory (up to core.MaxFileBytes), so at most maxLargeWrites
// writes larger than largeWrite (or of unknown size) are read at once; the rest wait their turn.
const (
	largeWrite     = 8 << 20
	maxLargeWrites = 4
)

func isLargeWrite(r *http.Request) bool {
	return r.ContentLength < 0 || r.ContentLength > largeWrite
}

// writeSlot takes a large write's slot, waiting while every slot is held; false (and nothing
// written) when the caller left first.
func (s *Server) writeSlot(w http.ResponseWriter, r *http.Request) bool {
	if !isLargeWrite(r) {
		return true
	}
	select {
	case s.largeWrites <- struct{}{}:
		return true
	case <-r.Context().Done():
		return false
	}
}

func (s *Server) releaseWriteSlot(r *http.Request) {
	if isLargeWrite(r) {
		<-s.largeWrites
	}
}

func parseMode(s string, def fs.FileMode) fs.FileMode {
	if s == "" {
		return def
	}
	n, err := strconv.ParseUint(s, 8, 32)
	if err != nil {
		return def
	}
	return fs.FileMode(n) & fs.ModePerm
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]any{"error": map[string]string{"message": msg}})
}

// writeCodedErr is an error a client branches on: the nested code says which 404 this is.
func writeCodedErr(w http.ResponseWriter, status int, msg, code string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"message": msg, "code": code}})
}

// writePathErr maps a file or directory read's failure: a path the sandbox does not hold is the
// caller's 404 no_such_path or 403 forbidden_path; anything else is the backend's 502, as before.
func writePathErr(w http.ResponseWriter, err error) {
	if errors.Is(err, core.ErrNoSuchPath) || errors.Is(err, core.ErrForbiddenPath) {
		writeCoreErr(w, err)
		return
	}
	writeErr(w, http.StatusBadGateway, err.Error())
}

func writeCoreErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, core.ErrNotFound):
		// A gone sandbox and a missing path are both 404s; the code tells them apart.
		writeCodedErr(w, http.StatusNotFound, err.Error(), "not_found")
	case errors.Is(err, core.ErrNoSuchPath):
		writeCodedErr(w, http.StatusNotFound, err.Error(), "no_such_path")
	case errors.Is(err, core.ErrForbiddenPath):
		writeCodedErr(w, http.StatusForbidden, err.Error(), "forbidden_path")
	case errors.Is(err, core.ErrBadSpec):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, core.ErrBackend):
		writeErr(w, http.StatusServiceUnavailable, err.Error())
	case errors.Is(err, core.ErrStale):
		writeErr(w, http.StatusConflict, err.Error())
	default:
		writeErr(w, http.StatusBadGateway, err.Error())
	}
}
