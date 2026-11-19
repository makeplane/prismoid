// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package firecracker

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"strconv"
	"time"
)

// guestPort is the vsock port sandbar-guestd listens on inside the VM.
const guestPort = 52

// Firecracker exposes the guest's vsock as a HOST unix socket: the host connects to
// that unix socket, writes "CONNECT <port>\n", reads "OK <hostport>\n", and then has
// a byte stream to the guest listener on <port>. This is Firecracker's documented
// "host-initiated connection" handshake - we implement it here rather than depend on
// a vsock library, keeping the backend stdlib-only.
//
// dialGuest returns a ready net.Conn already past the handshake.
func dialGuest(ctx context.Context, udsPath string, port int) (net.Conn, error) {
	d := net.Dialer{}
	conn, err := d.DialContext(ctx, "unix", udsPath)
	if err != nil {
		return nil, fmt.Errorf("dial vsock uds: %w", err)
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", port); err != nil {
		conn.Close()
		return nil, fmt.Errorf("vsock CONNECT: %w", err)
	}
	// Response line: "OK <assigned_host_port>\n".
	br := bufio.NewReader(conn)
	line, err := br.ReadString('\n')
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("vsock CONNECT reply: %w", err)
	}
	if len(line) < 3 || line[:2] != "OK" {
		conn.Close()
		return nil, fmt.Errorf("vsock CONNECT refused: %q", line)
	}
	_ = conn.SetDeadline(time.Time{}) // clear; per-request contexts own timeouts hereafter
	// Any bytes buffered in br after the OK line belong to the caller. Wrap so they
	// are not lost (guestd will not send early bytes, but correctness beats luck).
	return &bufConn{Conn: conn, r: br}, nil
}

type bufConn struct {
	net.Conn
	r *bufio.Reader
}

func (c *bufConn) Read(p []byte) (int, error) { return c.r.Read(p) }

// waitForGuest polls the vsock handshake until guestd answers or the deadline passes -
// the honest "VM is actually ready" signal, better than sleeping a fixed guess.
func waitForGuest(ctx context.Context, udsPath string, port int, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		dctx, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
		conn, err := dialGuest(dctx, udsPath, port)
		cancel()
		if err == nil {
			conn.Close()
			return nil
		}
		last = err
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
	return fmt.Errorf("guest not ready after %s: %w", timeout, last)
}

// sscan is a tiny strconv wrapper used by config (kept here so config.go stays import-light).
func sscan(s string, f *float64) (int, error) {
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, err
	}
	*f = v
	return 1, nil
}
