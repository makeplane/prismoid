// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package firecracker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// The guest wire framing, host side. Each call opens a fresh vsock connection (guestd
// serves one request per connection, each in its own goroutine, so a VM's calls run side by
// side; per-call latency is dominated by the work, not the dial). Frame layout:
//
//	→  <4-byte big-endian header length><JSON Request header><BodyLen raw bytes>
//	←  <4-byte big-endian header length><JSON Response header><BodyLen raw bytes>

// call sends a request with an optional in-memory/streamed body and returns the parsed
// response plus any raw response body (read_file contents).
func (i *instance) call(ctx context.Context, req guestwire.Request, body any) (guestwire.Response, []byte, error) {
	var out []byte
	resp, err := i.transact(ctx, req, body, func(r io.Reader, n int64) error {
		// The guest names the length; it is held in memory here, so it is bounded like any file.
		if n > core.MaxFileBytes {
			return fmt.Errorf("guest body of %d bytes is over the %d-byte bound", n, int64(core.MaxFileBytes))
		}
		var err error
		out, err = io.ReadAll(io.LimitReader(r, n))
		return err
	})
	return resp, out, err
}

// callStream is like call but pipes the raw response body to w (archive tar stream).
func (i *instance) callStream(ctx context.Context, req guestwire.Request, body any, w io.Writer) error {
	_, err := i.transact(ctx, req, body, func(r io.Reader, n int64) error {
		_, err := io.CopyN(w, r, n)
		return err
	})
	return err
}

// transact runs one full request/response over a fresh guest connection. No lock is held while
// it runs: the connection is registered with the instance instead, so Destroy can close it and
// a call that would run for minutes never holds a teardown back.
func (i *instance) transact(
	ctx context.Context, req guestwire.Request, body any,
	onBody func(io.Reader, int64) error,
) (guestwire.Response, error) {
	conn, err := i.open(ctx)
	if err != nil {
		return guestwire.Response{}, err
	}
	defer i.release(conn)
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}

	if err := writeFrame(conn, req, body); err != nil {
		return guestwire.Response{}, err
	}

	resp, err := readHeader(conn)
	if err != nil {
		return guestwire.Response{}, err
	}
	if resp.BodyLen > 0 {
		if err := onBody(conn, resp.BodyLen); err != nil {
			return resp, err
		}
	}
	if !resp.OK {
		if resp.Code == guestwire.CodeForbiddenPath {
			return resp, fmt.Errorf("%w: guest: %s", core.ErrForbiddenPath, resp.Error)
		}
		if resp.Code == guestwire.CodeNoSuchPath {
			return resp, fmt.Errorf("%w: guest: %s", core.ErrNoSuchPath, resp.Error)
		}
		return resp, fmt.Errorf("guest: %s", resp.Error)
	}
	return resp, nil
}

// open dials the guest and registers the connection, refusing once the sandbox is destroyed.
func (i *instance) open(ctx context.Context) (net.Conn, error) {
	i.mu.Lock()
	closed := i.closed
	i.mu.Unlock()
	if closed {
		return nil, fmt.Errorf("sandbox %s destroyed", i.id)
	}
	conn, err := dialGuest(ctx, i.vsockUDS, guestPort)
	if err != nil {
		return nil, err
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.closed { // destroyed while dialling
		conn.Close()
		return nil, fmt.Errorf("sandbox %s destroyed", i.id)
	}
	if i.conns == nil {
		i.conns = make(map[net.Conn]struct{})
	}
	i.conns[conn] = struct{}{}
	return conn, nil
}

// release closes a call's connection and forgets it.
func (i *instance) release(conn net.Conn) {
	i.mu.Lock()
	delete(i.conns, conn)
	i.mu.Unlock()
	conn.Close()
}

// writeFrame writes the 4-byte-length-prefixed JSON header then the raw body.
func writeFrame(w io.Writer, req guestwire.Request, body any) error {
	hdr, err := json.Marshal(req)
	if err != nil {
		return err
	}
	if err := writeLen(w, len(hdr)); err != nil {
		return err
	}
	if _, err := w.Write(hdr); err != nil {
		return err
	}
	switch b := body.(type) {
	case nil:
		return nil
	case []byte:
		_, err := w.Write(b)
		return err
	case streamBody:
		_, err := io.CopyN(w, b.r, b.n)
		return err
	default:
		return fmt.Errorf("unsupported body type %T", body)
	}
}

func readHeader(r io.Reader) (guestwire.Response, error) {
	var lb [4]byte
	if _, err := io.ReadFull(r, lb[:]); err != nil {
		return guestwire.Response{}, fmt.Errorf("read response header len: %w", err)
	}
	n := int(lb[0])<<24 | int(lb[1])<<16 | int(lb[2])<<8 | int(lb[3])
	if n < 0 || n > guestwire.MaxHeaderLen {
		return guestwire.Response{}, fmt.Errorf("response header length %d out of range", n)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(r, buf); err != nil {
		return guestwire.Response{}, err
	}
	var resp guestwire.Response
	if err := json.Unmarshal(buf, &resp); err != nil {
		return guestwire.Response{}, err
	}
	return resp, nil
}

func writeLen(w io.Writer, n int) error {
	_, err := w.Write([]byte{byte(n >> 24), byte(n >> 16), byte(n >> 8), byte(n)})
	return err
}

// streamBody carries a length-known reader (a temp file) as a request body.
type streamBody struct {
	r io.Reader
	n int64
}

func bytesReader(b []byte) io.Reader { return bytes.NewReader(b) }

// copyFile clones src → dst. On filesystems that support it the OS may reflink; here we
// do a plain copy for portability (the rootfs template is read-only, so correctness
// holds either way - reflink is a build-time optimization documented in the roadmap).
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}
