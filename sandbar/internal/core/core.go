// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package core defines sandbar's vocabulary: what a sandbox is, what every backend must
// provide, and the registry that owns running instances.
//
// The design rule: THIS package is the whole contract. A backend (docker, firecracker,
// whatever ships next) implements two small interfaces and nothing else; the API layer
// speaks only these types and never imports a backend. That is what keeps isolation
// technology a swappable detail - the same seam sandbar's consumers get one level up,
// where the HTTP API hides even Go.
package core

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"strings"
	"sync"
	"time"
)

// Owner says who a written file belongs to inside the sandbox. The distinction is
// load-bearing for agent platforms: "system" files are what the sandbox was GIVEN
// (sources, instructions - root-owned, typically read-only), "agent" files are its
// workshop. A sandbox where the workload can rewrite what it was given cannot support
// verification ("does this number appear in a source?" - always yes).
type Owner string

const (
	OwnerAgent  Owner = "agent"
	OwnerSystem Owner = "system"
)

// AgentUID is the uid workload files belong to, and the uid workloads run as.
// Backends bake the same number into their images/rootfs (useradd --uid 1000).
const AgentUID = 1000

// BrowserUID is the browser sidecar's uid inside a frame (image_browser/Dockerfile). The agent
// (AgentUID) may never read what it owns: cookies are tokens.
const BrowserUID = 1001

// SystemRoot is where a caller places what a sandbox is GIVEN (OwnerSystem: its sources and
// instructions, root's and read-only to the workload) - the frame layout, like AgentUID. It is the
// one place root may remove from (Remover): the system takes back what it gave, and nothing else.
const SystemRoot = "/task"

// SystemPath refuses a path root may not remove: anything but a clean, absolute path strictly below
// SystemRoot. SystemRoot itself stays (its mode and owner are the lock the workload cannot pass),
// and an escape (`/task/../etc`), a relative path or a sibling (`/tasks`) never reaches a backend.
func SystemPath(p string) error {
	if p != path.Clean(p) || !strings.HasPrefix(p, SystemRoot+"/") {
		return fmt.Errorf("%w: %q is not a clean path below %s", ErrBadSpec, p, SystemRoot)
	}
	return nil
}

// CleanPath refuses a path no file route may hand a backend: an empty or relative path, one holding a
// NUL byte, and any that is not already clean (`..`, `//`, a trailing slash), so an escape is
// never resolved by a backend and every path means one thing.
func CleanPath(p string) error {
	if !strings.HasPrefix(p, "/") || strings.ContainsRune(p, 0) || p != path.Clean(p) {
		return fmt.Errorf("%w: %q is not a clean absolute path", ErrBadSpec, p)
	}
	return nil
}

// Spec is everything a sandbox is born with. Backends may ignore fields they cannot
// honour yet, but MUST refuse (not silently drop) anything security-relevant they
// cannot enforce - see Backend.Create.
type Spec struct {
	// Backend names which isolation technology to use ("docker", "firecracker").
	// Empty means the daemon's default backend.
	Backend string `json:"backend,omitempty"`
	// Image is backend-specific: a Docker image reference, or a Firecracker rootfs
	// template name registered with the daemon.
	Image string `json:"image"`
	CPUs  int    `json:"cpus,omitempty"`   // default 2
	MemMB int    `json:"mem_mb,omitempty"` // default 2048
	// AllowDomains is the egress allowlist. v1 backends run with NO network at all
	// (deny-by-default is the only honest starting point); a backend that grows an
	// egress path must enforce this list, never just record it.
	AllowDomains []string          `json:"allow_domains,omitempty"`
	Env          map[string]string `json:"env,omitempty"`
	// Labels are the caller's ownership marks: opaque to sandbar,
	// returned by list and get, so a reconciler can tell whose sandbox is whose.
	Labels map[string]string `json:"labels,omitempty"`
}

// WithDefaults returns the spec with zero values filled in.
func (s Spec) WithDefaults() Spec {
	if s.CPUs <= 0 {
		s.CPUs = 2
	}
	if s.MemMB <= 0 {
		s.MemMB = 2048
	}
	return s
}

