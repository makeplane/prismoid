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
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// execMark is the variable each Exec carries, set to a fresh random value, so the processes of an
// exec that outlived its timeout can be found and killed: the Engine has no verb that kills an
// exec, and its processes inherit the variable. A process that clears its own environment escapes
// the sweep, though its parent shell does not.
const execMark = "SANDBAR_EXEC"

// killMarkedScript kills every process of the calling uid whose environment carries
// execMark=$1. It runs as the exec's own uid, so it can signal nothing that uid does not own.
const killMarkedScript = `for p in /proc/[0-9]*; do
	case "$(tr '\000' '\n' < "$p/environ" 2>/dev/null)" in
	*"` + execMark + `=$1"*) kill -9 "${p#/proc/}" 2>/dev/null ;;
	esac
done
true`

func (i *instance) Exec(ctx context.Context, req core.ExecRequest) (core.ExecResult, error) {
	timeout := req.TimeoutS
	if timeout <= 0 {
		timeout = 120
	}
	uid, err := req.UID()
	if err != nil {
		return core.ExecResult{}, err
	}
	runCtx, cancel := context.WithTimeout(ctx, time.Duration(timeout*float64(time.Second)))
	defer cancel()

	start := time.Now()
	// Run as the resolved uid (agent or browser) through a plain `sh -c`. PATH is the image's
	// own; a login shell would also run $HOME/.profile, which the agent writes when HOME is its
	// workshop, inside every command the caller's system runs through this door. Env rides the
	// exec request itself, so it is visible only to this one command - never baked into the
	// container's own environment, where every other exec would see it too.
	mark := randToken()
	env := append(envList(req.Env), execMark+"="+mark)
	id, err := i.createExec(runCtx, strconv.Itoa(uid), env, []string{"/bin/sh", "-c", req.Command}, false)
	var res core.ExecResult
	if err == nil {
		res, err = i.collect(runCtx, id)
	}
	switch {
	case err == nil:
		res.DurationMS = float64(time.Since(start).Microseconds()) / 1000
		return res, nil
	case runCtx.Err() == nil:
		return core.ExecResult{}, err
	}
	// The deadline passed or the caller left: the Engine cannot kill an exec, so its processes are
	// killed here, and nothing it started runs on.
	i.killMarked(context.WithoutCancel(ctx), uid, mark)
	if ctx.Err() != nil {
		return core.ExecResult{}, err
	}
	res.ExitCode = core.ExitTimedOut // our own deadline: a result, as `timeout` reports it
	res.Stderr += fmt.Sprintf("timed out after %gs", timeout)
	res.DurationMS = float64(time.Since(start).Microseconds()) / 1000
	return res, nil
}

// collect runs a created exec to its end and reads its exit code. Each of stdout and stderr keeps
// at most core.MaxExecOutput bytes; on a failure, what was read so far is still returned.
func (i *instance) collect(ctx context.Context, execID string) (core.ExecResult, error) {
	stdout, stderr := core.NewOutputBuffer(core.MaxExecOutput), core.NewOutputBuffer(core.MaxExecOutput)
	err := i.b.execStart(ctx, execID, stdout, stderr)
	res := core.ExecResult{Stdout: stdout.String(), Stderr: stderr.String(), Truncated: stdout.Truncated() || stderr.Truncated()}
	if err != nil {
		return res, err
	}
	res.ExitCode, err = i.b.exitCode(ctx, execID)
	return res, err
}

// exitCode reads the exit code of an exec whose stream has ended.
func (b *Backend) exitCode(ctx context.Context, execID string) (int, error) {
	var insp struct {
		ExitCode int `json:"ExitCode"`
	}
	err := b.do(ctx, http.MethodGet, "/exec/"+execID+"/json", "", nil, &insp)
	return insp.ExitCode, err
}

// killMarked kills what an exec left running (killMarkedScript), bounded so a wedged Engine cannot
// hold the caller. Best effort: a sandbox that is gone has nothing left to kill.
func (i *instance) killMarked(ctx context.Context, uid int, mark string) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	id, err := i.createExec(ctx, strconv.Itoa(uid), []string{}, []string{"/bin/sh", "-c", killMarkedScript, "sh", mark}, false)
	if err == nil {
		_, _ = i.collect(ctx, id)
	}
}

// createExec is the one place an exec is made: stdout and stderr attached, no TTY, the given user,
// environment and argv, and stdin when the caller streams (Attach).
func (i *instance) createExec(ctx context.Context, user string, env, argv []string, stdin bool) (string, error) {
	create := map[string]any{
		"AttachStdout": true, "AttachStderr": true, "Tty": false,
		"User": user, "Env": env, "Cmd": argv,
	}
	if stdin {
		create["AttachStdin"] = true
	}
	body, _ := json.Marshal(create)
	var ex struct {
		ID string `json:"Id"`
	}
	if err := i.b.do(ctx, http.MethodPost, "/containers/"+i.cid+"/exec", "application/json",
		bytes.NewReader(body), &ex); err != nil {
		return "", err
	}
	return ex.ID, nil
}

