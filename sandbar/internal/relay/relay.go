// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package relay is a sandbox network's one door out (ADR-063). Each sandbox with
// domains lives on an INTERNAL Docker network of its own - no gateway, no route anywhere - and this
// pipe, the only other thing on it (sandbard joins it to every such network), forwards TCP to the
// egress proxy outside them. It parses
// nothing: authentication (the per-sandbox token) and the allowlist stay the proxy's. And it
// reaches exactly one target, so a sandbox that finds it reaches the proxy and nothing else -
// not sandbard's API, not the Docker socket behind it.
package relay

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"strconv"
	"sync"
	"time"
)

// Relay pipes every accepted connection to To.
type Relay struct {
	To         string        // host:port of the egress proxy
	MaxConns   int           // concurrent pipes in all; past it a new connection is closed at once
	MaxPerPeer int           // concurrent pipes from one source IP (one sandbox); <= 0 means MaxConns
	DialTO     time.Duration // how long a dial to To may take
	DrainTO    time.Duration // after the proxy finishes, how long a silent client may keep the pipe
	Log        *slog.Logger

	peer func(net.Conn) string // the peer a connection counts against; nil = its source IP (tests swap it)
}

// logEvery is how often one peer may put the same warning in the log: a sandbox provoking refusals
// in a loop would otherwise write a line per connection and fill the host's disk.
const logEvery = 10 * time.Second

// FromEnv reads SANDBAR_RELAY_TO (required, host:port), SANDBAR_RELAY_ADDR (default 0.0.0.0:7080),
// SANDBAR_RELAY_MAX_CONNS (default 4096) and SANDBAR_RELAY_MAX_PER_PEER (default 64: one sandbox
// holds at most 1/64th of the pipes - a Chromium opens at most 32 to one proxy - so filling the
// relay takes 64 sandboxes each keeping 64 tunnels busy; the proxy closes an idle one). A dial to the
// proxy gets 10 s (DialTO), and once the proxy is done a silent client keeps its pipe 10 s more
// (DrainTO). It returns the relay and the address to listen on.
func FromEnv(getenv func(string) string) (*Relay, string, error) {
	to := getenv("SANDBAR_RELAY_TO")
	if to == "" {
		return nil, "", errors.New("SANDBAR_RELAY_TO is required (the egress proxy, e.g. sandbard:7080)")
	}
	// A target with no port boots fine and then fails every dial: refuse it here instead.
	if host, port, err := net.SplitHostPort(to); err != nil || host == "" || port == "" {
		return nil, "", fmt.Errorf("SANDBAR_RELAY_TO must be host:port (e.g. sandbard:7080), got %q", to)
	}
	addr := getenv("SANDBAR_RELAY_ADDR")
	if addr == "" {
		addr = "0.0.0.0:7080"
	}
	maxConns, err := positive(getenv, "SANDBAR_RELAY_MAX_CONNS", 4096)
	if err != nil {
		return nil, "", err
	}
	perPeer, err := positive(getenv, "SANDBAR_RELAY_MAX_PER_PEER", 64)
	if err != nil {
		return nil, "", err
	}
	return &Relay{To: to, MaxConns: maxConns, MaxPerPeer: perPeer, DialTO: 10 * time.Second, DrainTO: 10 * time.Second}, addr, nil
}

func positive(getenv func(string) string, key string, def int) (int, error) {
	raw := getenv(key)
	if raw == "" {
		return def, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 {
		return 0, fmt.Errorf("%s must be a positive integer, got %q", key, raw)
	}
	return n, nil
}

// Serve accepts on ln until ctx ends (then it closes ln and returns nil). Open pipes are left to
// finish on their own; the process exiting ends them.
func (r *Relay) Serve(ctx context.Context, ln net.Listener) error {
	go func() {
		<-ctx.Done()
		ln.Close()
	}()
	peerOf := r.peer
	if peerOf == nil {
		peerOf = peerIP
	}
	g := newGate(r.MaxConns, r.MaxPerPeer)
	refusals, dials := newHush(logEvery), newHush(logEvery)
	for {
		c, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			r.Log.Warn("relay accept failed", "err", err)
			time.Sleep(50 * time.Millisecond) // e.g. out of file descriptors: back off, never spin
			continue
		}
		from := peerOf(c)
		if why := g.admit(from); why != "" {
			if ok, held := refusals.speak(from); ok {
				r.Log.Warn("relay refused a connection", "from", from, "why", why,
					"max_conns", g.max, "max_per_peer", g.perPeer, "suppressed", held)
			}
			c.Close()
			continue
		}
		go func() {
			defer g.release(from)
			r.pipe(ctx, c, from, dials)
		}()
	}
}

