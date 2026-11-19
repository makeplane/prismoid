// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/guestwire"
)

// vsockConn wraps the accepted fd as an io.ReadWriteCloser without pulling in net.
type vsockConn struct{ fd int }

func (c vsockConn) Read(p []byte) (int, error)  { return unix.Read(c.fd, p) }
func (c vsockConn) Write(p []byte) (int, error) { return unix.Write(c.fd, p) }
func (c vsockConn) Close() error                { return unix.Close(c.fd) }

// serve handles exactly one request on one connection.
func serve(fd int) {
	conn := vsockConn{fd: fd}
	defer conn.Close()

	req, err := readRequest(conn)
	if err != nil {
		writeResponse(conn, guestwire.Response{OK: false, Error: "bad request: " + err.Error()}, nil, 0)
		return
	}
	handle(conn, req)
}

func handle(conn io.ReadWriter, req guestwire.Request) {
	switch req.Op {
	case guestwire.OpPing:
		writeResponse(conn, guestwire.Response{OK: true}, nil, 0)
	case guestwire.OpExec:
		doExec(conn, req)
	case guestwire.OpWriteFile:
		doWriteFile(conn, req)
	case guestwire.OpReadFile:
		doReadFile(conn, req)
	case guestwire.OpListDir:
		doListDir(conn, req)
	case guestwire.OpArchive:
		doArchive(conn, req)
	case guestwire.OpExtract:
		doExtract(conn, req)
	default:
		writeResponse(conn, guestwire.Response{OK: false, Error: "unknown op " + string(req.Op)}, nil, 0)
	}
}

func doExec(conn io.ReadWriter, req guestwire.Request) {
	timeout := req.TimeoutS
	if timeout <= 0 {
		timeout = 120
	}
	// Resolve the exec's uid the same way the docker backend does - agent or browser, nothing
	// else. guestwire.Request carries the same User vocabulary as core.ExecRequest, so borrow
	// its UID() rather than re-deriving the switch here.
	uid, err := (core.ExecRequest{User: req.User}).UID()
	if err != nil {
		writeResponse(conn, guestwire.Response{OK: false, Error: err.Error()}, nil, 0)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeout*float64(time.Second)))
	defer cancel()

	start := time.Now()
	cmd := execCommand(ctx, req, uid)
	stdout, stderr := core.NewOutputBuffer(core.MaxExecOutput), core.NewOutputBuffer(core.MaxExecOutput)
	cmd.Stdout, cmd.Stderr = stdout, stderr
	err = cmd.Run()

	resp := guestwire.Response{
		OK: true, Stdout: stdout.String(), Stderr: stderr.String(),
		Truncated:  stdout.Truncated() || stderr.Truncated(),
		DurationMS: float64(time.Since(start).Microseconds()) / 1000,
	}
	var ee *exec.ExitError
	switch {
	case ctx.Err() == context.DeadlineExceeded:
		resp.ExitCode = core.ExitTimedOut
		resp.Stderr += fmt.Sprintf("timed out after %gs", timeout)
	case errors.As(err, &ee):
		resp.ExitCode = ee.ExitCode()
	case errors.Is(err, exec.ErrWaitDelay):
		resp.ExitCode = cmd.ProcessState.ExitCode()
	case err != nil:
		resp.ExitCode = -1
		resp.Stderr += "\n" + err.Error()
	}
	resp.Fit()
	writeResponse(conn, resp, nil, 0)
}

// execCommand is one exec's process: a plain `sh -c`, never a login shell - this one door runs the
// agent's commands AND the caller's own system commands, and a login shell would run
// $HOME/.profile in all of them. PATH comes from sandbar-init's environment, not /etc/profile.
func execCommand(ctx context.Context, req guestwire.Request, uid int) *exec.Cmd {
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", req.Command)
	cmd.Dir = "/work"
	// Drop to the resolved uid so workload commands never run as root, in a process group of its
	// own: the timeout kills the whole group, so a background child cannot outlive its command.
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid:    true,
		Credential: &syscall.Credential{Uid: uint32(uid), Gid: uint32(uid)},
	}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	// A child still holding stdout after its parent exits would hold Wait forever; past this, the
	// pipes are closed, the shell's own exit code stands and the child runs on. The docker Engine
	// gives an exec's streams the same 2 s after its process exits, so both backends answer
	// `sleep 300 & echo hi` alike.
	cmd.WaitDelay = 2 * time.Second
	if len(req.Env) > 0 {
		env := make([]string, 0, len(req.Env))
		for k, v := range req.Env {
			env = append(env, k+"="+v)
		}
		// Extra vars ride ONLY this exec's environment, appended to the guest's own - never
		// baked into another process's view, exactly like the docker backend's per-exec Env.
		cmd.Env = append(os.Environ(), env...)
	}
	return cmd
}