// Validate refuses what no backend should accept.
func (s Spec) Validate() error {
	if s.Image == "" {
		return fmt.Errorf("%w: spec.image is required", ErrBadSpec)
	}
	if s.CPUs < 0 || s.CPUs > 64 {
		return fmt.Errorf("%w: cpus %d out of range", ErrBadSpec, s.CPUs)
	}
	if s.MemMB < 0 || s.MemMB > 262144 {
		return fmt.Errorf("%w: mem_mb %d out of range", ErrBadSpec, s.MemMB)
	}
	if len(s.Labels) > 32 {
		return fmt.Errorf("%w: at most 32 labels", ErrBadSpec)
	}
	for k, v := range s.Labels {
		if k == "" || len(k) > 63 || len(v) > 200 {
			return fmt.Errorf("%w: label %q must be 1-63 chars with a value of at most 200", ErrBadSpec, k)
		}
	}
	return nil
}

// ExecRequest is one command run inside the sandbox, through its shell.
type ExecRequest struct {
	Command  string            `json:"command"`
	TimeoutS float64           `json:"timeout_s,omitempty"` // default 120
	User     string            `json:"user,omitempty"`      // "" or "agent" ⇒ AgentUID; "browser" ⇒ BrowserUID
	Env      map[string]string `json:"env,omitempty"`       // extra environment, visible only to that one exec
}

// UID resolves the request's user. Anything but agent or browser is a bad spec: the two uids
// are the only principals a frame has, and root is never one of them.
func (r ExecRequest) UID() (int, error) {
	switch r.User {
	case "", "agent":
		return AgentUID, nil
	case "browser":
		return BrowserUID, nil
	}
	return 0, fmt.Errorf("%w: unknown exec user %q", ErrBadSpec, r.User)
}

// ExecResult reports what happened. A non-zero exit code is a RESULT, not an error -
// errors are for "the sandbox could not run this at all". A command that outlives its timeout is
// killed and reports exit code 124 (the code `timeout` uses). Truncated says stdout or stderr
// reached MaxExecOutput and the rest was dropped.
type ExecResult struct {
	ExitCode   int     `json:"exit_code"`
	Stdout     string  `json:"stdout"`
	Stderr     string  `json:"stderr"`
	DurationMS float64 `json:"duration_ms"`
	Truncated  bool    `json:"truncated,omitempty"`
}

// ExitTimedOut is the exit code of a command killed at its timeout.
const ExitTimedOut = 124

// Denial is one egress request the sandbox's allowlist refused - surfaced so a caller can say
// "the fetch to X was blocked" instead of leaving it a silent timeout. Defined here (not in the
// egress package) so `core` stays the contract every backend speaks and never imports a backend
// concern.
type Denial struct {
	Host   string    `json:"host"`
	Method string    `json:"method"`
	Reason string    `json:"reason,omitempty"`
	At     time.Time `json:"at"`
}

// State is whether a sandbox's isolation unit runs: a caller that binds
// to a sandbox between uses must not take a stopped one for live.
type State string

const (
	StateRunning State = "running"
	StateStopped State = "stopped" // present, not running: its processes are gone, its files are not (Start)
	StateMissing State = "missing" // the record outlived its container
	StateUnknown State = "unknown" // the backend could not say; never read as stopped
)

// Stater is the optional half of the contract: a backend whose instances can stop under it
// reports their State, and starts a stopped one again with the files, environment and network it
// had - only its processes are new. One interface, so a backend that can say "stopped" can always
// undo it. One that cannot (firecracker today) reports none, and its records carry no state. Like
// Instance, an implementation must honour its context's cancellation (the API's 2 s probe cap
// relies on it).
type Stater interface {
	State(ctx context.Context) State
	// Start runs a stopped sandbox again; a running one is left as it is. ErrNotFound when what
	// isolated it is gone.
	Start(ctx context.Context) error
}