func (r *Relay) pipe(ctx context.Context, in net.Conn, from string, dials *hush) {
	defer in.Close()
	d := net.Dialer{Timeout: r.DialTO}
	out, err := d.DialContext(ctx, "tcp", r.To)
	if err != nil {
		if ok, held := dials.speak(from); ok {
			r.Log.Warn("relay dial failed", "from", from, "to", r.To, "err", err, "suppressed", held)
		}
		return
	}
	defer out.Close()
	clientDone, proxyDone := make(chan struct{}), make(chan struct{})
	go func() { _, _ = io.Copy(out, in); closeWrite(out); close(clientDone) }()
	go func() { _, _ = io.Copy(in, out); closeWrite(in); close(proxyDone) }()
	select {
	case <-clientDone:
		// The client half-closed ("that is all", e.g. an upload): it waits as long as the proxy
		// takes to answer. The proxy owns its own timeouts.
		<-proxyDone
	case <-proxyDone:
		// The proxy is done sending. A sandbox that never sends again and never closes would
		// otherwise hold this slot forever, and the cap is shared by every sandbox: it gets DrainTO
		// to finish, then its read fails and the pipe ends. The write toward a proxy that stopped
		// reading gets the same deadline, or a client still sending parks the copy there instead.
		grace := time.Now().Add(r.DrainTO)
		_ = in.SetReadDeadline(grace)
		_ = out.SetWriteDeadline(grace)
		<-clientDone
	}
}

// closeWrite passes a half-close on, so a client that says "that is all" is heard as such.
func closeWrite(c net.Conn) {
	if tc, ok := c.(*net.TCPConn); ok {
		_ = tc.CloseWrite()
	}
}

// peerIP is a connection's source address without its port: each sandbox has its own, on its own
// network, with no NAT in between, so it names the sandbox for the per-peer cap and the log.
func peerIP(c net.Conn) string {
	addr := c.RemoteAddr().String()
	if host, _, err := net.SplitHostPort(addr); err == nil {
		return host
	}
	return addr
}

// gate counts open pipes, in all and per peer. One sandbox can hold at most perPeer of the max, so
// no single sandbox - its own valid token is enough to open pipes - takes every slot from the rest
// (ADR-063).
type gate struct {
	mu      sync.Mutex
	max     int
	perPeer int
	open    int
	byPeer  map[string]int
}

func newGate(max, perPeer int) *gate {
	if perPeer <= 0 {
		perPeer = max
	}
	return &gate{max: max, perPeer: perPeer, byPeer: map[string]int{}}
}

// admit takes a slot for peer, or says why it cannot.
func (g *gate) admit(peer string) string {
	g.mu.Lock()
	defer g.mu.Unlock()
	switch {
	case g.byPeer[peer] >= g.perPeer:
		return "peer at its cap"
	case g.open >= g.max:
		return "relay full"
	}
	g.open++
	g.byPeer[peer]++
	return ""
}

func (g *gate) release(peer string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.open--
	g.byPeer[peer]--
	if g.byPeer[peer] <= 0 {
		delete(g.byPeer, peer)
	}
}

// hush lets one line per peer through per window and counts the rest; the next line that speaks
// carries the count.
type hush struct {
	every time.Duration
	now   func() time.Time
	mu    sync.Mutex
	peers map[string]*hushed
}

type hushed struct {
	last time.Time
	held int
}

// hushPeers bounds the map: past it, peers quiet for a whole window are forgotten (a count they
// held back goes with them - it is a log, not a ledger).
const hushPeers = 4096

func newHush(every time.Duration) *hush {
	return &hush{every: every, now: time.Now, peers: map[string]*hushed{}}
}

// speak reports whether peer may log now, and how many lines it held back since its last one.
func (h *hush) speak(peer string) (bool, int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	now := h.now()
	p, ok := h.peers[peer]
	if !ok {
		if len(h.peers) >= hushPeers {
			for k, q := range h.peers {
				if now.Sub(q.last) >= h.every {
					delete(h.peers, k)
				}
			}
		}
		h.peers[peer] = &hushed{last: now}
		return true, 0
	}
	if now.Sub(p.last) < h.every {
		p.held++
		return false, 0
	}
	held := p.held
	p.last, p.held = now, 0
	return true, held
}
