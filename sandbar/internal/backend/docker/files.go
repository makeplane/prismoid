// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"path"
	"strconv"
	"strings"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// WriteFile places data at p as owner. An agent's write is the agent's own act: it runs inside the
// sandbox as core.AgentUID, so a path the sandbox's permissions keep from the agent - a root-owned
// file, a folder it cannot enter, a link into one - is core.ErrForbiddenPath and nothing is
// written. A system write is the Engine's, as root, and is refused through a parent that is a link.
func (i *instance) WriteFile(ctx context.Context, p string, data []byte, mode fs.FileMode, owner core.Owner) error {
	if err := core.CleanPath(p); err != nil {
		return err
	}
	dir, base := path.Split(p)
	if base == "" {
		return fmt.Errorf("%w: %q names no file", core.ErrBadSpec, p)
	}
	parent := path.Clean(dir)
	if owner == core.OwnerSystem {
		return i.writeSystem(ctx, parent, base, data, mode)
	}
	return i.writeAgent(ctx, parent, p, data, mode)
}

// agentWriteScript writes stdin to $2 as whoever runs it: it makes the parent ($1), writes into a
// fresh name beside the target ($5, refused if anything holds it), sets the mode ($3), checks the
// size against what was sent ($4, so a stream cut short never replaces a file), then renames over
// the target - never into it when the target is a directory. A failure removes the fresh name. Its
// arguments are paths and numbers, never shell text.
const agentWriteScript = `set -eC
mkdir -p -- "$1"
trap 'rm -f -- "$5"' EXIT
cat > "$5"
chmod "$3" -- "$5"
n=$(stat -c %s -- "$5")
[ "$n" = "$4" ] || { echo "short write: $n of $4 bytes" >&2; exit 1; }
mv -fT -- "$5" "$2"`

// writeAgent runs agentWriteScript as the agent, the bytes on its stdin.
func (i *instance) writeAgent(ctx context.Context, parent, p string, data []byte, mode fs.FileMode) error {
	tmp := path.Join(parent, ".sandbar-"+randToken()[:16])
	argv := []string{"/bin/sh", "-c", agentWriteScript, "sh",
		parent, p, strconv.FormatUint(uint64(mode.Perm()), 8), strconv.Itoa(len(data)), tmp}
	exit, stderr, err := i.execAs(ctx, core.AgentUID, argv, bytes.NewReader(data), io.Discard, 0)
	if err != nil {
		return err
	}
	if exit != 0 {
		return refused("write", p, stderr)
	}
	return nil
}

// writeSystem writes as root through the Engine. Docker's PUT archive extracts a tar at a directory
// that MUST already exist, and follows links in that directory's path: so every component of the
// parent is checked first (systemParent), the parent is made as root when it is not all there, and
// the one-entry tar - carrying the file's own uid, gid and mode - is aimed at it. The Engine replaces
// a link at the file's own name rather than following it.
func (i *instance) writeSystem(ctx context.Context, parent, base string, data []byte, mode fs.FileMode) error {
	there, err := i.systemParent(ctx, parent)
	if err != nil {
		return err
	}
	if !there {
		res, err := i.execAsRoot(ctx, "mkdir", "-p", parent)
		if err != nil {
			return err
		}
		if res.ExitCode != 0 { // an Engine that could not start it says why on stdout
			return refused("make", parent, res.Stderr+res.Stdout)
		}
	}
	// The tar is streamed to the Engine as it is written, never assembled in memory: the file is
	// held once, not twice. Closing the reader when the request ends releases the writer.
	pr, pw := io.Pipe()
	defer pr.Close()
	go func() {
		tw := tar.NewWriter(pw)
		hdr := &tar.Header{
			Name: base, Mode: int64(mode.Perm()), Size: int64(len(data)),
			Uid: 0, Gid: 0, Typeflag: tar.TypeReg,
		}
		err := tw.WriteHeader(hdr)
		if err == nil {
			_, err = tw.Write(data)
		}
		if err == nil {
			err = tw.Close()
		}
		pw.CloseWithError(err)
	}()
	return i.b.do(ctx, http.MethodPut, archiveURL(i.cid, parent), "application/x-tar", pr, nil)
}

