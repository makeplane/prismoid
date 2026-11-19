// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package core

import "testing"

// An OutputBuffer keeps its first max bytes, takes every write whole (so the writer keeps draining
// instead of stalling the process), and says whether it dropped anything.
func TestOutputBufferKeepsTheFirstMaxBytes(t *testing.T) {
	o := NewOutputBuffer(5)
	for _, chunk := range []string{"abc", "defg", "hij"} {
		if n, err := o.Write([]byte(chunk)); n != len(chunk) || err != nil {
			t.Fatalf("write %q: %d %v", chunk, n, err)
		}
	}
	if o.String() != "abcde" || !o.Truncated() {
		t.Fatalf("got %q, truncated %v", o.String(), o.Truncated())
	}
	exact := NewOutputBuffer(3)
	_, _ = exact.Write([]byte("abc"))
	if exact.String() != "abc" || exact.Truncated() {
		t.Fatalf("output at the bound is whole: %q %v", exact.String(), exact.Truncated())
	}
}
