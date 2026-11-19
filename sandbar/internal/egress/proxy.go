// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package egress is sandbar's controlled way out: a deny-by-default HTTP/HTTPS forward proxy
// that lets a sandbox reach ONLY the domains its spec allowed, and records every refusal.
//
// The whole security argument in one paragraph: a sandbox has no route to the internet of its
// own (docker: network=none until egress is granted; firecracker: no NIC). When egress is
// granted, the sandbox's ONLY reachable next hop is this proxy, and the proxy answers yes or no
// per request against a per-sandbox allowlist. So "what can this untrusted code reach" is a
// decision made here, in ~200 lines of stdlib, not a firewall rule nobody can read.
//
// Two request shapes, both handled:
//   - CONNECT host:443      (HTTPS) - checked, then a blind byte tunnel (the proxy never sees
//     the TLS plaintext, which is the point: it authorises the destination, it does not inspect).
//   - GET http://host/path  (plain HTTP absolute-URI) - checked, then forwarded.
//
// Allowlists are keyed by a per-sandbox PRINCIPAL, registered by the backend when it creates a
// sandbox and revoked when it destroys one - a push model, so
// the hot path is a map lookup, never a database call. The principal is a per-sandbox token the
// backend hands the workload as its proxy credential (`http://sb:<token>@proxy` in HTTPS_PROXY),
// so it identifies the sandbox regardless of what source IP the network stack presents -
// portable across Docker Desktop, a Linux bridge, and a firecracker tap alike. When no credential
// is presented the source IP is the fallback key, which is enough for a single-tenant host.
package egress

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Denial is one refused request, kept so a caller can surface "why did the fetch fail" instead
// of leaving it a silent timeout.
type Denial struct {
	Host   string    `json:"host"`
	Method string    `json:"method"`
	Reason string    `json:"reason,omitempty"`
	At     time.Time `json:"at"`
}

// Proxy is a forward proxy with per-principal allowlists. Safe for concurrent use.
type Proxy struct {
	log      *slog.Logger
	dialTO   time.Duration
	maxDenip int // cap on remembered denials per principal, so a loop cannot grow memory unbounded

	// AllowLoopback lets a resolved loopback address through vettedDial. Test-only: it exists so
	// the suite can tunnel to its own 127.0.0.1 httptest origins. Production never sets it - a
	// sandbox reaching this machine's loopback is exactly the SSRF this proxy exists to refuse.
	AllowLoopback bool

	// fwd is the plain-HTTP forward path's Transport, built once in New() with DialContext
	// pointed at vettedDial and reused for the proxy's whole life. A zero-value Transport built
	// per request (the original shape here) never expires its idle connections, so every plain
	// request leaked a pooled connection plus the two goroutines that pump it - a fresh Transport
	// per request defeats connection pooling AND leaks. One Transport, shared, fixes both: a
	// pooled connection was vetted when it was dialed, so reuse is exactly as safe as a fresh
	// vetted dial and reuses the work.
	fwd *http.Transport

	// idleTO is how long a kept-alive client connection may sit idle between requests (90 s). With
	// none, net/http waits forever, and behind the relay an idle keep-alive is a relay pipe held
	// forever. A var only so a test can shorten it.
	idleTO time.Duration

	// tunnelIdle is how long a CONNECT tunnel may carry no byte either way before it is closed
	// (DefaultTunnelIdle). A hijacked tunnel is outside the server's timeouts, and behind the relay
	// an open tunnel holds a relay pipe. A var only so a test can shorten it.
	tunnelIdle time.Duration

	// connectPorts are the ports a CONNECT tunnel may reach; nil means any. HTTPS and plain HTTP by
	// default, so an allowlisted name is not also a door to its SSH or database port.
	connectPorts map[string]bool

	mu      sync.RWMutex
	allow   map[string][]string // principal → allowed domains (empty list = deny everything)
	denials map[string][]Denial // known principal → recent refusals, dropped with its allowlist
	// stray holds the refusals of principals nobody registered (a wrong token, a source IP), at
	// most maxStray of them: such a principal is never revoked, and a client minting a fresh token
	// per request must not grow memory without bound.
	stray map[string][]Denial
}

// maxStray caps how many unregistered principals keep their refusals.
const maxStray = 256

// DefaultConnectPorts are the ports a CONNECT tunnel may reach unless the operator names others.
const DefaultConnectPorts = "443,80"

