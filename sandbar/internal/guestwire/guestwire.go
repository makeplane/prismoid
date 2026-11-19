// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package guestwire is the protocol spoken between the firecracker backend (host) and
// sandbar-guestd (inside the VM), over a single vsock byte stream.
//
// It is deliberately the SAME shape as sandbar's public HTTP API - request line,
// JSON body, JSON (or raw) response - so the guest agent is essentially a tiny HTTP
// server on vsock. Keeping the two protocols isomorphic means one mental model covers
// the whole path from consumer to kernel-isolated workload.
//
// This package is imported by BOTH the host backend and the guest binary, so it must
// stay OS-neutral (no syscalls, no build tags) - just the types on the wire and the bound
// their headers keep.
package guestwire

import "io/fs"

// Op names a guest operation.
type Op string

const (
	OpPing      Op = "ping"
	OpExec      Op = "exec"
	OpWriteFile Op = "write_file"
	OpReadFile  Op = "read_file"
	OpListDir   Op = "list_dir"
	OpArchive   Op = "archive" // response body is a raw tar stream, not JSON
	OpExtract   Op = "extract" // request body is a raw tar stream, not JSON
)

// CodeForbiddenPath is Response.Code for a path the guest's own permissions keep from the uid.
const CodeForbiddenPath = "forbidden_path"

// CodeNoSuchPath is Response.Code for a path the guest does not hold.
const CodeNoSuchPath = "no_such_path"

// MaxHeaderLen bounds a JSON header on either side. An exec's response carries its stdout and
// stderr (each at most core.MaxExecOutput) in the header, escaped; a byte can escape to six, so
// guestd cuts them further with Response.Fit rather than send a header the host refuses.
const MaxHeaderLen = 128 << 20

// headerSlack is the room Fit leaves for an exec response's other fields: a few numbers and
// flags.
const headerSlack = 64 << 10

// Request is the JSON header of every guest call. For OpExtract, a tar stream follows
// the header (length-prefixed by BodyLen); for all others the header is the whole
// request.
type Request struct {
	Op    Op       `json:"op"`
	Path  string   `json:"path,omitempty"`
	Paths []string `json:"paths,omitempty"`
	// Exec
	Command  string            `json:"command,omitempty"`
	TimeoutS float64           `json:"timeout_s,omitempty"`
	User     string            `json:"user,omitempty"` // "" or "agent" ⇒ core.AgentUID; "browser" ⇒ core.BrowserUID
	Env      map[string]string `json:"env,omitempty"`  // extra environment, visible only to that one exec
	// WriteFile, ReadFile and ListDir: the uid the file is touched as (0, or none, is root's own).
	Mode fs.FileMode `json:"mode,omitempty"`
	UID  int         `json:"uid,omitempty"`
	// BodyLen is the number of raw bytes that follow this JSON header (file contents
	// for write_file, tar for extract). Zero means no trailing body.
	BodyLen int64 `json:"body_len,omitempty"`
}

// Response is the JSON header of every guest reply. For OpReadFile and OpArchive,
// BodyLen raw bytes follow the header.
type Response struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
	// Code names a failure the host maps to a typed error; CodeForbiddenPath is the guest's
	// permissions refusing the uid the file was touched as.
	Code string `json:"code,omitempty"`
	// Exec
	ExitCode   int     `json:"exit_code,omitempty"`
	Stdout     string  `json:"stdout,omitempty"`
	Stderr     string  `json:"stderr,omitempty"`
	DurationMS float64 `json:"duration_ms,omitempty"`
	Truncated  bool    `json:"truncated,omitempty"` // stdout or stderr reached core.MaxExecOutput
	// ListDir
	Entries []string `json:"entries,omitempty"`
	// BodyLen: raw bytes following this header (file contents / tar).
	BodyLen int64 `json:"body_len,omitempty"`
}
