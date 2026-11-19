// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// echoOnce answers each connection with "got:" + everything it read, after the client half-closes.
func echoOnce(t *testing.T, ln net.Listener) {
	t.Helper()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("got:"), data...))
			}()
		}
	}()
}

// timedOut says a read gave up waiting - the one outcome a closed connection never has.
func timedOut(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

func listen(t *testing.T) net.Listener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	return ln
}

// serve runs a relay to `to` and returns its address.
func serve(t *testing.T, to string, maxConns int) string {
	t.Helper()
	return start(t, &Relay{To: to, MaxConns: maxConns, DialTO: time.Second, DrainTO: time.Second, Log: quiet()})
}

// start runs r on a fresh listener and returns its address.
func start(t *testing.T, r *Relay) string {
	t.Helper()
	ln := listen(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = r.Serve(ctx, ln) }()
	return ln.Addr().String()
}

func roundTrip(t *testing.T, addr, msg string) (string, error) {
	t.Helper()
	c, err := net.DialTimeout("tcp", addr, time.Second)
	if err != nil {
		return "", err
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(3 * time.Second))
	if _, err := c.Write([]byte(msg)); err != nil {
		return "", err
	}
	_ = c.(*net.TCPConn).CloseWrite()
	got, err := io.ReadAll(c)
	return string(got), err
}

func TestPipesBothWaysAndCarriesTheHalfClose(t *testing.T) {
	target := listen(t)
	echoOnce(t, target)
	got, err := roundTrip(t, serve(t, target.Addr().String(), 4), "hello")
	if err != nil || got != "got:hello" {
		t.Fatalf("want got:hello, got %q (%v)", got, err)
	}
}

func TestPastTheCapAConnectionIsClosedAtOnce(t *testing.T) {
	target := listen(t)
	held := make(chan net.Conn, 1)
	go func() {
		c, err := target.Accept()
		if err == nil {
			held <- c // accepted and never answered: the relay's one slot stays taken
		}
	}()
	addr := serve(t, target.Addr().String(), 1)
	first, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	select {
	case c := <-held:
		defer c.Close()
	case <-time.After(2 * time.Second):
		t.Fatal("the first connection never reached the target")
	}
	second, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	_ = second.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := second.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("a connection past the cap must be closed at once, got %v", err)
	}
}

// The proxy restarting under the relay. A dead target closes the frame's
// connection; the next connection reaches the target once it is back.
func TestADeadTargetClosesAndTheNextConnectionReachesItOnceItIsBack(t *testing.T) {
	gone := listen(t)
	to := gone.Addr().String()
	gone.Close()
	addr := serve(t, to, 4)
	began := time.Now()
	// Closed, not merely silent: an EOF (or a reset, as the relay closes without reading the "x")
	// well inside roundTrip's 3 s deadline, so a relay that held the connection open until the
	// client gave up fails here.
	if got, err := roundTrip(t, addr, "x"); timedOut(err) || got != "" || time.Since(began) > time.Second {
		t.Fatalf("a dead target must close the connection at once with nothing, got %q (%v) after %v",
			got, err, time.Since(began))
	}
	back, err := net.Listen("tcp", to)
	if err != nil {
		t.Skipf("the target's port was taken meanwhile: %v", err)
	}
	defer back.Close()
	echoOnce(t, back)
	if got, err := roundTrip(t, addr, "again"); err != nil || got != "got:again" {
		t.Fatalf("the relay must reach a target that came back, got %q (%v)", got, err)
	}
}