// systemParent looks, as root and without following any link, at every component of a system
// write's parent: one that is a symbolic link is refused, so a link the workload planted never
// carries root's write somewhere else, and so is one root cannot look at - it cannot be checked.
// The components that exist come first in stat's output (a missing one hides all below it), so the
// n-th line is the n-th component. It fails closed: a stat that failed for any reason but missing
// components, a line that names no kind of file, or a count that does not add up is an error. It
// reports whether the whole parent is already there.
func (i *instance) systemParent(ctx context.Context, parent string) (bool, error) {
	var parts []string
	for d := parent; d != "/"; d = path.Dir(d) {
		parts = append([]string{d}, parts...)
	}
	if len(parts) == 0 {
		return true, nil
	}
	var out bytes.Buffer // one short line per component: "directory", "symbolic link", ...
	exit, stderr, err := i.execAs(ctx, 0, append([]string{"stat", "-c", "%F", "--"}, parts...), nil, &out, int64(32*len(parts)))
	if err != nil {
		return false, err
	}
	missing := 0
	if exit != 0 {
		if missing = notYetThere(stderr); missing == 0 {
			return false, refused("check", parent, stderr)
		}
	}
	var kinds []string
	if out.Len() > 0 {
		kinds = strings.Split(strings.TrimSuffix(out.String(), "\n"), "\n")
	}
	if len(kinds)+missing != len(parts) {
		return false, fmt.Errorf("check %s: stat accounted for %d of its %d components", parent, len(kinds)+missing, len(parts))
	}
	dirs := 0
	for n, kind := range kinds {
		switch {
		case kind == "symbolic link":
			return false, fmt.Errorf("%w: %s is a symbolic link", core.ErrForbiddenPath, parts[n])
		case kind == "directory":
			dirs++
		case !fileKinds[kind]:
			return false, fmt.Errorf("check %s: stat called %s %q, no kind of file", parent, parts[n], kind)
		}
	}
	return dirs == len(parts), nil
}

// fileKinds are stat's words (%F) for the kinds of file a component can be, busybox's and
// coreutils'.
var fileKinds = map[string]bool{
	"directory": true, "symbolic link": true, "regular file": true, "regular empty file": true,
	"fifo": true, "socket": true, "character special file": true, "block special file": true,
}

// notYetThere counts the components a failed stat found missing: every line of its reason must be
// one that is not there (ENOENT). Anything else - a refusal, a command that could not start, no
// reason at all - is 0.
func notYetThere(stderr string) int {
	n := 0
	for _, line := range strings.Split(strings.TrimSpace(stderr), "\n") {
		if !strings.HasSuffix(strings.TrimSpace(line), "No such file or directory") {
			return 0
		}
		n++
	}
	return n
}

// agentReadScript prints the file at $1 as whoever runs it. It looks first (stat, following a link
// as the read will): anything but a regular file - a directory, a pipe that would never end, a
// device - is refused before it is opened, and so is a file over $2 bytes (readOverBound). Then
// it prints the size, on a line of its own, and exactly that many of the file's bytes (head), so a
// file that grew since, or a /proc file whose size says 0, gives no more. Its arguments are a
// path and a number, never shell text.
const agentReadScript = `s=$(stat -L -c '%s %F' -- "$1") || exit 1
n=${s%% *}
case ${s#* } in
"regular file" | "regular empty file") ;;
*) printf '%s is a %s, not a regular file\n' "$1" "${s#* }" >&2; exit 1 ;;
esac
[ "$n" -le "$2" ] || { printf '%s is %s bytes, over the %s-byte bound on a single file\n' "$1" "$n" "$2" >&2; exit 3; }
printf '%s\n' "$n"
exec head -c "$n" -- "$1"`

// readOverBound is agentReadScript's exit for a file over the bound.
const readOverBound = 3

// maxSizeLine bounds the size line agentReadScript prints before the bytes.
const maxSizeLine = 24