// Instance is one running sandbox. Implementations must be safe for concurrent use;
// every method takes a context and honours its cancellation.
type Instance interface {
	// Exec runs a command through the sandbox's shell and waits for it.
	Exec(ctx context.Context, req ExecRequest) (ExecResult, error)
	// WriteFile places bytes at an absolute path, creating parent directories.
	// owner=system ⇒ uid 0; owner=agent ⇒ uid AgentUID. Mode is applied exactly. An agent write is
	// made as AgentUID, so a path the sandbox's own permissions keep from it is ErrForbiddenPath; a
	// system write through a parent that is a symbolic link is ErrForbiddenPath too.
	WriteFile(ctx context.Context, path string, data []byte, mode fs.FileMode, owner Owner) error
	// ReadFile returns the exact bytes at path (binary-safe - no text decoding).
	ReadFile(ctx context.Context, path string) ([]byte, error)
	// ListDir returns entry names; directories carry a trailing "/".
	ListDir(ctx context.Context, path string) ([]string, error)
	// Archive streams a tar of the given absolute paths. Entry names are rooted at
	// each path's basename (tar of /work ⇒ "work/…"), so extracting at / restores.
	Archive(ctx context.Context, paths []string, w io.Writer) error
	// Extract applies a tar produced by Archive (or compatible) at /.
	Extract(ctx context.Context, r io.Reader) error
	// Denials reports what this sandbox's egress allowlist refused. Empty (never an error) for a
	// sandbox with no egress - a v1 microVM has no NIC, an airtight container no route out.
	Denials(ctx context.Context) ([]Denial, error)
	// Egress REPLACES the sandbox's egress allowlist while it runs (a consent widening it, a
	// step narrowing it). ErrBadSpec when the sandbox was created without egress - it has no
	// proxy principal to re-register - or the backend cannot do egress at all.
	Egress(ctx context.Context, domains []string) error
	// Destroy tears the sandbox down. Idempotent.
	Destroy(ctx context.Context) error
}

// Attacher is the optional half of the contract a backend implements when it can stream an exec
// (the display route). Attach starts req and returns a raw two-way stream:
// Write feeds the process's stdin, Read drains its stdout - demultiplexed, so the stream carries
// only the process's own bytes - and stderr is discarded. The stream ends (Read: EOF) when the
// process exits; Close ends the process's stdin, which ends a process that exits at EOF, as the
// screen bridge does (Docker cannot kill an exec, so one that ignores EOF runs on). No timeout:
// req.TimeoutS is Exec's, and a stream lasts as long as its caller holds it, or until ctx ends.
// A backend that cannot stream yet (firecracker) refuses with ErrBadSpec and a reason.
type Attacher interface {
	Attach(ctx context.Context, req ExecRequest) (io.ReadWriteCloser, error)
}

// DisplayBridge is the exec the display route attaches to: the browser sidecar's screen bridge
// (Plane's image_browser/sidecar/screen.py), run as the browser user. It opens no port - it pumps
// its stdin and stdout to the sidecar daemon's unix socket - and it never starts the daemon.
// `exec` makes the bridge the shell's own process, so closing its stdin ends it.
func DisplayBridge() ExecRequest {
	return ExecRequest{
		Command: "cd /home/browser && exec python3 -m sidecar.screen",
		User:    "browser",
		Env:     map[string]string{"PYTHONPATH": "/opt/plane/browser", "HOME": "/home/browser"},
	}
}

// Remover is the optional half of a backend that can take back what the system gave a sandbox:
// each path below SystemRoot (SystemPath) is removed with all it
// holds, as root - the owner of what it removes - and the directory it sat in keeps its mode and
// owner. A path that is not there is already removed. It is no exec: a caller names paths, never a
// command, and a backend without it (firecracker) answers 400.
type Remover interface {
	Remove(ctx context.Context, paths []string) error
}

// Adopter is the optional half of a backend whose sandboxes outlive its daemon: at boot it
// hands back every sandbox it made that is still there, so a restarted sandbard - every deploy
// restarts it - keeps a warm Machine instead of forgetting it. A backend that adopts keeps its
// sandboxes at the daemon's exit too (Registry.Shutdown).
type Adopter interface {
	Adopt(ctx context.Context) ([]Adopted, error)
}