// The proxy is done (it said its piece and closed) but the sandbox
// neither sends nor closes. Its pipe must end within DrainTO, or it holds a slot of a shared cap
// forever.
func TestASilentClientLetsGoOfItsSlotAfterTheProxyIsDone(t *testing.T) {
	target := listen(t)
	go func() {
		first := true
		for {
			c, err := target.Accept()
			if err != nil {
				return
			}
			if first { // the silent client's pipe: the proxy says its piece and closes fully
				first = false
				_, _ = c.Write([]byte("bye"))
				c.Close()
				continue
			}
			go func() { // everyone after: an echo
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("got:"), data...))
			}()
		}
	}()
	addr := start(t, &Relay{To: target.Addr().String(), MaxConns: 1, DialTO: time.Second,
		DrainTO: 200 * time.Millisecond, Log: quiet()})
	silent, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer silent.Close() // only at the end: from here on it sends nothing and never closes
	_ = silent.SetReadDeadline(time.Now().Add(2 * time.Second))
	if got, err := io.ReadAll(silent); err != nil || string(got) != "bye" {
		t.Fatalf("the silent client must hear the proxy out, got %q (%v)", got, err)
	}
	deadline := time.Now().Add(time.Second)
	for {
		got, err := roundTrip(t, addr, "next")
		if err == nil && got == "got:next" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the silent client still holds the only slot a second later, last %q (%v)", got, err)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// The grace is the proxy-done path's only: a client that half-closed (an upload, "that is all")
// waits as long as the proxy takes to answer, well past DrainTO.
func TestAHalfClosedClientStillWaitsOutASlowProxy(t *testing.T) {
	target := listen(t)
	go func() {
		for {
			c, err := target.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				data, _ := io.ReadAll(c)
				time.Sleep(500 * time.Millisecond) // > DrainTO
				_, _ = c.Write(append([]byte("late:"), data...))
			}()
		}
	}()
	addr := start(t, &Relay{To: target.Addr().String(), MaxConns: 4, DialTO: time.Second,
		DrainTO: 200 * time.Millisecond, Log: quiet()})
	if got, err := roundTrip(t, addr, "upload"); err != nil || got != "late:upload" {
		t.Fatalf("a half-closed client must still get a slow reply, got %q (%v)", got, err)
	}
}

func TestFromEnv(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	if _, _, err := FromEnv(env(nil)); err == nil || !strings.Contains(err.Error(), "SANDBAR_RELAY_TO") {
		t.Fatalf("a relay with no target must refuse to start, got %v", err)
	}
	if _, _, err := FromEnv(env(map[string]string{"SANDBAR_RELAY_TO": "sandbard:7080", "SANDBAR_RELAY_MAX_CONNS": "x"})); err == nil {
		t.Fatal("a non-numeric cap must refuse to start")
	}
	for _, bad := range []string{"x", "0", "-3"} {
		if _, _, err := FromEnv(env(map[string]string{"SANDBAR_RELAY_TO": "sandbard:7080", "SANDBAR_RELAY_MAX_PER_PEER": bad})); err == nil ||
			!strings.Contains(err.Error(), "SANDBAR_RELAY_MAX_PER_PEER") {
			t.Fatalf("a per-peer cap of %q must refuse to start, got %v", bad, err)
		}
	}
	// A target with no port would boot and then fail every dial: refuse it at the start.
	for _, bad := range []string{"sandbard", "sandbard:", ":7080"} {
		if _, _, err := FromEnv(env(map[string]string{"SANDBAR_RELAY_TO": bad})); err == nil || !strings.Contains(err.Error(), "host:port") {
			t.Fatalf("SANDBAR_RELAY_TO=%q must refuse to start, got %v", bad, err)
		}
	}
	// One sandbox holds at most 1/64th of the pipes: it takes 64 sandboxes, each keeping 64 tunnels
	// busy, to fill the relay (a Chromium opens at most 32 to one proxy).
	r, addr, err := FromEnv(env(map[string]string{"SANDBAR_RELAY_TO": "sandbard:7080"}))
	if err != nil || addr != "0.0.0.0:7080" || r.To != "sandbard:7080" || r.MaxConns != 4096 || r.MaxPerPeer != 64 ||
		r.DialTO != 10*time.Second || r.DrainTO != 10*time.Second {
		t.Fatalf("defaults: %+v %q %v", r, addr, err)
	}
	r, _, err = FromEnv(env(map[string]string{"SANDBAR_RELAY_TO": "sandbard:7080", "SANDBAR_RELAY_MAX_PER_PEER": "8"}))
	if err != nil || r.MaxPerPeer != 8 {
		t.Fatalf("SANDBAR_RELAY_MAX_PER_PEER=8: %+v %v", r, err)
	}
}

// heldTarget accepts and keeps every connection without answering; the channel hands each one over
// as it arrives (so a test can tell "reached the proxy" from "refused by the relay").
func heldTarget(t *testing.T) (net.Listener, chan net.Conn) {
	t.Helper()
	target := listen(t)
	held := make(chan net.Conn, 16)
	go func() {
		for {
			c, err := target.Accept()
			if err != nil {
				return
			}
			held <- c
		}
	}()
	t.Cleanup(func() {
		for {
			select {
			case c := <-held:
				c.Close()
			default:
				return
			}
		}
	})
	return target, held
}

// dialFrom connects to addr from the given local IP ("" = the OS's choice).
func dialFrom(ip, addr string) (net.Conn, error) {
	d := net.Dialer{Timeout: time.Second}
	if ip != "" {
		d.LocalAddr = &net.TCPAddr{IP: net.ParseIP(ip)}
	}
	return d.Dial("tcp", addr)
}

