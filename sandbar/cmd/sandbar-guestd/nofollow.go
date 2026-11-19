// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

//go:build linux

package main

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// guestd runs as root, and the workload can plant a symlink anywhere it may write. Every path
// guestd writes is therefore opened without following a symlink at any component, so a link the
// workload made can never turn a write into one on a file the workload does not own.

// openNoFollow opens path, refusing a symlink at any component (openat2's RESOLVE_NO_SYMLINKS).
// On a kernel older than openat2 (5.6) it checks every existing component and refuses a symlink
// at the last one (O_NOFOLLOW).
func openNoFollow(path string, flags int, mode uint32) (*os.File, error) {
	how := &unix.OpenHow{Flags: uint64(flags | unix.O_CLOEXEC), Resolve: unix.RESOLVE_NO_SYMLINKS}
	if flags&unix.O_CREAT != 0 {
		how.Mode = uint64(mode)
	}
	fd, err := unix.Openat2(unix.AT_FDCWD, path, how)
	if errors.Is(err, unix.ENOSYS) {
		if err = noSymlinkIn(path); err != nil {
			return nil, err
		}
		fd, err = unix.Open(path, flags|unix.O_NOFOLLOW|unix.O_CLOEXEC, mode)
	}
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	return os.NewFile(uintptr(fd), path), nil
}

// noSymlinkIn refuses a path any existing component of which is a symlink.
func noSymlinkIn(path string) error {
	p := "/"
	for _, part := range strings.Split(filepath.Clean(path), "/") {
		if part == "" {
			continue
		}
		p = filepath.Join(p, part)
		fi, err := os.Lstat(p)
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if fi.Mode()&fs.ModeSymlink != 0 {
			return fmt.Errorf("%s is a symlink: %w", p, errSymlink)
		}
	}
	return nil
}

// mkdirAllNoFollow is os.MkdirAll that refuses a symlink on the way. A link swapped in after the
// check can at most place an empty directory; the open that follows still refuses it.
func mkdirAllNoFollow(dir string, mode fs.FileMode) error {
	if err := noSymlinkIn(dir); err != nil {
		return err
	}
	return os.MkdirAll(dir, mode)
}

// replaceSymlink removes a symlink at path, so the file written there replaces the link (as the
// docker backend's archive write does) instead of writing through it.
func replaceSymlink(path string) error {
	fi, err := os.Lstat(path)
	if err != nil || fi.Mode()&fs.ModeSymlink == 0 {
		return nil
	}
	return os.Remove(path)
}

// writeOwned writes one file through openNoFollow with an exact mode and owner, set on the open
// file so no path is resolved again. The open's own mode is pre-umask, hence the Chmod.
func writeOwned(path string, mode fs.FileMode, uid, gid int, write func(*os.File) error) error {
	if err := mkdirAllNoFollow(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	if err := replaceSymlink(path); err != nil {
		return err
	}
	// O_NONBLOCK: a pipe planted at the target fails (ENXIO with no reader) instead of pinning the
	// call forever; one with a reader is refused below as no regular file.
	f, err := openNoFollow(path, unix.O_WRONLY|unix.O_CREAT|unix.O_TRUNC|unix.O_NONBLOCK, uint32(mode.Perm()))
	if err != nil {
		return err
	}
	defer f.Close()
	if fi, err := f.Stat(); err != nil || !fi.Mode().IsRegular() {
		return fmt.Errorf("%s is no regular file: refusing to write it", path)
	}
	if err := write(f); err != nil {
		return err
	}
	if err := f.Chmod(mode); err != nil {
		return err
	}
	return f.Chown(uid, gid)
}

// makeDirOwned makes a directory with an exact mode and owner, set on the open directory.
func makeDirOwned(path string, mode fs.FileMode, uid, gid int) error {
	if err := mkdirAllNoFollow(path, mode); err != nil {
		return err
	}
	f, err := openNoFollow(path, unix.O_RDONLY|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	if err := f.Chmod(mode); err != nil {
		return err
	}
	return f.Chown(uid, gid)
}

// makeSymlink places a symlink, replacing a file or link already there (never a directory), owned
// as the archive says - the link itself, never its target. Every step is relative to the parent
// directory opened without following a link, so nothing resolves the path again.
func makeSymlink(path, target string, uid, gid int) error {
	dir, base := filepath.Split(path)
	if err := mkdirAllNoFollow(dir, 0o755); err != nil {
		return err
	}
	d, err := openNoFollow(dir, unix.O_RDONLY|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer d.Close()
	dfd := int(d.Fd())
	var st unix.Stat_t
	if err := unix.Fstatat(dfd, base, &st, unix.AT_SYMLINK_NOFOLLOW); err == nil && st.Mode&unix.S_IFMT != unix.S_IFDIR {
		if err := unix.Unlinkat(dfd, base, 0); err != nil {
			return &os.PathError{Op: "unlink", Path: path, Err: err}
		}
	}
	if err := unix.Symlinkat(target, dfd, base); err != nil {
		return &os.PathError{Op: "symlink", Path: path, Err: err}
	}
	if err := unix.Fchownat(dfd, base, uid, gid, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return &os.PathError{Op: "lchown", Path: path, Err: err}
	}
	return nil
}
