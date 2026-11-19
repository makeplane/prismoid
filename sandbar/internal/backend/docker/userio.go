// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// The file door's way into a sandbox: a fixed argv run inside it as one uid, so the sandbox's own
// permissions decide what that uid may touch. A path rides as its own argument, never inside shell
// text, and every command runs in the C locale, so the errno words its caller reads are stable.

// errOverLimit is a command whose stdout ran past what its caller would take: the stream is cut
// there, so the daemon never holds more than it asked for.
var errOverLimit = errors.New("output over the limit")

// maxStderr bounds what execAs keeps of a command's stderr: room for the reason it failed.
const maxStderr = 64 << 10

// maxStdoutReason bounds what execAs keeps of stdout to say why a command failed: the Engine tells
// a command it could not start at all (exit 126 or 127) on stdout, not on stderr.
const maxStdoutReason = 4 << 10

// The Engine's exit codes for a command it could not start: not executable, not found.
const (
	exitCannotRun = 126
	exitNotFound  = 127
)

// execAs runs argv inside the container as uid:uid. When stdin is given it is streamed in and its
// end is the command's end of input. At most limit bytes of stdout reach stdout; one more is
// errOverLimit, carrying the start of what was printed, and the command's stream ends there. It
// reports the exit code and what was kept of stderr; for a command that failed without a word
// there, the reason is the Engine's from stdout when it could not start the command (exit 126 or
// 127), and otherwise the exit code - never what the command printed, which may be a file's bytes.
// A command that exits non-zero is a result, not an error. Every command is found in the system's
// own folders (systemPATH), and root runs with root's own HOME (execAsRoot's rule).
func (i *instance) execAs(ctx context.Context, uid int, argv []string, stdin io.Reader, stdout io.Writer, limit int64) (exit int, stderr string, err error) {
	env := []string{"LC_ALL=C", systemPATH}
	if uid == 0 {
		env = append(env, "HOME=/root")
	}
	id, err := i.createExec(ctx, fmt.Sprintf("%d:%d", uid, uid), env, argv, stdin != nil)
	if err != nil {
		return 0, "", err
	}
	errs, head := core.NewOutputBuffer(maxStderr), core.NewOutputBuffer(maxStdoutReason)
	out := io.MultiWriter(head, &capped{w: stdout, left: limit})
	if stdin == nil {
		err = i.b.execStart(ctx, id, out, errs)
	} else {
		err = i.b.feedExec(ctx, id, stdin, out, errs)
	}
	if err == nil {
		exit, err = i.b.exitCode(ctx, id)
	}
	stderr = errs.String()
	switch {
	case errors.Is(err, errOverLimit):
		err = fmt.Errorf("%w of %d bytes: %s", errOverLimit, limit, strings.TrimSpace(head.String()))
	case err == nil && exit != 0 && strings.TrimSpace(stderr) == "":
		stderr = fmt.Sprintf("exit %d, with no reason given", exit)
		if (exit == exitCannotRun || exit == exitNotFound) && strings.TrimSpace(head.String()) != "" {
			stderr = head.String()
		}
	}
	return exit, stderr, err
}

// feedExec runs a created exec with src as its stdin: src is copied up and the connection's write
// side closed, so the command reads its input's end, while stdout and stderr come down
// demultiplexed. A command that stops reading early is no error here - its exit code and stderr say
// why, read to the end of the stream. A src that fails is one: the connection is closed, so the
// command sees its input end short instead of waiting for more.
func (b *Backend) feedExec(ctx context.Context, execID string, src io.Reader, stdout, stderr io.Writer) error {
	conn, br, stop, err := b.hijackExec(ctx, execID)
	if err != nil {
		return err
	}
	defer stop()
	defer conn.Close()
	half, ok := conn.(interface{ CloseWrite() error })
	if !ok {
		return fmt.Errorf("exec %s: the Engine's connection cannot end its input alone", execID)
	}
	in := &source{r: src}
	fed := make(chan struct{})
	go func() {
		defer close(fed)
		_, err := io.Copy(conn, in)
		switch {
		case in.err != nil:
			_ = conn.Close()
		case err == nil:
			_ = half.CloseWrite()
		}
		// A write that failed is a command that stopped reading: the stream still ends on its own.
	}()
	err = demuxInto(br, stdout, stderr)
	_ = conn.Close() // a feed the command stopped reading ends here
	<-fed
	switch {
	case ctx.Err() != nil:
		return ctx.Err()
	case in.err != nil:
		return in.err
	}
	return err
}

// source keeps the error its reader failed with, telling it from the connection's.
type source struct {
	r   io.Reader
	err error
}

func (s *source) Read(p []byte) (int, error) {
	n, err := s.r.Read(p)
	if err != nil && err != io.EOF {
		s.err = err
	}
	return n, err
}

// capped passes on at most left bytes; a write past them is errOverLimit, which ends the stream.
type capped struct {
	w    io.Writer
	left int64
}

func (c *capped) Write(p []byte) (int, error) {
	if int64(len(p)) <= c.left {
		n, err := c.w.Write(p)
		c.left -= int64(n)
		return n, err
	}
	n, err := c.w.Write(p[:c.left])
	c.left -= int64(n)
	if err == nil {
		err = errOverLimit
	}
	return n, err
}

// refused names why a command the file door ran failed: a refusal the sandbox's permissions made
// (EACCES, EPERM) is the caller's core.ErrForbiddenPath; anything else is a failure, with its reason.
func refused(verb, p, stderr string) error {
	msg := strings.TrimSpace(stderr)
	if strings.Contains(msg, "Permission denied") || strings.Contains(msg, "Operation not permitted") {
		return fmt.Errorf("%w: %s %s: %s", core.ErrForbiddenPath, verb, p, msg)
	}
	return fmt.Errorf("%s %s: %s", verb, p, msg)
}