// Adopted is one sandbox a backend found at boot.
type Adopted struct {
	ID        string
	Spec      Spec
	CreatedAt time.Time
	Instance  Instance
}

// Backend creates instances. Name is the registry key ("docker", "firecracker").
//
// Contract: Create either returns a fully usable Instance or an error - never a
// half-booted sandbox the caller must poll. If the spec asks for something the
// backend cannot ENFORCE (not merely cannot optimise), Create must refuse.
type Backend interface {
	Name() string
	Create(ctx context.Context, id string, spec Spec) (Instance, error)
}

// Sentinel errors the API layer maps to status codes. Backends wrap these.
var (
	ErrNotFound = errors.New("no such sandbox")
	// ErrNoSuchPath is a file or directory a sandbox that still exists does not hold: the
	// caller's answer ("not there"), never a backend failure and never a gone sandbox
	// (ErrNotFound). The API says 404 with code no_such_path, so a client tells the two apart
	// and a model's read of a path that is not there stays a tool result (RR-missing-path).
	ErrNoSuchPath = errors.New("no such path")
	ErrBadSpec    = errors.New("invalid spec")
	// ErrForbiddenPath is a path the workload's own identity may not touch: the caller's answer
	// ("not yours"), 403 with code forbidden_path, never a backend failure.
	ErrForbiddenPath = errors.New("forbidden path")
	ErrBackend       = errors.New("backend unavailable")
	// ErrStale is an egress update whose version is not newer than the one already applied.
	ErrStale = errors.New("stale")
)

// Sandbox is the registry's record of one instance.
type Sandbox struct {
	ID        string    `json:"id"`
	Backend   string    `json:"backend"`
	Spec      Spec      `json:"spec"`
	CreatedAt time.Time `json:"created_at"`

	inst   Instance
	egress *egressState // behind a pointer: the record is handed around by pointer, but a lock never copies
}

// egressState is the version of the allowlist last applied to one sandbox:
// a push whose version is not newer is refused, so a slow push can never overwrite a
// newer list. Serialised by its own lock, so the check and the apply are one step.
type egressState struct {
	mu      sync.Mutex
	version int64
}

// Instance returns the live instance behind the record.
func (s *Sandbox) Instance() Instance { return s.inst }

// InitEgressVersion records the version the creator's first allowlist carries (0 = unversioned).
func (s *Sandbox) InitEgressVersion(v int64) {
	s.egress.mu.Lock()
	defer s.egress.mu.Unlock()
	s.egress.version = v
}

// SetEgress replaces the allowlist unless `version` is stale. Version 0 is an unversioned push
// (a caller from before versions) and always applies, keeping the recorded version. It returns
// the version now recorded.
func (s *Sandbox) SetEgress(ctx context.Context, domains []string, version int64) (int64, error) {
	s.egress.mu.Lock()
	defer s.egress.mu.Unlock()
	if version != 0 && version <= s.egress.version {
		return s.egress.version, fmt.Errorf("%w: egress version %d is not newer than %d", ErrStale, version, s.egress.version)
	}
	if err := s.inst.Egress(ctx, domains); err != nil {
		return s.egress.version, err
	}
	if version != 0 {
		s.egress.version = version
	}
	return s.egress.version, nil
}

// Registry owns every running sandbox in the daemon. It is deliberately dumb: a map
// with a lock, persisting nothing. A sandbox outlives the daemon only where its backend can
// find it again (Adopter): at boot `Adopt` records those, and the caller's durable truth (its
// next versioned egress push, its reconciler) does the rest.
type Registry struct {
	mu       sync.RWMutex
	byID     map[string]*Sandbox
	backends map[string]Backend
	def      string // default backend name
}

func NewRegistry(def string, backends ...Backend) *Registry {
	m := make(map[string]Backend, len(backends))
	for _, b := range backends {
		m[b.Name()] = b
	}
	return &Registry{byID: make(map[string]*Sandbox), backends: m, def: def}
}