// ReadFile returns the bytes at p as the agent reads them: agentReadScript runs inside the sandbox
// as core.AgentUID, so a path the sandbox's permissions keep from the agent - the browser user's
// closed profile, a file only root may read, a link into either - is core.ErrForbiddenPath. A path
// that holds no regular file (absent, under a parent that is a file, a link to nothing, a
// directory, a pipe) is core.ErrNoSuchPath. A file over core.MaxFileBytes is refused before a byte
// of it is read, and the bytes land in one buffer made for the size the look saw: the daemon never
// holds more than that.
func (i *instance) ReadFile(ctx context.Context, p string) ([]byte, error) {
	if err := core.CleanPath(p); err != nil {
		return nil, err
	}
	var out sizedRead
	argv := []string{"/bin/sh", "-c", agentReadScript, "sh", p, strconv.Itoa(core.MaxFileBytes)}
	exit, stderr, err := i.execAs(ctx, core.AgentUID, argv, nil, &out, core.MaxFileBytes+maxSizeLine)
	switch {
	case err != nil:
		return nil, err
	case exit == readOverBound:
		return nil, errors.New(strings.TrimSpace(stderr))
	case exit != 0:
		return nil, readRefused(p, stderr)
	case !out.sized:
		return nil, fmt.Errorf("read %s: no size came before the bytes, only %q", p, out.line)
	}
	return out.data, nil
}

// sizedRead collects what agentReadScript prints - the file's size and a newline, then that many
// bytes - into one buffer made for that size, so a read holds its file once. A first line that is
// no size (what an Engine that could not start the command says) is let by unkept, for the
// command's exit to say why; a byte past the size ends the stream as an error.
type sizedRead struct {
	line  []byte // the first line, until it is a size or plainly is not
	sized bool   // it was: data is made for the size
	none  bool   // it was not
	data  []byte
}

func (r *sizedRead) Write(p []byte) (int, error) {
	n := len(p)
	for !r.sized && !r.none && len(p) > 0 {
		c := p[0]
		p = p[1:]
		if c != '\n' {
			r.line = append(r.line, c)
			r.none = len(r.line) > maxSizeLine
			continue
		}
		size, err := strconv.Atoi(string(r.line))
		if r.none = err != nil || size < 0 || size > core.MaxFileBytes; !r.none {
			// Made before head opens the file, so a file the agent may stat but not read costs
			// this before its refusal: still within the bound, and the open stays head's alone, so
			// one command's words name every refusal.
			r.sized, r.data = true, make([]byte, 0, size)
		}
	}
	switch {
	case r.none || len(p) == 0:
		return n, nil
	case len(p) > cap(r.data)-len(r.data):
		return 0, fmt.Errorf("the read gave more than the %d bytes its size said", cap(r.data))
	}
	r.data = append(r.data, p...)
	return n, nil
}

// readRefused names why a read failed: a path with no file to read - agentReadScript's own words,
// or the errno words stat and head end on in the C locale, busybox's and coreutils' - is
// core.ErrNoSuchPath; a refusal the sandbox's permissions made is core.ErrForbiddenPath; anything
// else is a failure.
func readRefused(p, stderr string) error {
	msg := strings.TrimSpace(stderr)
	for _, words := range []string{
		"not a regular file", "No such file or directory", "Not a directory", "Is a directory",
		"Symbolic link loop", "Too many levels of symbolic links",
	} {
		if strings.HasSuffix(msg, words) {
			return fmt.Errorf("%w: %s: %s", core.ErrNoSuchPath, p, msg)
		}
	}
	return refused("read", p, stderr)
}

func (i *instance) ListDir(ctx context.Context, p string) ([]string, error) {
	res, err := i.Exec(ctx, core.ExecRequest{
		// The C locale: ls's errno words below tell a missing path from a refusal, and a frame
		// image with another LANG would translate them.
		Command: "LC_ALL=C ls -1Ap " + shellQuote(p), TimeoutS: 30,
	})
	if err != nil {
		return nil, err
	}
	if res.ExitCode != 0 {
		msg := strings.TrimSpace(res.Stderr)
		// ENOENT or ENOTDIR is a path that is not there; a permission refusal is the agent's
		// (core.ErrForbiddenPath, as firecracker answers it); anything else (a listing that timed
		// out) is a failure. ls names the errno in both busybox and coreutils.
		if strings.Contains(msg, "No such file or directory") || strings.Contains(msg, "Not a directory") {
			return nil, fmt.Errorf("%w: list %s: %s", core.ErrNoSuchPath, p, msg)
		}
		return nil, refused("list", p, msg)
	}
	if res.Truncated {
		return nil, fmt.Errorf("list %s: the listing is over %d bytes", p, core.MaxExecOutput)
	}
	var out []string
	for _, line := range strings.Split(res.Stdout, "\n") {
		if line != "" {
			out = append(out, line)
		}
	}
	return out, nil
}