// DefaultTunnelIdle is a CONNECT tunnel's idle bound: long enough that a page's quiet websocket
// pings inside it, short enough that a parked tunnel gives its relay pipe back.
const DefaultTunnelIdle = 5 * time.Minute

// New returns a proxy. dialTimeout bounds each upstream connect.
func New(log *slog.Logger, dialTimeout time.Duration) *Proxy {
	if dialTimeout <= 0 {
		dialTimeout = 15 * time.Second
	}
	p := &Proxy{
		log:        log,
		dialTO:     dialTimeout,
		maxDenip:   100,
		idleTO:     90 * time.Second,
		tunnelIdle: DefaultTunnelIdle,
		allow:      make(map[string][]string),
		denials:    make(map[string][]Denial),
		stray:      make(map[string][]Denial),
	}
	_ = p.SetConnectPorts(DefaultConnectPorts)
	p.fwd = &http.Transport{
		DialContext:     func(ctx context.Context, _, addr string) (net.Conn, error) { return p.vettedDial(ctx, addr) },
		IdleConnTimeout: 90 * time.Second,
	}
	return p
}

// SetConnectPorts sets the ports a CONNECT tunnel may reach: a comma list of port numbers, "*" for
// any port, or "" for DefaultConnectPorts (SANDBAR_EGRESS_CONNECT_PORTS).
func (p *Proxy) SetConnectPorts(raw string) error {
	raw = strings.TrimSpace(raw)
	switch raw {
	case "":
		raw = DefaultConnectPorts
	}
	var ports map[string]bool // nil: any port
	if raw != "*" {
		ports = make(map[string]bool)
		for _, f := range strings.Split(raw, ",") {
			n, err := strconv.Atoi(strings.TrimSpace(f))
			if err != nil || n < 1 || n > 65535 {
				return fmt.Errorf("SANDBAR_EGRESS_CONNECT_PORTS must be a comma list of ports or \"*\", got %q", raw)
			}
			ports[strconv.Itoa(n)] = true
		}
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.connectPorts = ports
	return nil
}

// portAllowed reports whether a CONNECT tunnel may reach port.
func (p *Proxy) portAllowed(port string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.connectPorts == nil || p.connectPorts[port]
}

// Allow registers (or replaces) the domains a principal may reach. An empty or nil list is a
// real, meaningful state: the principal is known but permitted nothing - deny-by-default made
// explicit, distinct from an unknown principal (also denied, but unregistered). It forgets the
// principal's refusals: they were refusals of the list it replaces, and a host the new list permits
// must not still read as refused.
func (p *Proxy) Allow(principal string, domains []string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.allow[principal] = append([]string(nil), domains...)
	delete(p.denials, principal)
	delete(p.stray, principal)
}

// Revoke forgets a principal's allowlist and denials - called when its sandbox is destroyed.
func (p *Proxy) Revoke(principal string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.allow, principal)
	delete(p.denials, principal)
	delete(p.stray, principal)
}

// Denials returns a principal's recent refusals - what the backend journals as "this was blocked".
func (p *Proxy) Denials(principal string) []Denial {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if d, ok := p.denials[principal]; ok {
		return append([]Denial(nil), d...)
	}
	return append([]Denial(nil), p.stray[principal]...)
}

// permitted reports whether host is on the principal's allowlist. Dot-anchored suffix match: an
// allow of "example.com" permits "example.com" and "api.example.com" but NOT "evil-example.com"
// - the classic way an allowlist becomes decorative is a bare strings.HasSuffix.
func (p *Proxy) permitted(principal, host string) bool {
	host = strings.ToLower(strings.Split(host, ":")[0])
	p.mu.RLock()
	domains, known := p.allow[principal]
	p.mu.RUnlock()
	if !known {
		return false
	}
	for _, d := range domains {
		d = strings.ToLower(strings.TrimSpace(d))
		if d == "" {
			continue
		}
		if host == d || strings.HasSuffix(host, "."+d) {
			return true
		}
	}
	return false
}

// known reports whether principal has a registered allowlist at all - even an empty one ("known
// but permitted nothing" is distinct from "never registered", the same distinction Allow's own
// doc comment draws). It is what tells challenge apart from permitted: an unregistered fallback
// principal (no credential presented) gets ASKED for one; a registered principal (token or, for a
// sandbox with none, its source IP) that simply is not permitted for this host gets denied, same
// as always.
func (p *Proxy) known(principal string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	_, known := p.allow[principal]
	return known
}

