// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package firecracker

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sync"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// Backend implements core.Backend with one Firecracker microVM per sandbox.
type Backend struct {
	cfg Config
	log *slog.Logger
}

// New validates the host can run Firecracker and returns the backend. It refuses on a
// host without /dev/kvm or without a kernel - a firecracker backend that silently
// degrades to "no isolation" is the exact failure this project exists to prevent.
func New(cfg Config, log *slog.Logger) (*Backend, error) {
	if _, err := os.Stat("/dev/kvm"); err != nil {
		return nil, fmt.Errorf("%w: /dev/kvm not available (Firecracker needs a Linux/KVM host): %v",
			core.ErrBackend, err)
	}
	if cfg.KernelPath == "" {
		return nil, fmt.Errorf("%w: SANDBAR_FC_KERNEL is required", core.ErrBackend)
	}
	if _, err := os.Stat(cfg.KernelPath); err != nil {
		return nil, fmt.Errorf("%w: kernel %q: %v", core.ErrBackend, cfg.KernelPath, err)
	}
	if _, err := exec.LookPath(cfg.BinPath); err != nil {
		return nil, fmt.Errorf("%w: firecracker binary %q not found: %v", core.ErrBackend, cfg.BinPath, err)
	}
	if err := os.MkdirAll(cfg.RunDir, 0o700); err != nil {
		return nil, fmt.Errorf("%w: run dir: %v", core.ErrBackend, err)
	}
	return &Backend{cfg: cfg, log: log}, nil
}

func (b *Backend) Name() string { return "firecracker" }

// Create boots one microVM: copy the rootfs template, write the FC machine config,
// launch firecracker, and wait for guestd to answer on vsock.
func (b *Backend) Create(ctx context.Context, id string, spec core.Spec) (core.Instance, error) {
	// Egress is not yet wired for firecracker: a v1 microVM has no NIC, so there is nothing to
	// enforce an allowlist ON. Refusing is the honest answer - silently running airtight would
	// fail a task that needs the network for a reason no log explains. The design (a virtio-net
	// device + a host tap routed only to the shared allowlist proxy) is in docs/firecracker.md;
	// until it lands, ask for egress on the docker backend, or drop allow_domains.
	if len(spec.AllowDomains) > 0 {
		return nil, fmt.Errorf("%w: firecracker egress is not implemented yet (a v1 microVM has "+
			"no NIC); use the docker backend for egress or omit allow_domains - see "+
			"docs/firecracker.md", core.ErrBadSpec)
	}

	if !imageName.MatchString(spec.Image) {
		return nil, fmt.Errorf("%w: image %q must name a rootfs template (letters a-z, digits, '.', '_', '-')",
			core.ErrBadSpec, spec.Image)
	}

	dir := filepath.Join(b.cfg.RunDir, id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("%w: sandbox dir: %v", core.ErrBackend, err)
	}
	cleanup := func() { _ = os.RemoveAll(dir) }

	rootfs := filepath.Join(dir, "rootfs.ext4")
	template := filepath.Join(b.cfg.RootfsDir, spec.Image+".ext4")
	if err := copyFile(template, rootfs); err != nil {
		cleanup()
		return nil, fmt.Errorf("%w: rootfs copy (image %q): %v", core.ErrBadSpec, spec.Image, err)
	}

	apiSock := filepath.Join(dir, "firecracker.sock")
	vsockUDS := filepath.Join(dir, "vsock.sock")

	proc, err := b.launch(ctx, id, dir, apiSock)
	if err != nil {
		cleanup()
		return nil, err
	}
	inst := &instance{
		b: b, id: id, dir: dir, proc: proc,
		apiSock: apiSock, vsockUDS: vsockUDS,
	}
	if err := inst.configure(ctx, spec, rootfs, vsockUDS); err != nil {
		_ = inst.Destroy(context.WithoutCancel(ctx))
		return nil, err
	}
	// Wait for the guest agent, not a fixed sleep - the honest readiness signal.
	timeout := time.Duration(b.cfg.BootTimeoutS * float64(time.Second))
	if err := waitForGuest(ctx, vsockUDS, guestPort, timeout); err != nil {
		_ = inst.Destroy(context.WithoutCancel(ctx))
		return nil, fmt.Errorf("%w: %v", core.ErrBackend, err)
	}
	return inst, nil
}

