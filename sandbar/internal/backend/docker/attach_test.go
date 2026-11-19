// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

var _ core.Attacher = (*instance)(nil)

// frame is one of Docker's multiplexed frames: {stream, 0,0,0, size32} + payload.
func frame(stream byte, payload string) []byte {
	hdr := make([]byte, 8, 8+len(payload))
	hdr[0] = stream
	binary.BigEndian.PutUint32(hdr[4:], uint32(len(payload)))
	return append(hdr, payload...)
}

// attachEngine plays the two Engine calls Attach makes, on a unix socket: exec create (recording
// its body) and a hijacked exec start that says "hello" on stdout, "noise" on stderr, then echoes
// every stdin chunk back as stdout. createStatus != 201 refuses the create. With swallow set, the
// start is a command that reads its stdin to the end (kept in stdin), says errText on stderr and
// exits with exit, which the exec inspect then answers.
type attachEngine struct {
	sock         string
	createStatus int
	mu           sync.Mutex
	created      map[string]any
	upgrade      string

	swallow bool
	exit    int
	errText string
	stdin   []byte
}

// swallowing turns e into the command a file write runs: it reads all of stdin, then exits.
func (e *attachEngine) swallowing(exit int, errText string) *attachEngine {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.swallow, e.exit, e.errText = true, exit, errText
	return e
}