func doWriteFile(conn io.Reader, req guestwire.Request) {
	w, isW := conn.(io.Writer)
	if req.BodyLen < 0 || req.BodyLen > core.MaxFileBytes {
		if isW {
			writeResponse(w, guestwire.Response{OK: false, Error: fmt.Sprintf("body_len %d out of range", req.BodyLen)}, nil, 0)
		}
		return
	}
	data := make([]byte, req.BodyLen)
	if _, err := io.ReadFull(conn, data); err != nil {
		if isW {
			writeResponse(w, guestwire.Response{OK: false, Error: "read body: " + err.Error()}, nil, 0)
		}
		return
	}
	err := writeFileAs(req.Path, data, os.FileMode(req.Mode), req.UID)
	resp := failure(err)
	if isW {
		writeResponse(w, resp, nil, 0)
	}
}

// writeFileAs writes as uid: root (0) is the caller's own system write, anything else is made by
// that uid itself (asUID), so the guest's permissions bound an agent's write.
func writeFileAs(path string, data []byte, mode os.FileMode, uid int) error {
	if mode == 0 {
		mode = 0o644
	}
	return asUID(uid, func() error {
		return writeOwned(path, mode, uid, uid, func(f *os.File) error {
			_, err := f.Write(data)
			return err
		})
	})
}

// failure is a call's answer for err: ok, or the error with its code when the guest's permissions
// refused it.
func failure(err error) guestwire.Response {
	if err == nil {
		return guestwire.Response{OK: true}
	}
	resp := guestwire.Response{Error: err.Error()}
	switch {
	case forbidden(err):
		resp.Code = guestwire.CodeForbiddenPath
	case errors.Is(err, fs.ErrNotExist):
		resp.Code = guestwire.CodeNoSuchPath
	}
	return resp
}

// doReadFile answers one regular file's bytes, read as req.UID (the host passes the agent's; 0 is
// root's own read), so a file that uid may not read is a forbidden answer. Like the docker backend,
// an agent's read follows a link only as far as that uid could (openForRead); a directory or
// anything else that is no regular file is refused, and so is a file over core.MaxFileBytes,
// before it is read.
func doReadFile(conn io.ReadWriter, req guestwire.Request) {
	var data []byte
	err := asUID(req.UID, func() (err error) {
		data, err = readRegular(req.Path, req.UID)
		return err
	})
	if err != nil {
		writeResponse(conn, failure(err), nil, 0)
		return
	}
	writeResponse(conn, guestwire.Response{OK: true, BodyLen: int64(len(data))}, bytes.NewReader(data), int64(len(data)))
}

// openForRead opens path to read it as uid, never blocking on a planted pipe. Root's own read
// never follows a link (openNoFollow). Another uid's runs on that uid's own thread (asUID), so it
// follows a link as the docker backend's read does, the kernel checking the uid at every hop; a
// /proc magic link (a process's fd, root or cwd) is still refused (RESOLVE_NO_MAGICLINKS), because
// that thread is in guestd's own thread group, to which /proc would hand guestd's open files. On a
// kernel older than openat2 (5.6) it follows no link at all.
func openForRead(path string, uid int) (*os.File, error) {
	if uid == 0 {
		return openNoFollow(path, unix.O_RDONLY|unix.O_NONBLOCK, 0)
	}
	how := &unix.OpenHow{Flags: unix.O_RDONLY | unix.O_NONBLOCK | unix.O_CLOEXEC, Resolve: unix.RESOLVE_NO_MAGICLINKS}
	fd, err := unix.Openat2(unix.AT_FDCWD, path, how)
	if errors.Is(err, unix.ENOSYS) {
		return openNoFollow(path, unix.O_RDONLY|unix.O_NONBLOCK, 0)
	}
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	return os.NewFile(uintptr(fd), path), nil
}

func readRegular(path string, uid int) ([]byte, error) {
	f, err := openForRead(path, uid)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !fi.Mode().IsRegular() {
		return nil, fmt.Errorf("%s holds no regular file", path)
	}
	if fi.Size() > core.MaxFileBytes {
		return nil, fmt.Errorf("%s is %d bytes, over the %d-byte bound on a single file", path, fi.Size(), int64(core.MaxFileBytes))
	}
	return io.ReadAll(io.LimitReader(f, core.MaxFileBytes))
}

func doListDir(conn io.ReadWriter, req guestwire.Request) {
	var entries []os.DirEntry
	err := asUID(req.UID, func() (err error) {
		entries, err = os.ReadDir(req.Path)
		return err
	})
	if err != nil {
		writeResponse(conn, failure(err), nil, 0)
		return
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() {
			name += "/"
		}
		names = append(names, name)
	}
	writeResponse(conn, guestwire.Response{OK: true, Entries: names}, nil, 0)
}