// deny records a refusal. A registered principal keeps its last maxDenip; an unregistered one goes
// to stray, which forgets an arbitrary other unregistered principal once it holds maxStray.
func (p *Proxy) deny(principal, host, method, reason string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	book := p.denials
	if _, known := p.allow[principal]; !known {
		book = p.stray
		if _, held := book[principal]; !held && len(book) >= maxStray {
			for k := range book {
				delete(book, k)
				break
			}
		}
	}
	d := book[principal]
	if len(d) >= p.maxDenip {
		d = d[1:] // ring: keep the most recent, bounded
	}
	book[principal] = append(d, Denial{Host: host, Method: method, Reason: reason, At: time.Now().UTC()})
}

// notGlobal is the CIDR table for everything Go's net.IP.Is* methods do NOT already know about,
// but Python's `is_global`/`is_reserved` (schemas/egress.refuse_ip) refuses - enumerated
// explicitly so the two sides of this door refuse the same set.
// The Is* checks below already cover loopback/private (RFC 1918 + IPv6 ULA)/link-local/
// multicast/unspecified; this table is the rest:
//   - 100.64.0.0/10   RFC 6598 carrier-grade NAT - Tailscale meshes, several clouds' internals
//   - 0.0.0.0/8       "this network" (RFC 791)
//   - 192.0.0.0/24    IETF protocol assignments (RFC 6890)
//   - 198.18.0.0/15   RFC 2544 benchmarking
//   - 240.0.0.0/4     reserved for future use
//   - 64:ff9b::/96    the NAT64 well-known prefix - translates straight to an inside address
//   - 2002::/16       6to4 - the embedded IPv4 may itself be private
//   - 2001:db8::/32   documentation - never a real destination
//   - 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24   the IPv4 documentation ranges (RFC 5737)
//   - 64:ff9b:1::/48  local-use NAT64 (RFC 8215) - an inside address wherever it is deployed
//   - 2001::/23       IETF protocol assignments, Teredo among them (RFC 2928)
//   - 100::/64        discard-only (RFC 6666)
//
// testdata/refuse_ip.json is the table both doors are tested against.
var notGlobal = mustCIDRs(
	"100.64.0.0/10",
	"0.0.0.0/8",
	"192.0.0.0/24",
	"198.18.0.0/15",
	"240.0.0.0/4",
	"64:ff9b::/96",
	"2002::/16",
	"2001:db8::/32",
	"192.0.2.0/24",
	"198.51.100.0/24",
	"203.0.113.0/24",
	"64:ff9b:1::/48",
	"2001::/23",
	"100::/64",
)

func mustCIDRs(cidrs ...string) []*net.IPNet {
	out := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		_, n, err := net.ParseCIDR(c)
		if err != nil {
			panic("egress: bad CIDR literal " + c + ": " + err.Error())
		}
		out = append(out, n)
	}
	return out
}

// refuseIP is the proxy's half of schemas/egress.refuse_ip: nothing that is not globally
// routable may be dialed, whatever name resolved to it.
func refuseIP(ip net.IP) bool {
	if ip == nil || ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() {
		return true
	}
	for _, n := range notGlobal {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

var errPrivateAddress = errors.New("private address")

// vettedDial resolves hostport's host, refuses any answer that is not globally routable (a
// mixed record - one public, one private - refuses the whole name; that mix is the classic
// DNS-rebinding shape), and then dials each vetted literal IN TURN - never the name, so the TCP
// handshake cannot land somewhere a second lookup would have refused - so an unroutable first
// answer (say an AAAA this host cannot reach) does not fail a dial a later, equally-vetted
// answer would have completed.
func (p *Proxy) vettedDial(ctx context.Context, hostport string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(hostport)
	if err != nil {
		return nil, err
	}
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, fmt.Errorf("resolve %s: %w", host, err)
	}
	if len(addrs) == 0 {
		return nil, fmt.Errorf("resolve %s: no addresses returned", host)
	}
	for _, a := range addrs {
		if p.AllowLoopback && a.IP.IsLoopback() {
			continue
		}
		if refuseIP(a.IP) {
			return nil, errPrivateAddress
		}
	}
	dialer := &net.Dialer{Timeout: p.dialTO}
	var lastErr error
	for _, a := range addrs {
		conn, dialErr := dialer.DialContext(ctx, "tcp", net.JoinHostPort(a.IP.String(), port))
		if dialErr == nil {
			return conn, nil
		}
		lastErr = dialErr
	}
	return nil, lastErr
}

// principalOf keys a request: the proxy-auth token (the portable per-sandbox credential) if
// present, else the source IP (single-tenant-host fallback). Basic auth's password half is the
// token; the username is cosmetic (the sandbox id, for a readable proxy log).
func principalOf(r *http.Request) string {
	if h := r.Header.Get("Proxy-Authorization"); strings.HasPrefix(h, "Basic ") {
		if raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(h, "Basic ")); err == nil {
			if _, token, ok := strings.Cut(string(raw), ":"); ok && token != "" {
				return "tok:" + token
			}
		}
	}
	return "ip:" + clientIP(r.RemoteAddr)
}