// swallowStart answers a hijacked start as the command swallowing describes.
func (e *attachEngine) swallowStart(conn net.Conn, rw *bufio.ReadWriter) {
	_, _ = rw.WriteString("HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n" +
		"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
	_ = rw.Flush()
	in, _ := io.ReadAll(rw) // to the client's half-close: the command's stdin ends
	e.mu.Lock()
	e.stdin = in
	errText := e.errText
	e.mu.Unlock()
	if errText != "" {
		_, _ = conn.Write(frame(2, errText))
	}
}

func playAttachEngine(t *testing.T, createStatus int) *attachEngine {
	t.Helper()
	dir, err := os.MkdirTemp("", "sbd") // short: a unix socket path has a ~104-byte limit on macOS
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	e := &attachEngine{sock: filepath.Join(dir, "e.sock"), createStatus: createStatus}
	ln, err := net.Listen("unix", e.sock)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1.44/containers/c1/exec", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		e.mu.Lock()
		e.created = body
		e.mu.Unlock()
		if e.createStatus != http.StatusCreated {
			w.WriteHeader(e.createStatus)
			_, _ = w.Write([]byte(`{"message":"container c1 is not running"}`))
			return
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"Id":"e1"}`))
	})
	mux.HandleFunc("POST /v1.44/exec/e1/start", func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body) // the Engine reads the start body before it hijacks
		e.mu.Lock()
		e.upgrade = r.Header.Get("Upgrade")
		e.mu.Unlock()
		conn, rw, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer conn.Close()
		e.mu.Lock()
		swallow := e.swallow
		e.mu.Unlock()
		if swallow {
			e.swallowStart(conn, rw)
			return
		}
		// The 101 and the first frames in one write: the client must not lose bytes its
		// response reader buffered past the headers.
		_, _ = rw.WriteString("HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n" +
			"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n" + string(frame(2, "noise")) + string(frame(1, "hello")))
		_ = rw.Flush()
		buf := make([]byte, 1024)
		for {
			n, err := rw.Read(buf)
			if n > 0 {
				_, _ = conn.Write(frame(1, string(buf[:n])))
			}
			if err != nil {
				return
			}
		}
	})
	mux.HandleFunc("GET /v1.44/exec/e1/json", func(w http.ResponseWriter, r *http.Request) {
		e.mu.Lock()
		defer e.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"ExitCode": e.exit, "Running": false})
	})
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return e
}

// demuxInto keeps stdout and stderr apart, and a stream cut inside a frame header is a clean end.
func TestDemuxSplitsStdoutFromStderr(t *testing.T) {
	stream := append(append(frame(1, "out "), frame(2, "err")...), frame(1, "more")...)
	stream = append(stream, 1, 0, 0) // a truncated header
	var stdout, stderr bytes.Buffer
	err := demuxInto(bytes.NewReader(stream), &stdout, &stderr)
	if err != nil || stdout.String() != "out more" || stderr.String() != "err" {
		t.Fatalf("demux: %q %q %v", stdout.String(), stderr.String(), err)
	}
}

// A command that prints without end is cut at the bound, keeps draining (so it never stalls on a
// full pipe), and the result says it was cut.
func TestExecOutputIsBoundedAndSaysSo(t *testing.T) {
	big := strings.Repeat("x", core.MaxExecOutput+10)
	inst := lsEngine(201, 0, big, "err")
	res, err := inst.Exec(context.Background(), core.ExecRequest{Command: "yes"})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Stdout) != core.MaxExecOutput || !res.Truncated || res.Stderr != "err" {
		t.Fatalf("stdout %d bytes, truncated %v, stderr %q", len(res.Stdout), res.Truncated, res.Stderr)
	}
	small := lsEngine(201, 0, "ok\n", "")
	if res, _ := small.Exec(context.Background(), core.ExecRequest{Command: "true"}); res.Truncated {
		t.Fatal("output under the bound is not truncated")
	}
}

func TestAttachTakesTheExecOverAndPassesOnlyStdout(t *testing.T) {
	e := playAttachEngine(t, http.StatusCreated)
	inst := &instance{b: New(e.sock), cid: "c1"}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	s, err := inst.Attach(ctx, core.DisplayBridge())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	got := make([]byte, 5)
	if _, err := io.ReadFull(s, got); err != nil || string(got) != "hello" {
		t.Fatalf("the first stdout bytes, with stderr dropped: %q %v", got, err)
	}
	if _, err := s.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(s, got[:4]); err != nil || string(got[:4]) != "ping" {
		t.Fatalf("stdin must reach the process and come back: %q %v", got[:4], err)
	}
	e.mu.Lock()
	created, upgrade := e.created, e.upgrade
	e.mu.Unlock()
	cmd, _ := created["Cmd"].([]any)
	env, _ := json.Marshal(created["Env"])
	// A plain `sh -c`, as Exec runs: a login shell would read a startup file first.
	if created["AttachStdin"] != true || created["Tty"] != false || created["User"] != "1001" ||
		len(cmd) != 3 || cmd[0] != "/bin/sh" || cmd[1] != "-c" || cmd[2] != "cd /home/browser && exec python3 -m sidecar.screen" ||
		!strings.Contains(string(env), "PYTHONPATH=/opt/plane/browser") || upgrade != "tcp" {
		t.Fatalf("exec create %v, upgrade %q", created, upgrade)
	}
}

func TestAttachEndsWithItsContext(t *testing.T) {
	e := playAttachEngine(t, http.StatusCreated)
	inst := &instance{b: New(e.sock), cid: "c1"}
	ctx, cancel := context.WithCancel(context.Background())
	s, err := inst.Attach(ctx, core.DisplayBridge())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	got := make([]byte, 5)
	if _, err := io.ReadFull(s, got); err != nil {
		t.Fatal(err)
	}
	cancel()
	done := make(chan error, 1)
	go func() {
		_, err := s.Read(got)
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a read after the context ended must fail")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the stream outlived its context")
	}
}

func TestAttachToAStoppedContainerIsTheEnginesError(t *testing.T) {
	e := playAttachEngine(t, http.StatusConflict)
	inst := &instance{b: New(e.sock), cid: "c1"}
	_, err := inst.Attach(context.Background(), core.DisplayBridge())
	var ee *engineError
	if !errors.As(err, &ee) || ee.status != http.StatusConflict || !strings.Contains(ee.msg, "not running") {
		t.Fatalf("want the Engine's 409, got %v", err)
	}
	if _, err := inst.Attach(context.Background(), core.ExecRequest{Command: "id", User: "root"}); !errors.Is(err, core.ErrBadSpec) {
		t.Fatalf("an unknown user is a bad spec, got %v", err)
	}
}

// TestTheBrowserBridgeSaysHello runs the screen's bridge in a real browser sandbox: one sidecar
// verb first, so the daemon exists (the bridge never starts it), then the first framed message
// down the attached bridge is the daemon's hello - [u32 header_len][JSON header][payload].
func TestTheBrowserBridgeSaysHello(t *testing.T) {
	b := dockerOrSkip(t)
	image := os.Getenv("SANDBAR_BROWSER_IMAGE")
	if image == "" {
		t.Skip("SANDBAR_BROWSER_IMAGE not set; run `just sandbar-browser-image` and export the tag")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	inst, err := b.Create(ctx, "sb-display-"+randToken()[:6], core.Spec{Image: image}.WithDefaults())
	if err != nil {
		t.Fatal(err)
	}
	defer inst.Destroy(context.Background())
	res, err := inst.Exec(ctx, core.ExecRequest{
		Command: "cd /home/browser && python3 -m sidecar.ctl", User: "browser", TimeoutS: 60,
		Env: map[string]string{"SIDECAR_CMD": `{"verb":"open","url":"about:blank"}`, "PYTHONPATH": "/opt/plane/browser", "HOME": "/home/browser"},
	})
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(res.Stdout), "\n")
	var opened map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &opened); err != nil || opened["ok"] != true {
		t.Fatalf("open about:blank: %q (stderr %q)", res.Stdout, res.Stderr)
	}

	readCtx, cancelRead := context.WithTimeout(ctx, 30*time.Second)
	defer cancelRead()
	s, err := inst.(core.Attacher).Attach(readCtx, core.DisplayBridge())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	hello := []byte(`{"n":0,"t":"hello","v":1}`)
	up := binary.BigEndian.AppendUint32(nil, uint32(len(hello)))
	if _, err := s.Write(append(up, hello...)); err != nil {
		t.Fatal(err)
	}
	r := bufio.NewReader(s)
	var size [4]byte
	if _, err := io.ReadFull(r, size[:]); err != nil {
		t.Fatalf("no framed message from the bridge: %v", err)
	}
	n := binary.BigEndian.Uint32(size[:])
	if n == 0 || n > 4096 {
		t.Fatalf("header length %d is outside the wire's bounds", n)
	}
	head := make([]byte, n)
	if _, err := io.ReadFull(r, head); err != nil {
		t.Fatal(err)
	}
	var h map[string]any
	if err := json.Unmarshal(head, &h); err != nil || h["t"] != "hello" || h["v"] != float64(1) {
		t.Fatalf("the first message must be hello v1: %s (%v)", head, err)
	}
}