func mustDial(t *testing.T, addr string) net.Conn {
	t.Helper()
	c, err := dialFrom("", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

// reaches waits for the relay to hand a connection to the target.
func reaches(t *testing.T, held chan net.Conn, what string) {
	t.Helper()
	select {
	case c := <-held:
		t.Cleanup(func() { c.Close() })
	case <-time.After(2 * time.Second):
		t.Fatalf("%s never reached the target", what)
	}
}

// refused expects the relay to have closed c at once.
func refused(t *testing.T, c net.Conn, what string) {
	t.Helper()
	_ = c.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := c.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("%s must be closed at once, got %v", what, err)
	}
}

// One sandbox (one source IP on the frame network) holds at most MaxPerPeer pipes, so
// it cannot take the shared cap from every other sandbox. macOS's loopback answers only 127.0.0.1,
// so here the peers are named in accept order; TestThePeerIsTheSourceIP pins the real key.
func TestOnePeerAtItsCapIsRefusedWhileAnotherStillGetsIn(t *testing.T) {
	target, held := heldTarget(t)
	peers := make(chan string, 4)
	for _, p := range []string{"a", "a", "a", "b"} {
		peers <- p
	}
	r := &Relay{To: target.Addr().String(), MaxConns: 8, MaxPerPeer: 2, DialTO: time.Second, DrainTO: time.Second, Log: quiet()}
	r.peer = func(net.Conn) string { return <-peers }
	addr := start(t, r)
	mustDial(t, addr)
	reaches(t, held, "peer a's first connection")
	mustDial(t, addr)
	reaches(t, held, "peer a's second connection")
	refused(t, mustDial(t, addr), "peer a's third connection (past its cap of 2)")
	mustDial(t, addr)
	reaches(t, held, "peer b's connection, while a is at its cap")
}

// The default peer is the connection's source IP, whatever its port: two connections from one
// address count against one cap. A second loopback address (Linux answers all of 127/8) is a
// second peer; macOS has only 127.0.0.1, so that half runs where the OS allows it (CI).
func TestThePeerIsTheSourceIP(t *testing.T) {
	target, held := heldTarget(t)
	addr := start(t, &Relay{To: target.Addr().String(), MaxConns: 8, MaxPerPeer: 1, DialTO: time.Second, DrainTO: time.Second, Log: quiet()})
	mustDial(t, addr)
	reaches(t, held, "127.0.0.1's first connection")
	refused(t, mustDial(t, addr), "127.0.0.1's second connection, from another port")
	other, err := dialFrom("127.0.0.2", addr)
	if err != nil {
		t.Logf("this OS's loopback has no 127.0.0.2 (%v); the second-peer half runs on Linux", err)
		return
	}
	defer other.Close()
	reaches(t, held, "127.0.0.2's connection, while 127.0.0.1 is at its cap")
}

// logLines is a slog destination a test can read back, one JSON record per line.
type logLines struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (l *logLines) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *logLines) logger() *slog.Logger { return slog.New(slog.NewJSONHandler(l, nil)) }

func (l *logLines) records(t *testing.T, msg string) []map[string]any {
	t.Helper()
	l.mu.Lock()
	defer l.mu.Unlock()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(l.buf.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("log line %q: %v", line, err)
		}
		if rec["msg"] == msg {
			out = append(out, rec)
		}
	}
	return out
}

// A refusal names its source (which sandbox fills the cap is the operator's first question), and a
// sandbox provoking refusals in a loop writes one line per window, not one per connection - an
// unthrottled line per refusal fills the host's disk.
func TestARefusalSaysWhoAndIsLoggedOncePerPeerPerWindow(t *testing.T) {
	target, held := heldTarget(t)
	var logs logLines
	addr := start(t, &Relay{To: target.Addr().String(), MaxConns: 8, MaxPerPeer: 1, DialTO: time.Second, DrainTO: time.Second, Log: logs.logger()})
	mustDial(t, addr)
	reaches(t, held, "the first connection")
	for i := 0; i < 5; i++ {
		refused(t, mustDial(t, addr), "a connection past the peer's cap")
	}
	recs := logs.records(t, "relay refused a connection")
	if len(recs) != 1 || recs[0]["from"] != "127.0.0.1" || recs[0]["why"] != "peer at its cap" {
		t.Fatalf("want one refusal line from 127.0.0.1 for five refusals, got %v", recs)
	}
}