// systemPATH is where a command sandbar runs for itself is found: the system's own folders, never
// one the image's PATH puts first, which the workload may be able to write - a `stat` or `mkdir`
// planted there would otherwise be what root runs.
const systemPATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"

// execAsRoot runs argv as uid 0 - used for privileged setup (mkdir of a system-owned
// directory) and for taking system paths back (Remove), which the agent-uid exec path cannot do.
// Never a caller's command, root's own HOME (the container's HOME may be the agent's workshop,
// and nothing root runs may read a file from it) and the system's own PATH.
func (i *instance) execAsRoot(ctx context.Context, argv ...string) (core.ExecResult, error) {
	id, err := i.createExec(ctx, "0", []string{"HOME=/root", systemPATH}, argv, false)
	if err != nil {
		return core.ExecResult{}, err
	}
	return i.collect(ctx, id)
}

// execStart runs an exec and demultiplexes Docker's stdout/stderr stream framing into the two
// writers.
func (b *Backend) execStart(ctx context.Context, execID string, stdout, stderr io.Writer) error {
	body, _ := json.Marshal(map[string]any{"Detach": false, "Tty": false})
	rc, err := b.request(ctx, http.MethodPost, "/exec/"+execID+"/start", "application/json", bytes.NewReader(body))
	if err != nil {
		return err
	}
	defer rc.Close()
	return demuxInto(rc, stdout, stderr)
}

// attachExec starts an exec over a connection of its own (hijackExec): Write feeds the process's
// stdin, and Docker's multiplexed frames come down, demultiplexed into a pipe, stderr dropped.
func (b *Backend) attachExec(ctx context.Context, execID string) (io.ReadWriteCloser, error) {
	conn, br, stop, err := b.hijackExec(ctx, execID)
	if err != nil {
		return nil, err
	}
	out, pw := io.Pipe()
	go func() { pw.CloseWithError(demuxInto(br, pw, io.Discard)) }()
	return &attached{conn: conn, out: out, stop: stop}, nil
}

// hijackExec starts an exec over a connection of its own and takes that connection over - the
// Engine's hijack: `Connection: Upgrade` and `Upgrade: tcp`, answered 101. Raw bytes then go up as
// the process's stdin, and Docker's multiplexed frames come down. ctx bounds the whole stream, the
// handshake included: its end closes the connection; stop releases that hook.
// Why a raw connection and http.ReadResponse: the same connection becomes the exec's stdio after
// the 101, and the bufio.Reader may already hold the first frames past the response head, so the
// demux reads from it, never from the connection.
func (b *Backend) hijackExec(ctx context.Context, execID string) (net.Conn, *bufio.Reader, func() bool, error) {
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", b.sock)
	if err != nil {
		return nil, nil, nil, err
	}
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	fail := func(err error) (net.Conn, *bufio.Reader, func() bool, error) {
		stop()
		_ = conn.Close()
		return nil, nil, nil, err
	}
	// The same versioned base request() uses; the host is ignored on a unix socket.
	req, err := http.NewRequest(http.MethodPost, "http://docker/v1.44/exec/"+execID+"/start",
		strings.NewReader(`{"Detach":false,"Tty":false}`))
	if err != nil {
		return fail(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "tcp")
	if err := req.Write(conn); err != nil {
		return fail(err)
	}
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, req)
	if err != nil {
		return fail(err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		return fail(&engineError{status: resp.StatusCode, msg: strings.TrimSpace(string(msg))})
	}
	return conn, br, stop, nil
}

// attached is one streaming exec: Write is the process's stdin, Read its demultiplexed stdout.
type attached struct {
	conn net.Conn
	out  *io.PipeReader
	stop func() bool
	once sync.Once
}

func (a *attached) Read(p []byte) (int, error)  { return a.out.Read(p) }
func (a *attached) Write(p []byte) (int, error) { return a.conn.Write(p) }

// Close ends the stream: the Engine closes the process's stdin when the connection goes.
func (a *attached) Close() error {
	a.once.Do(func() {
		a.stop()
		_ = a.conn.Close()
		_ = a.out.Close()
	})
	return nil
}

// demuxInto decodes Docker's multiplexed stream: 8-byte header {stream, 0,0,0, size32} +
// payload, repeated. stream 2 = stderr; every other frame (1 = stdout) is stdout. A clean or
// truncated end between frames is no error. One copy buffer serves every frame: a stream of many
// frames (a large file read) leaves no garbage the size of what it carried.
func demuxInto(r io.Reader, stdout, stderr io.Writer) error {
	var hdr [8]byte
	buf := make([]byte, 32<<10)
	for {
		if _, err := io.ReadFull(r, hdr[:]); err != nil {
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				return nil
			}
			return err
		}
		size := binary.BigEndian.Uint32(hdr[4:])
		dst := stdout
		if hdr[0] == 2 {
			dst = stderr
		}
		n, err := io.CopyBuffer(dst, io.LimitReader(r, int64(size)), buf)
		if err == nil && n < int64(size) {
			err = io.EOF // cut inside a frame, as io.CopyN reports it
		}
		if err != nil {
			return err
		}
	}
}
