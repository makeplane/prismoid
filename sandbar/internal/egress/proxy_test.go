// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package egress

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"testing"
	"time"
)

// serve runs p for one test. The suite's origins listen on random loopback ports, so every port
// may be tunnelled to here; TestConnectReachesOnlyTheAllowedPorts proves the default.
func serve(t *testing.T, p *Proxy) (*Proxy, string) {
	t.Helper()
	if err := p.SetConnectPorts("*"); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	addr, _, err := p.Serve(ctx, "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return p, addr
}

func newProxy(t *testing.T) (*Proxy, string) {
	t.Helper()
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	p.AllowLoopback = true // test-only escape: the shared suite tunnels to 127.0.0.1 origins
	return serve(t, p)
}

func TestPermittedIsDotAnchored(t *testing.T) {
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 0)
	key := TokenKey("t1")
	p.Allow(key, []string{"example.com"})

	allow := []string{"example.com", "api.example.com", "EXAMPLE.com", "a.b.example.com"}
	deny := []string{"evil-example.com", "example.com.evil.com", "notexample.com", "example.org"}
	for _, h := range allow {
		if !p.permitted(key, h) {
			t.Errorf("%q should be permitted", h)
		}
	}
	for _, h := range deny {
		if p.permitted(key, h) {
			t.Errorf("%q should be denied", h)
		}
	}
}

func TestDenyByDefault(t *testing.T) {
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 0)
	key := TokenKey("t1")
	// unknown principal → denied
	if p.permitted(key, "example.com") {
		t.Fatal("an unregistered principal must reach nothing")
	}
	// registered with an EMPTY list → still denied (deny-by-default made explicit)
	p.Allow(key, nil)
	if p.permitted(key, "example.com") {
		t.Fatal("an empty allowlist must permit nothing")
	}
	// revoke clears
	p.Allow(key, []string{"example.com"})
	p.Revoke(key)
	if p.permitted(key, "example.com") {
		t.Fatal("revoke must forget the allowlist")
	}
}

func TestPrincipalPrefersTokenOverSourceIP(t *testing.T) {
	// The credential path is what makes egress portable: the same token identifies a sandbox no
	// matter what source IP the network presents.
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 0)
	p.AllowLoopback = true // test-only escape: this origin is a 127.0.0.1 httptest server
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))
	defer origin.Close()
	host := originHostPort(t, origin.URL)
	p.Allow(TokenKey("secret"), []string{hostOnly(host)}) // keyed by TOKEN, not IP

	_, proxyAddr := serve(t, p)
	proxyURL, _ := url.Parse("http://sb:secret@" + proxyAddr) // credential in the proxy URL
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}

	resp, err := client.Get("http://" + host + "/")
	if err != nil {
		t.Fatalf("token-authorised request failed: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "ok" {
		t.Fatalf("body = %q", body)
	}
	// wrong token → its own (unregistered) principal → denied
	wrong, _ := url.Parse("http://sb:nope@" + proxyAddr)
	c2 := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(wrong)}}
	resp, err = c2.Get("http://" + host + "/")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong token should 403, got %d", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestConnectTunnelsAllowedAndBlocksDenied(t *testing.T) {
	// A TLS origin server to CONNECT to.
	origin := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("secret payload"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL)

	p, proxyAddr := newProxy(t)
	key := TokenKey("s1")
	p.Allow(key, []string{hostOnly(originHost)})

	client := tlsClientThroughProxy(proxyAddr, "s1", origin.Certificate())

	// allowed → tunnels through, we read the origin's bytes
	resp, err := client.Get("https://" + originHost + "/")
	if err != nil {
		t.Fatalf("allowed CONNECT failed: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "secret payload" {
		t.Fatalf("tunnel body = %q", body)
	}

	// deny everything, retry → the proxy refuses the CONNECT (403 surfaces as a transport error)
	p.Allow(key, nil)
	if _, err := client.Get("https://" + originHost + "/"); err == nil {
		t.Fatal("denied CONNECT should not succeed")
	}
	if d := p.Denials(key); len(d) == 0 || d[len(d)-1].Method != "CONNECT" {
		t.Fatalf("a denial should be recorded, got %+v", d)
	}
}