func TestADialFailureSaysWhoAndIsLoggedOncePerPeerPerWindow(t *testing.T) {
	gone := listen(t)
	to := gone.Addr().String()
	gone.Close()
	var logs logLines
	addr := start(t, &Relay{To: to, MaxConns: 8, MaxPerPeer: 8, DialTO: time.Second, DrainTO: time.Second, Log: logs.logger()})
	for i := 0; i < 3; i++ {
		if got, err := roundTrip(t, addr, "x"); timedOut(err) || got != "" {
			t.Fatalf("a dead target closes with nothing, got %q (%v)", got, err)
		}
	}
	recs := logs.records(t, "relay dial failed")
	if len(recs) != 1 || recs[0]["from"] != "127.0.0.1" || recs[0]["to"] != to {
		t.Fatalf("want one dial-failure line from 127.0.0.1 for three failures, got %v", recs)
	}
}

// The window: one line per peer, then silence until it passes; the next line says how many it held
// back. Each peer has its own window.
func TestTheHushLetsOneLinePerPeerPerWindowAndCountsTheRest(t *testing.T) {
	now := time.Unix(1_000, 0)
	h := newHush(10 * time.Second)
	h.now = func() time.Time { return now }
	if ok, held := h.speak("10.0.0.5"); !ok || held != 0 {
		t.Fatalf("a peer's first line speaks: %v %d", ok, held)
	}
	for i := 0; i < 3; i++ {
		if ok, _ := h.speak("10.0.0.5"); ok {
			t.Fatal("inside the window the same peer is held back")
		}
	}
	if ok, _ := h.speak("10.0.0.6"); !ok {
		t.Fatal("another peer has its own window")
	}
	now = now.Add(9 * time.Second)
	if ok, _ := h.speak("10.0.0.5"); ok {
		t.Fatal("9 s in, still held back")
	}
	now = now.Add(time.Second)
	if ok, held := h.speak("10.0.0.5"); !ok || held != 4 {
		t.Fatalf("after the window the next line speaks and counts the four held back, got %v %d", ok, held)
	}
}

// The proxy said its piece and half-closed (an upstream that hung up): a client reading to EOF hears
// it at once - not after DrainTO, which is for a client that never answers. closeWrite(in) is it.
func TestAClientHearsTheProxyFinishAtOnce(t *testing.T) {
	target := listen(t)
	go func() {
		for {
			c, err := target.Accept()
			if err != nil {
				return
			}
			_, _ = c.Write([]byte("done"))
			c.Close()
		}
	}()
	addr := start(t, &Relay{To: target.Addr().String(), MaxConns: 4, MaxPerPeer: 4, DialTO: time.Second, DrainTO: 2 * time.Second, Log: quiet()})
	c := mustDial(t, addr) // never half-closes: it only reads
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	began := time.Now()
	got, err := io.ReadAll(c)
	if err != nil || string(got) != "done" || time.Since(began) > 500*time.Millisecond {
		t.Fatalf("the client must hear the proxy's end at once, got %q (%v) after %v (DrainTO is 2 s)", got, err, time.Since(began))
	}
}

// The proxy is done sending but never reads again: a client still sending fills the pipe toward it
// and parks the relay's write there. DrainTO bounds that write too, or the slot is held forever.
func TestAProxyThatStopsReadingCannotHoldASlot(t *testing.T) {
	target := listen(t)
	go func() {
		first := true
		for {
			c, err := target.Accept()
			if err != nil {
				return
			}
			if first { // says its piece, half-closes, and never reads
				first = false
				_, _ = c.Write([]byte("bye"))
				_ = c.(*net.TCPConn).CloseWrite()
				t.Cleanup(func() { c.Close() })
				continue
			}
			go func() {
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("got:"), data...))
			}()
		}
	}()
	addr := start(t, &Relay{To: target.Addr().String(), MaxConns: 1, MaxPerPeer: 1, DialTO: time.Second,
		DrainTO: 200 * time.Millisecond, Log: quiet()})
	flood := mustDial(t, addr)
	go func() { // sends until the relay lets go of it
		chunk := make([]byte, 64<<10)
		_ = flood.SetWriteDeadline(time.Now().Add(5 * time.Second))
		for {
			if _, err := flood.Write(chunk); err != nil {
				return
			}
		}
	}()
	deadline := time.Now().Add(2 * time.Second)
	for {
		got, err := roundTrip(t, addr, "next")
		if err == nil && got == "got:next" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("a proxy that stopped reading still holds the only slot 2 s later, last %q (%v)", got, err)
		}
		time.Sleep(50 * time.Millisecond)
	}
}