// imageName is what a spec's image may be here: a rootfs template's file name, never a path.
var imageName = regexp.MustCompile(`^[a-z0-9._-]+$`)

// launch starts the firecracker process bound to its API unix socket. Firecracker
// creates the socket, so we spawn then poll for it.
func (b *Backend) launch(ctx context.Context, id, dir, apiSock string) (*os.Process, error) {
	logFile, err := os.Create(filepath.Join(dir, "firecracker.log"))
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(b.cfg.BinPath, "--api-sock", apiSock, "--id", id)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	// Detach from our process group so a daemon restart does not SIGINT running VMs
	// mid-flight; Destroy kills them explicitly.
	cmd.SysProcAttr = detachAttr()
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return nil, fmt.Errorf("%w: launch firecracker: %v", core.ErrBackend, err)
	}
	go func() { _ = cmd.Wait(); logFile.Close() }() // reap, don't block

	// Poll for the API socket (firecracker creates it a few ms after start).
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(apiSock); err == nil {
			return cmd.Process, nil
		}
		time.Sleep(5 * time.Millisecond)
	}
	_ = cmd.Process.Kill()
	return nil, fmt.Errorf("%w: firecracker API socket never appeared", core.ErrBackend)
}

// instance is one running microVM.
type instance struct {
	b        *Backend
	id       string
	dir      string
	proc     *os.Process
	apiSock  string
	vsockUDS string

	mu     sync.Mutex            // guards closed and conns, never held across a call
	closed bool                  // set by Destroy; no call starts after it
	conns  map[net.Conn]struct{} // the calls in flight, which Destroy closes
}

// configure PUTs the machine config to Firecracker's API and starts the guest.
func (i *instance) configure(ctx context.Context, spec core.Spec, rootfs, vsockUDS string) error {
	put := func(path string, body any) error { return i.fcAPI(ctx, http.MethodPut, path, body) }

	if err := put("/boot-source", map[string]any{
		"kernel_image_path": i.b.cfg.KernelPath,
		"boot_args":         i.b.cfg.KernelArgs,
	}); err != nil {
		return err
	}
	if err := put("/drives/rootfs", map[string]any{
		"drive_id": "rootfs", "path_on_host": rootfs,
		"is_root_device": true, "is_read_only": false,
	}); err != nil {
		return err
	}
	if err := put("/machine-config", map[string]any{
		"vcpu_count": spec.CPUs, "mem_size_mib": spec.MemMB,
	}); err != nil {
		return err
	}
	// vsock: guest_cid 3 is the conventional first guest; uds_path is the HOST socket
	// we dial with the CONNECT handshake.
	if err := put("/vsock", map[string]any{
		"guest_cid": 3, "uds_path": vsockUDS,
	}); err != nil {
		return err
	}
	// NO network device is configured: the VM has no NIC, so there is no route out at
	// all. Egress, when it lands, is an explicit device + host proxy - never a default.
	return put("/actions", map[string]any{"action_type": "InstanceStart"})
}

// ── core.Instance via guestd ────────────────────────────────────────────────────

func (i *instance) Exec(ctx context.Context, req core.ExecRequest) (core.ExecResult, error) {
	if _, err := req.UID(); err != nil {
		return core.ExecResult{}, err
	}
	resp, _, err := i.call(ctx, guestwire.Request{
		Op: guestwire.OpExec, Command: req.Command, TimeoutS: req.TimeoutS,
		User: req.User, Env: req.Env,
	}, nil)
	if err != nil {
		return core.ExecResult{}, err
	}
	return core.ExecResult{
		ExitCode: resp.ExitCode, Stdout: resp.Stdout, Stderr: resp.Stderr,
		DurationMS: resp.DurationMS, Truncated: resp.Truncated,
	}, nil
}