func TestPlainHTTPForwardsAllowedAndBlocksDenied(t *testing.T) {
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL)

	p, proxyAddr := newProxy(t)
	key := TokenKey("h1")
	p.Allow(key, []string{hostOnly(originHost)})

	proxyURL, _ := url.Parse("http://sb:h1@" + proxyAddr)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}

	resp, err := client.Get("http://" + originHost + "/x")
	if err != nil {
		t.Fatalf("allowed HTTP failed: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "ok" {
		t.Fatalf("body = %q", body)
	}

	p.Revoke(key)
	resp, err = client.Get("http://" + originHost + "/x")
	if err != nil {
		t.Fatalf("request error: %v", err)
	}
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("denied HTTP should be 403, got %d", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestVettedDialRefusesInwardAnswers(t *testing.T) {
	for _, ip := range []string{
		"127.0.0.1",         // loopback
		"10.1.2.3",          // RFC 1918 private
		"169.254.169.254",   // link-local - the cloud metadata door, by name
		"::1",               // IPv6 loopback
		"100.64.0.9",        // RFC 6598 carrier-grade NAT
		"0.0.0.1",           // "this network" (RFC 791)
		"192.0.0.1",         // IETF protocol assignments (RFC 6890)
		"198.18.0.1",        // RFC 2544 benchmarking
		"240.0.0.1",         // reserved for future use
		"64:ff9b::a00:1",    // NAT64 well-known prefix (10.0.0.1 on a NAT64 network)
		"2002:c000:0204::1", // 6to4 - embeds 192.0.2.4
		"2001:db8::1",       // documentation - never a real destination
	} {
		if !refuseIP(net.ParseIP(ip)) {
			t.Fatalf("%s must be refused", ip)
		}
	}
	if refuseIP(net.ParseIP("93.184.216.34")) {
		t.Fatal("a public address must pass")
	}
}

// The backend's schemas/egress.refuse_ip walks the same file (tests/unit/
// test_egress_refuse_vectors.py): one table, two doors, no drift.
func TestRefuseIPAgreesWithTheSharedVectors(t *testing.T) {
	raw, err := os.ReadFile("testdata/refuse_ip.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Refuse []string `json:"refuse"`
		Allow  []string `json:"allow"`
	}
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, ip := range vectors.Refuse {
		if !refuseIP(net.ParseIP(ip)) {
			t.Errorf("%s must be refused", ip)
		}
	}
	for _, ip := range vectors.Allow {
		if refuseIP(net.ParseIP(ip)) {
			t.Errorf("%s must pass", ip)
		}
	}
}

func TestProxyRefusesAnAllowlistedHostThatResolvesInward(t *testing.T) {
	origin := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("must never arrive"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL) // 127.0.0.1:port

	strict := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	// AllowLoopback stays false: this proxy is configured the way production is.
	p, proxyAddr := serve(t, strict)
	key := TokenKey("s2")
	p.Allow(key, []string{hostOnly(originHost)}) // allowlisted by name, resolves to loopback

	client := tlsClientThroughProxy(proxyAddr, "s2", origin.Certificate())
	if _, err := client.Get("https://" + originHost + "/"); err == nil {
		t.Fatal("an allowlisted host that resolves inward must still be refused")
	}
	d := p.Denials(key)
	if len(d) == 0 || d[len(d)-1].Reason != "private address" {
		t.Fatalf("expected a 'private address' denial, got %+v", d)
	}
}

// The plain-HTTP forward path's mirror of TestProxyRefusesAnAllowlistedHostThatResolvesInward:
// vettedDial guards handleHTTP exactly the same way it guards handleConnect.
func TestProxyRefusesAnAllowlistedHostOverPlainHTTPThatResolvesInward(t *testing.T) {
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("must never arrive"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL) // 127.0.0.1:port

	strict := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	// AllowLoopback stays false: this proxy is configured the way production is.
	p, proxyAddr := serve(t, strict)
	key := TokenKey("h2")
	p.Allow(key, []string{hostOnly(originHost)}) // allowlisted by name, resolves to loopback

	proxyURL, _ := url.Parse("http://sb:h2@" + proxyAddr)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}

	resp, err := client.Get("http://" + originHost + "/")
	if err != nil {
		t.Fatalf("request error: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("an allowlisted host that resolves inward must still be refused, got %d", resp.StatusCode)
	}
	d := p.Denials(key)
	if len(d) == 0 || d[len(d)-1].Reason != "private address" {
		t.Fatalf("expected a 'private address' denial, got %+v", d)
	}
}

// TestConnectChallengesUncredentialedClient proves the door a browser image's sidecar answers
// (image_browser/sidecar/browser.py's Fetch.authRequired handler) actually gets opened:
// Chromium never puts the proxy credential on argv, so its FIRST CONNECT carries no
// Proxy-Authorization at all - and only a genuine 407 (never a flat 403) makes it retry with one.
// The host is deliberately ALLOWED for the token principal here, to prove the challenge fires on
// the missing credential, not the domain.
func TestConnectChallengesUncredentialedClient(t *testing.T) {
	origin := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("must never arrive"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL)

	p, proxyAddr := newProxy(t)
	p.Allow(TokenKey("c1"), []string{hostOnly(originHost)})

	resp := rawConnect(t, proxyAddr, originHost, "")
	if resp.StatusCode != http.StatusProxyAuthRequired {
		t.Fatalf("uncredentialed CONNECT should 407, got %d", resp.StatusCode)
	}
	if got := resp.Header.Get("Proxy-Authenticate"); got != `Basic realm="sandbar"` {
		t.Fatalf("Proxy-Authenticate = %q", got)
	}
	if d := p.Denials("ip:127.0.0.1"); len(d) == 0 || d[len(d)-1].Reason != "no credential" {
		t.Fatalf("expected a 'no credential' denial, got %+v", d)
	}
}

// A WRONG credential must never re-challenge: browser.py's _on_auth_required answers a
// Proxy-sourced challenge exactly once (_auth_attempted), then CancelAuths any repeat for the
// same requestId - if a bad credential got a second 407 instead of a flat 403, that would loop.
func TestConnectWrongCredentialIsFlat403NotChallenge(t *testing.T) {
	origin := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("must never arrive"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL)

	p, proxyAddr := newProxy(t)
	p.Allow(TokenKey("right"), []string{hostOnly(originHost)})

	resp := rawConnect(t, proxyAddr, originHost, basicAuth("sb", "wrong"))
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong credential should 403, got %d", resp.StatusCode)
	}
	if resp.Header.Get("Proxy-Authenticate") != "" {
		t.Fatal("a wrong credential must not be re-challenged")
	}
}

// A correct credential still tunnels through on the first attempt - TestConnectTunnelsAllowedAndBlocksDenied
// (tlsClientThroughProxy always sends Proxy-Authorization preemptively) already covers this; the
// 407 path above only fires when no credential is presented at all.

// ── helpers ───────────────────────────────────────────────────────────────────

func rawConnect(t *testing.T, proxyAddr, hostport, proxyAuthHeader string) *http.Response {
	t.Helper()
	conn, err := net.Dial("tcp", proxyAddr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	req, err := http.NewRequest(http.MethodConnect, "http://"+hostport, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Host = hostport
	if proxyAuthHeader != "" {
		req.Header.Set("Proxy-Authorization", proxyAuthHeader)
	}
	if err := req.Write(conn); err != nil {
		t.Fatal(err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func basicAuth(user, pass string) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(user+":"+pass))
}

func originHostPort(t *testing.T, rawURL string) string {
	t.Helper()
	u, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	return u.Host
}

func tlsClientThroughProxy(proxyAddr, token string, cert *x509.Certificate) *http.Client {
	proxyURL, _ := url.Parse("http://sb:" + token + "@" + proxyAddr)
	pool := x509.NewCertPool()
	pool.AddCert(cert)
	return &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			Proxy:           http.ProxyURL(proxyURL),
			TLSClientConfig: &tls.Config{RootCAs: pool, ServerName: "127.0.0.1"},
			DialContext:     (&net.Dialer{Timeout: 3 * time.Second}).DialContext,
			// Force a fresh CONNECT per request so the deny case is re-evaluated. A real
			// sandbox's allowlist is set once at create and never tightened mid-life, so tunnel
			// reuse is not a security hole in production - but this test flips it mid-flight.
			DisableKeepAlives: true,
		},
	}
}

// An idle keep-alive must end. Behind the relay every connection the proxy keeps open
// holds a relay pipe, and a sandbox that parks one idle keep-alive per slot after a 403 would hold
// them all. (A CONNECT tunnel is hijacked; the server's timeouts no longer apply to it.)
func TestAnIdleKeepAliveIsClosed(t *testing.T) {
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	p.idleTO = 200 * time.Millisecond
	_, addr := serve(t, p)
	p.Allow(TokenKey("t1"), []string{"allowed.test"})
	c, err := net.DialTimeout("tcp", addr, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_, _ = c.Write([]byte("GET http://denied.test/ HTTP/1.1\r\nHost: denied.test\r\nProxy-Authorization: " +
		basicAuth("sb", "t1") + "\r\n\r\n"))
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, nil)
	if err != nil || resp.StatusCode != http.StatusForbidden || resp.Close {
		t.Fatalf("want a kept-alive 403, got %v (close=%v)", err, resp != nil && resp.Close)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	began := time.Now()
	if _, err := br.ReadByte(); err != io.EOF || time.Since(began) > 1500*time.Millisecond {
		t.Fatalf("an idle keep-alive must be closed after the idle timeout, got %v after %v", err, time.Since(began))
	}
}

// A CONNECT tunnel that carries nothing must end. It is hijacked, so the server's
// timeouts no longer apply, and behind the relay an open tunnel holds a relay pipe: a sandbox that
// parks idle tunnels to a host that never hangs up would hold its share of the relay for good.
func TestAnIdleTunnelIsClosed(t *testing.T) {
	upstream := make(chan net.Conn, 1)
	origin := holdingOrigin(t, upstream, nil)
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	p.AllowLoopback = true
	p.tunnelIdle = 200 * time.Millisecond
	_, addr := serve(t, p)
	p.Allow(TokenKey("t1"), []string{"127.0.0.1"})

	c, br := connectThrough(t, addr, origin, "t1")
	held := <-upstream
	began := time.Now()
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := br.ReadByte(); err != io.EOF || time.Since(began) > 1500*time.Millisecond {
		t.Fatalf("an idle tunnel must be closed after the idle bound, got %v after %v", err, time.Since(began))
	}
	_ = held.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := held.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("the upstream end of an idle tunnel must be closed too, got %v", err)
	}
}

// The bound is on the tunnel, not on each direction: a download whose client says nothing is
// busy, and its silent half must not cut it.
func TestATunnelBusyOneWayOutlivesTheIdleBound(t *testing.T) {
	origin := holdingOrigin(t, nil, func(c net.Conn) {
		for i := 0; i < 12; i++ { // 600 ms of trickle, three idle bounds long
			time.Sleep(50 * time.Millisecond)
			if _, err := c.Write([]byte{'x'}); err != nil {
				return
			}
		}
	})
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	p.AllowLoopback = true
	p.tunnelIdle = 200 * time.Millisecond
	_, addr := serve(t, p)
	p.Allow(TokenKey("t1"), []string{"127.0.0.1"})

	c, br := connectThrough(t, addr, origin, "t1")
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	got := 0
	for {
		if _, err := br.ReadByte(); err != nil {
			if err != io.EOF {
				t.Fatalf("the tunnel should end with EOF once it goes quiet, got %v", err)
			}
			break
		}
		got++
	}
	if got != 12 {
		t.Fatalf("a tunnel busy one way must carry every byte, got %d of 12", got)
	}
}

// holdingOrigin is a TCP origin on 127.0.0.1 that hands each accepted connection to conns (when
// set) and runs talk on it (when set), and never closes it first.
func holdingOrigin(t *testing.T, conns chan<- net.Conn, talk func(net.Conn)) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			t.Cleanup(func() { c.Close() })
			if conns != nil {
				conns <- c
			}
			if talk != nil {
				go talk(c)
			}
		}
	}()
	return ln.Addr().String()
}