func doArchive(conn io.ReadWriter, req guestwire.Request) {
	// Build the tar in memory to length-prefix it (guest working sets are small and
	// bounded; streaming-without-length would need chunked framing we don't want).
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, p := range req.Paths {
		if err := tarPath(tw, p); err != nil {
			writeResponse(conn, guestwire.Response{OK: false, Error: err.Error()}, nil, 0)
			return
		}
	}
	if err := tw.Close(); err != nil {
		writeResponse(conn, guestwire.Response{OK: false, Error: err.Error()}, nil, 0)
		return
	}
	writeResponse(conn, guestwire.Response{OK: true, BodyLen: int64(buf.Len())}, &buf, int64(buf.Len()))
}

// tarPath walks p and writes basename-rooted entries (tar of /work ⇒ "work/…"), so a
// host-side Extract at / restores exactly. Matches the docker backend's wire shape.
func tarPath(tw *tar.Writer, p string) error {
	base := filepath.Dir(p)
	return filepath.Walk(p, func(file string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(base, file)
		if err != nil {
			return err
		}
		link := ""
		if fi.Mode()&os.ModeSymlink != 0 { // Walk never follows one: the link itself is archived
			if link, err = os.Readlink(file); err != nil {
				return err
			}
		}
		hdr, err := tar.FileInfoHeader(fi, link)
		if err != nil {
			return err
		}
		hdr.Name = rel
		if fi.IsDir() {
			hdr.Name += "/"
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if fi.Mode().IsRegular() {
			f, err := os.Open(file)
			if err != nil {
				return err
			}
			defer f.Close()
			if _, err := io.Copy(tw, f); err != nil {
				return err
			}
		}
		return nil
	})
}

func doExtract(conn io.Reader, req guestwire.Request) {
	w, isW := conn.(io.Writer)
	tr := tar.NewReader(io.LimitReader(conn, req.BodyLen))
	err := extractTar(tr, "/")
	if isW {
		resp := guestwire.Response{OK: err == nil}
		if err != nil {
			resp.Error = err.Error()
		}
		writeResponse(w, resp, nil, 0)
	}
}

func extractTar(tr *tar.Reader, dest string) error {
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		target := filepath.Join(dest, hdr.Name)
		// Refuse path traversal: a crafted "../.." must not escape dest.
		if !strings.HasPrefix(target, filepath.Clean(dest)+string(os.PathSeparator)) && target != filepath.Clean(dest) {
			return fmt.Errorf("tar entry escapes destination: %q", hdr.Name)
		}
		// guestd runs as root and stamps the header's Uid/Gid/Mode exactly - the same contract
		// WriteFile gives a single file, extended to a whole tar so a restored browser profile lands
		// owned by the browser uid, not root. No entry is written through a symlink (nofollow.go):
		// a link the archive made earlier cannot carry a later entry out of its place.
		mode := os.FileMode(hdr.Mode).Perm()
		switch hdr.Typeflag {
		case tar.TypeDir:
			err = makeDirOwned(target, mode, hdr.Uid, hdr.Gid)
		case tar.TypeReg:
			err = writeOwned(target, mode, hdr.Uid, hdr.Gid, func(f *os.File) error {
				_, err := io.Copy(f, tr)
				return err
			})
		case tar.TypeSymlink:
			err = makeSymlink(target, hdr.Linkname, hdr.Uid, hdr.Gid)
		}
		if err != nil {
			return err
		}
	}
}

// ── framing (mirror of firecracker/call.go, guest side) ──────────────────────────

func readRequest(r io.Reader) (guestwire.Request, error) {
	var lb [4]byte
	if _, err := io.ReadFull(r, lb[:]); err != nil {
		return guestwire.Request{}, err
	}
	n := int(lb[0])<<24 | int(lb[1])<<16 | int(lb[2])<<8 | int(lb[3])
	if n < 0 || n > guestwire.MaxHeaderLen {
		return guestwire.Request{}, fmt.Errorf("header length %d out of range", n)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(r, buf); err != nil {
		return guestwire.Request{}, err
	}
	var req guestwire.Request
	return req, json.Unmarshal(buf, &req)
}

func writeResponse(w io.Writer, resp guestwire.Response, body io.Reader, bodyLen int64) {
	hdr, _ := json.Marshal(resp)
	_, _ = w.Write([]byte{byte(len(hdr) >> 24), byte(len(hdr) >> 16), byte(len(hdr) >> 8), byte(len(hdr))})
	_, _ = w.Write(hdr)
	if body != nil && bodyLen > 0 {
		_, _ = io.CopyN(w, body, bodyLen)
	}
}