// TokenKey is the allowlist key for a per-sandbox proxy token - what a backend passes to Allow /
// Denials / Revoke, and what `principalOf` reconstructs from the Proxy-Authorization header.
func TokenKey(token string) string { return "tok:" + token }

// ServeHTTP makes Proxy an http.Handler. CONNECT is hijacked and tunnelled; everything else is
// treated as an absolute-URI forward request.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	who := principalOf(r)
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r, who, r.Host)
		return
	}
	p.handleHTTP(w, r, who)
}

// challenge answers a request that carries no valid Proxy-Authorization (and whose IP fallback
// principal is unregistered) with 407 instead of a flat deny. This is the whole point: a browser
// image's sidecar never puts its proxy credential on argv (image_browser/sidecar/browser.py's
// `_proxy_arg`) - it answers CDP's `Fetch.authRequired` instead, and Chromium (like any
// browser-grade client) only starts that credential retry on a genuine 407 with
// `Proxy-Authenticate`. Any OTHER non-2xx (403 included) is a flat, final tunnel failure to
// Chromium, so a plain deny here would mean the CDP credential path never fires at all. Recorded
// as a denial the same way an outright refusal is, so "why did this fetch fail" still has an
// answer, even on the very first, credential-less attempt.
func (p *Proxy) challenge(w http.ResponseWriter, who, host, method string) {
	p.deny(who, host, method, "no credential")
	w.Header().Set("Proxy-Authenticate", `Basic realm="sandbar"`)
	w.Header().Set("Connection", "close")
	http.Error(w, "proxy authentication required", http.StatusProxyAuthRequired)
}

func (p *Proxy) handleConnect(w http.ResponseWriter, r *http.Request, who, hostport string) {
	host := hostOnly(hostport)
	if strings.HasPrefix(who, "ip:") && !p.known(who) {
		p.challenge(w, who, host, "CONNECT")
		return
	}
	if !p.permitted(who, host) {
		p.deny(who, host, "CONNECT", "not in allowlist")
		http.Error(w, "egress denied: "+host, http.StatusForbidden)
		return
	}
	target := ensurePort(hostport, "443")
	if _, port, _ := net.SplitHostPort(target); !p.portAllowed(port) {
		p.deny(who, host, "CONNECT", "port "+port+" not allowed")
		http.Error(w, "egress denied: "+host+":"+port, http.StatusForbidden)
		return
	}
	upstream, err := p.vettedDial(r.Context(), target)
	if err != nil {
		if errors.Is(err, errPrivateAddress) {
			p.deny(who, host, "CONNECT", "private address")
			http.Error(w, "egress denied: "+host, http.StatusForbidden)
			return
		}
		http.Error(w, "upstream dial failed", http.StatusBadGateway)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		upstream.Close()
		http.Error(w, "proxy cannot hijack", http.StatusInternalServerError)
		return
	}
	client, _, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}
	_, _ = client.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
	tunnel(client, upstream, p.tunnelIdle)
}

// tunnel is a blind, bidirectional byte pipe: the proxy authorised the destination; it does not
// see the TLS plaintext flowing across it. It ends when either side does, or once neither side has
// sent a byte for idle. Every read that carries bytes moves BOTH read deadlines on, so a quiet
// direction (a download's silent client) never cuts a busy one; and both ends are closed together,
// so a copy blocked writing to a peer that stopped reading ends with the tunnel.
func tunnel(client, upstream net.Conn, idle time.Duration) {
	alive := func() {
		at := time.Now().Add(idle)
		_ = client.SetReadDeadline(at)
		_ = upstream.SetReadDeadline(at)
	}
	alive()
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, active{client, alive}); done <- struct{}{} }()
	go func() { _, _ = io.Copy(client, active{upstream, alive}); done <- struct{}{} }()
	<-done
	_ = client.Close()
	_ = upstream.Close()
	<-done
}