// connectThrough opens a CONNECT tunnel to hostport through the proxy with the token as the proxy
// credential, and returns the tunnel once the proxy has said 200.
func connectThrough(t *testing.T, proxyAddr, hostport, token string) (net.Conn, *bufio.Reader) {
	t.Helper()
	c, err := net.DialTimeout("tcp", proxyAddr, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	_, _ = c.Write([]byte("CONNECT " + hostport + " HTTP/1.1\r\nHost: " + hostport +
		"\r\nProxy-Authorization: " + basicAuth("sb", token) + "\r\n\r\n"))
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, nil)
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200 Connection established, got %v %v", resp, err)
	}
	return c, br
}

// An allowlisted name is a door to its web ports only: a CONNECT to any other port (its SSH, its
// database) is refused and recorded, unless the operator names that port.
func TestConnectReachesOnlyTheAllowedPorts(t *testing.T) {
	origin := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("must never arrive"))
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL) // 127.0.0.1:<random>, never 80 or 443

	p, proxyAddr := newProxy(t)
	if err := p.SetConnectPorts(""); err != nil { // back to the default, 443 and 80
		t.Fatal(err)
	}
	key := TokenKey("ports")
	p.Allow(key, []string{"127.0.0.1"})
	resp := rawConnect(t, proxyAddr, originHost, basicAuth("sb", "ports"))
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("a CONNECT to %s must be refused by default, got %d", originHost, resp.StatusCode)
	}
	if d := p.Denials(key); len(d) != 1 || d[0].Reason == "" || d[0].Method != "CONNECT" {
		t.Fatalf("the refused port must be recorded, got %+v", d)
	}

	_, port, _ := net.SplitHostPort(originHost)
	if err := p.SetConnectPorts("443, " + port); err != nil {
		t.Fatal(err)
	}
	if resp := rawConnect(t, proxyAddr, originHost, basicAuth("sb", "ports")); resp.StatusCode != http.StatusOK {
		t.Fatalf("a port the operator named must tunnel, got %d", resp.StatusCode)
	}
	for _, bad := range []string{"0", "65536", "https", "443,"} {
		if err := p.SetConnectPorts(bad); err == nil {
			t.Errorf("SetConnectPorts(%q) must refuse", bad)
		}
	}
}