func (i *instance) WriteFile(ctx context.Context, p string, data []byte, mode fs.FileMode, owner core.Owner) error {
	if err := core.CleanPath(p); err != nil {
		return err
	}
	uid := core.AgentUID
	if owner == core.OwnerSystem {
		uid = 0
	}
	_, _, err := i.call(ctx, guestwire.Request{
		Op: guestwire.OpWriteFile, Path: p, Mode: mode.Perm(), UID: uid, BodyLen: int64(len(data)),
	}, data)
	return err
}

func (i *instance) ReadFile(ctx context.Context, p string) ([]byte, error) {
	if err := core.CleanPath(p); err != nil {
		return nil, err
	}
	// Every read is the agent's: guestd reads as that uid, so the guest's permissions bound it.
	_, body, err := i.call(ctx, guestwire.Request{Op: guestwire.OpReadFile, Path: p, UID: core.AgentUID}, nil)
	return body, err
}

func (i *instance) ListDir(ctx context.Context, p string) ([]string, error) {
	resp, _, err := i.call(ctx, guestwire.Request{Op: guestwire.OpListDir, Path: p, UID: core.AgentUID}, nil)
	if err != nil {
		return nil, err
	}
	return resp.Entries, nil
}

func (i *instance) Archive(ctx context.Context, paths []string, w io.Writer) error {
	return i.callStream(ctx, guestwire.Request{Op: guestwire.OpArchive, Paths: paths}, nil, w)
}

func (i *instance) Extract(ctx context.Context, r io.Reader) error {
	// The tar length is unknown ahead of time; buffer to a temp file to length-prefix.
	tmp, err := os.CreateTemp(i.dir, "extract-*.tar")
	if err != nil {
		return err
	}
	defer func() { tmp.Close(); os.Remove(tmp.Name()) }()
	n, err := io.Copy(tmp, r)
	if err != nil {
		return err
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		return err
	}
	_, _, err = i.call(ctx, guestwire.Request{Op: guestwire.OpExtract, BodyLen: n}, streamBody{tmp, n})
	return err
}

// Denials is empty for now: a v1 microVM has no NIC, so nothing can be refused. When egress
// lands (a virtio-net device + the shared allowlist proxy - docs/firecracker.md) this returns the
// proxy's refusals for this VM's principal, exactly like the docker backend.
func (i *instance) Denials(_ context.Context) ([]core.Denial, error) { return nil, nil }

// Egress is not yet wired for firecracker: a v1 microVM has no NIC, so there is nothing to
// replace an allowlist ON. Same refusal Create gives a spec that asks for egress up front.
func (i *instance) Egress(context.Context, []string) error {
	return fmt.Errorf("%w: firecracker egress is not implemented yet (a v1 microVM has "+
		"no NIC); use the docker backend for egress - see docs/firecracker.md", core.ErrBadSpec)
}

// Attach is not possible on firecracker yet: guestd answers one framed request at a time and has
// no streaming exec, so there is nothing to pipe the screen's bridge through. The display route
// answers this refusal as a 400 with its reason - the same posture as egress (docs/firecracker.md).
func (i *instance) Attach(context.Context, core.ExecRequest) (io.ReadWriteCloser, error) {
	return nil, fmt.Errorf("%w: firecracker has no streaming exec yet, so it has no display route; "+
		"use the docker backend for the screen - see docs/firecracker.md", core.ErrBadSpec)
}

var _ core.Attacher = (*instance)(nil)

func (i *instance) Destroy(ctx context.Context) error {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.closed {
		return nil
	}
	i.closed = true
	for conn := range i.conns { // a call in flight ends now, not when its command does
		conn.Close()
	}
	if i.proc != nil {
		_ = i.proc.Kill() // firecracker is a single process; killing it frees the VM
	}
	return os.RemoveAll(i.dir)
}

// fcAPI talks to Firecracker's own REST API over its unix socket.
func (i *instance) fcAPI(ctx context.Context, method, path string, body any) error {
	client := &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", i.apiSock)
		},
	}}
	var rdr io.Reader
	if body != nil {
		buf, _ := json.Marshal(body)
		rdr = bytesReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://fc"+path, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("firecracker api %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("firecracker api %s %s: %d %s", method, path, resp.StatusCode, msg)
	}
	return nil
}