// Backends lists what this daemon can isolate with - /healthz reports it.
func (r *Registry) Backends() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.backends))
	for name := range r.backends {
		names = append(names, name)
	}
	return names
}

// Create validates, picks the backend, boots the instance, and records it.
func (r *Registry) Create(ctx context.Context, id string, spec Spec) (*Sandbox, error) {
	spec = spec.WithDefaults()
	if err := spec.Validate(); err != nil {
		return nil, err
	}
	name := spec.Backend
	if name == "" {
		name = r.def
	}
	r.mu.RLock()
	backend, ok := r.backends[name]
	_, exists := r.byID[id]
	r.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("%w: no backend %q (have %v)", ErrBadSpec, name, r.Backends())
	}
	if exists {
		return nil, fmt.Errorf("%w: sandbox %q already exists", ErrBadSpec, id)
	}

	inst, err := backend.Create(ctx, id, spec)
	if err != nil {
		return nil, err
	}
	box := &Sandbox{ID: id, Backend: name, Spec: spec, CreatedAt: time.Now().UTC(), inst: inst, egress: &egressState{}}

	r.mu.Lock()
	defer r.mu.Unlock()
	if _, raced := r.byID[id]; raced {
		// Two concurrent creates with the same id: the second boot loses and is torn down.
		_ = inst.Destroy(context.WithoutCancel(ctx))
		return nil, fmt.Errorf("%w: sandbox %q already exists", ErrBadSpec, id)
	}
	r.byID[id] = box
	return box, nil
}

// Get returns the record or ErrNotFound.
func (r *Registry) Get(id string) (*Sandbox, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	box, ok := r.byID[id]
	if !ok {
		return nil, ErrNotFound
	}
	return box, nil
}

// List returns every record, unordered.
func (r *Registry) List() []*Sandbox {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Sandbox, 0, len(r.byID))
	for _, box := range r.byID {
		out = append(out, box)
	}
	return out
}

// Destroy tears down and forgets. Destroying an unknown id is ErrNotFound; a failed
// teardown keeps the record (so the operator can retry) and returns the error.
func (r *Registry) Destroy(ctx context.Context, id string) error {
	box, err := r.Get(id)
	if err != nil {
		return err
	}
	if err := box.inst.Destroy(ctx); err != nil {
		return err
	}
	r.mu.Lock()
	delete(r.byID, id)
	r.mu.Unlock()
	return nil
}

// Adopt records every sandbox an Adopter backend still holds - the daemon's boot. An adopted
// sandbox's egress version starts at 0, so its caller's next versioned push applies. It returns
// how many it recorded.
func (r *Registry) Adopt(ctx context.Context) (int, error) {
	r.mu.RLock()
	backends := make([]Backend, 0, len(r.backends))
	for _, b := range r.backends {
		backends = append(backends, b)
	}
	r.mu.RUnlock()
	n := 0
	for _, b := range backends {
		a, ok := b.(Adopter)
		if !ok {
			continue
		}
		found, err := a.Adopt(ctx)
		if err != nil {
			return n, fmt.Errorf("adopt %s sandboxes: %w", b.Name(), err)
		}
		r.mu.Lock()
		for _, f := range found {
			if _, taken := r.byID[f.ID]; taken {
				continue
			}
			r.byID[f.ID] = &Sandbox{ID: f.ID, Backend: b.Name(), Spec: f.Spec, CreatedAt: f.CreatedAt, inst: f.Instance, egress: &egressState{}}
			n++
		}
		r.mu.Unlock()
	}
	return n, nil
}

// Shutdown is the daemon's exit. A sandbox whose backend adopts is kept for the next boot - a
// restart, every deploy's, must never cost a warm Machine its work; any other is torn down, since
// nothing would find it again.
func (r *Registry) Shutdown(ctx context.Context) {
	for _, box := range r.List() {
		r.mu.RLock()
		_, adopts := r.backends[box.Backend].(Adopter)
		r.mu.RUnlock()
		if !adopts {
			_ = r.Destroy(ctx, box.ID)
		}
	}
}