// A client that presents a fresh unknown token per request must not grow the proxy's memory: the
// refusals of unregistered principals are capped, while a registered sandbox's are always kept.
func TestUnknownPrincipalsCannotGrowTheDenialBook(t *testing.T) {
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 0)
	known := TokenKey("known")
	p.Allow(known, nil)
	for i := 0; i < 3*maxStray; i++ {
		p.deny(TokenKey(strconv.Itoa(i)), "example.com", "CONNECT", "not in allowlist")
	}
	p.deny(known, "example.com", "CONNECT", "not in allowlist")
	if len(p.stray) > maxStray {
		t.Fatalf("unregistered principals hold %d entries, want at most %d", len(p.stray), maxStray)
	}
	if d := p.Denials(known); len(d) != 1 {
		t.Fatalf("a registered principal keeps its refusals, got %+v", d)
	}
	p.Revoke(known)
	if len(p.Denials(known)) != 0 {
		t.Fatal("Revoke drops a principal's refusals")
	}
}

// A new allowlist starts a new audit: the refusals recorded under the list it replaces are dropped,
// so a host the push now permits is never reported as refused, and one still refused is reported
// again only when it is refused again.
func TestAllowForgetsTheRefusalsOfTheListItReplaces(t *testing.T) {
	p := New(slog.New(slog.NewTextHandler(io.Discard, nil)), 0)
	key := TokenKey("box")
	p.Allow(key, []string{"conformance.example"})
	p.deny(key, "example.com", "CONNECT", "not in allowlist")
	p.deny(key, "other.example", "CONNECT", "not in allowlist")
	p.Allow(key, []string{"conformance.example", "example.com"})
	if d := p.Denials(key); len(d) != 0 {
		t.Fatalf("a push forgets the refusals of the list it replaced, got %+v", d)
	}
	p.deny(key, "other.example", "CONNECT", "not in allowlist")
	if d := p.Denials(key); len(d) != 1 || d[0].Host != "other.example" {
		t.Fatalf("a refusal under the new list is recorded, got %+v", d)
	}

	// A principal refused before it was registered (the stray book) starts clean too.
	late := TokenKey("late")
	p.deny(late, "example.com", "CONNECT", "not in allowlist")
	p.Allow(late, nil)
	if d := p.Denials(late); len(d) != 0 {
		t.Fatalf("registering forgets the refusals recorded before it, got %+v", d)
	}
}