// active is a connection whose every read that carries bytes calls on.
type active struct {
	net.Conn
	on func()
}

func (a active) Read(b []byte) (int, error) {
	n, err := a.Conn.Read(b)
	if n > 0 {
		a.on()
	}
	return n, err
}

func (p *Proxy) handleHTTP(w http.ResponseWriter, r *http.Request, who string) {
	host := hostOnly(r.Host)
	if strings.HasPrefix(who, "ip:") && !p.known(who) {
		p.challenge(w, who, host, r.Method)
		return
	}
	if !p.permitted(who, host) {
		p.deny(who, host, r.Method, "not in allowlist")
		http.Error(w, "egress denied: "+host, http.StatusForbidden)
		return
	}
	// Rebuild the outbound request without proxy-specific headers.
	outURL := r.URL
	if outURL.Host == "" { // some clients send origin-form after CONNECT; be defensive
		outURL.Scheme, outURL.Host = "http", r.Host
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	out, err := http.NewRequestWithContext(ctx, r.Method, outURL.String(), r.Body)
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	copyHeaders(out.Header, r.Header)
	removeHopHeaders(out.Header) // Proxy-Authorization among them: the credential never leaks upstream
	// p.fwd (built once in New()) dials through vettedDial - the same resolve-refuse-pin the
	// CONNECT path uses, so a plain-HTTP forward cannot land anywhere a CONNECT would have
	// refused - and pools connections instead of leaking one per request.
	resp, err := p.fwd.RoundTrip(out)
	if err != nil {
		if errors.Is(err, errPrivateAddress) {
			p.deny(who, host, r.Method, "private address")
			http.Error(w, "egress denied: "+host, http.StatusForbidden)
			return
		}
		http.Error(w, "upstream failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	copyHeaders(w.Header(), resp.Header)
	removeHopHeaders(w.Header())
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

// ── helpers ───────────────────────────────────────────────────────────────────

func clientIP(remoteAddr string) string {
	if host, _, err := net.SplitHostPort(remoteAddr); err == nil {
		return host
	}
	return remoteAddr
}

func hostOnly(hostport string) string {
	if h, _, err := net.SplitHostPort(hostport); err == nil {
		return h
	}
	return hostport
}

func ensurePort(hostport, def string) string {
	if _, _, err := net.SplitHostPort(hostport); err == nil {
		return hostport
	}
	return net.JoinHostPort(hostport, def)
}

func copyHeaders(dst, src http.Header) {
	for k, vs := range src {
		for _, v := range vs {
			dst.Add(k, v)
		}
	}
}

// hopHeaders describe one connection, not the message (RFC 9110 section 7.6.1), so a proxy never
// forwards them.
var hopHeaders = []string{
	"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization",
	"Te", "Trailer", "Transfer-Encoding", "Upgrade",
}

// removeHopHeaders drops the hop-by-hop headers and every header the Connection header names.
func removeHopHeaders(h http.Header) {
	for _, v := range h.Values("Connection") {
		for _, name := range strings.Split(v, ",") {
			if name = strings.TrimSpace(name); name != "" {
				h.Del(name)
			}
		}
	}
	for _, name := range hopHeaders {
		h.Del(name)
	}
}

// Serve runs the proxy on addr until the context is cancelled. Returns the bound address so a
// caller that asked for :0 learns the real port.
func (p *Proxy) Serve(ctx context.Context, addr string) (string, func() error, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return "", nil, fmt.Errorf("egress listen: %w", err)
	}
	// IdleTimeout ends an idle keep-alive; ReadHeaderTimeout ends a client that never finishes its
	// request. A CONNECT tunnel is hijacked and outside both: tunnelIdle ends a quiet one, and the
	// relay's per-peer cap (sandbard relay) bounds how many a busy sandbox holds.
	srv := &http.Server{Handler: p, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: p.idleTO}
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			p.log.Error("egress proxy stopped", "err", err)
		}
	}()
	return ln.Addr().String(), srv.Close, nil
}
