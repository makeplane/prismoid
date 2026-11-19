// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package core

import "bytes"

// MaxFileBytes bounds one file a sandbox's caller writes or reads, so a runaway file cannot
// exhaust the daemon's memory.
const MaxFileBytes = 512 << 20

// MaxExecOutput bounds each of an exec's stdout and stderr. What a command prints past it is
// dropped, and the result says so (ExecResult.Truncated).
const MaxExecOutput = 16 << 20

// OutputBuffer keeps the first max bytes written to it and drops the rest, so a command that
// prints without end cannot exhaust the memory of whoever collects its output. A write past the
// bound still reports every byte taken: the writer keeps draining the stream instead of stalling
// the process on a full pipe.
type OutputBuffer struct {
	max       int
	buf       bytes.Buffer
	truncated bool
}

// NewOutputBuffer returns a buffer that keeps at most max bytes.
func NewOutputBuffer(max int) *OutputBuffer { return &OutputBuffer{max: max} }

func (o *OutputBuffer) Write(p []byte) (int, error) {
	if room := o.max - o.buf.Len(); len(p) > room {
		o.truncated = true
		if room > 0 {
			o.buf.Write(p[:room])
		}
		return len(p), nil
	}
	return o.buf.Write(p)
}

// String is what was kept.
func (o *OutputBuffer) String() string { return o.buf.String() }

// Truncated reports whether anything was dropped.
func (o *OutputBuffer) Truncated() bool { return o.truncated }