// Hop-by-hop headers describe one connection, so the forward path drops them both ways - and with
// them every header the Connection header names, and the proxy credential.
func TestPlainHTTPDropsHopByHopHeaders(t *testing.T) {
	seen := make(chan http.Header, 1)
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Clone()
		w.Header().Set("Connection", "X-Origin-Hop")
		w.Header().Set("X-Origin-Hop", "1")
		w.Header().Set("Keep-Alive", "timeout=5")
		w.Header().Set("X-Kept", "1")
	}))
	defer origin.Close()
	originHost := originHostPort(t, origin.URL)

	p, proxyAddr := newProxy(t)
	p.Allow(TokenKey("hop"), []string{"127.0.0.1"})
	conn, err := net.Dial("tcp", proxyAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	head := "GET http://" + originHost + "/ HTTP/1.1\r\n" +
		"Host: " + originHost + "\r\n" +
		"Proxy-Authorization: " + basicAuth("sb", "hop") + "\r\n" +
		"Connection: X-Client-Hop\r\n" +
		"X-Client-Hop: 1\r\n" +
		"Te: trailers\r\n" +
		"X-Kept: 1\r\n\r\n"
	if _, err := conn.Write([]byte(head)); err != nil {
		t.Fatal(err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	up := <-seen
	for _, h := range []string{"Proxy-Authorization", "X-Client-Hop", "Te"} {
		if up.Get(h) != "" {
			t.Errorf("the origin saw the hop-by-hop header %s", h)
		}
	}
	if up.Get("X-Kept") != "1" || resp.Header.Get("X-Kept") != "1" {
		t.Error("an end-to-end header must pass both ways")
	}
	for _, h := range []string{"X-Origin-Hop", "Keep-Alive"} {
		if resp.Header.Get(h) != "" {
			t.Errorf("the client saw the hop-by-hop header %s", h)
		}
	}
}
