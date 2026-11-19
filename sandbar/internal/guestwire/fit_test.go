// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package guestwire

import (
	"encoding/json"
	"strings"
	"testing"
)

// Two outputs of 16 MiB of NULs escape to about 192 MiB, past the bound the host reads a header
// under: Fit cuts them until the header fits, and the response says it was cut.
func TestFitCutsNulHeavyOutputUnderTheBound(t *testing.T) {
	nuls := strings.Repeat("\x00", 16<<20)
	r := Response{OK: true, Stdout: nuls, Stderr: nuls + "timed out after 5s", ExitCode: 124, DurationMS: 5000}
	if hdr, _ := json.Marshal(r); len(hdr) <= MaxHeaderLen {
		t.Fatalf("the case must start over the bound, encodes to %d", len(hdr))
	}
	r.Fit()
	hdr, err := json.Marshal(r)
	if err != nil || len(hdr) > MaxHeaderLen {
		t.Fatalf("want a header under %d bytes, got %d (%v)", MaxHeaderLen, len(hdr), err)
	}
	if !r.Truncated || r.Stdout == "" || r.Stderr == "" || !strings.HasPrefix(nuls, r.Stdout) {
		t.Fatalf("want both outputs' starts kept and truncated set: stdout %d, stderr %d, truncated %v",
			len(r.Stdout), len(r.Stderr), r.Truncated)
	}
	// The room is shared: the two halves differ by at most the stderr suffix's escaping.
	if d := len(r.Stdout) - len(r.Stderr); d < -64 || d > 64 {
		t.Fatalf("want the room split evenly, stdout %d vs stderr %d", len(r.Stdout), len(r.Stderr))
	}
}

// An output that needs little room leaves the rest to the other; a cut never splits a rune, and
// invalid bytes and HTML characters, which escape to six bytes, count as such.
func TestFitGivesUnusedRoomAwayAndCutsAtRunes(t *testing.T) {
	big := strings.Repeat("<\xffé", 12<<20) // 4 raw bytes, 14 escaped: 168 MiB in all
	r := Response{OK: true, Stdout: big, Stderr: "small"}
	r.Fit()
	hdr, _ := json.Marshal(r)
	if len(hdr) > MaxHeaderLen || !r.Truncated || r.Stderr != "small" {
		t.Fatalf("header %d bytes, stderr %q, truncated %v", len(hdr), r.Stderr, r.Truncated)
	}
	if !strings.HasPrefix(big, r.Stdout) || strings.HasSuffix(r.Stdout, "\xc3") {
		t.Fatal("the cut must keep stdout's start and never split é")
	}
	if len(r.Stdout) < (MaxHeaderLen-headerSlack-5)/14*4-4 {
		t.Fatalf("stdout must get the room stderr left, kept %d bytes", len(r.Stdout))
	}
}

// Output that already fits, even at the full 16 MiB each, is left alone.
func TestFitLeavesAFittingResponseAlone(t *testing.T) {
	x := strings.Repeat("x", 16<<20)
	r := Response{OK: true, Stdout: x, Stderr: "a\n\"b\""}
	r.Fit()
	if r.Truncated || r.Stdout != x || r.Stderr != "a\n\"b\"" {
		t.Fatalf("a fitting response must not change: stdout %d, stderr %q, truncated %v", len(r.Stdout), r.Stderr, r.Truncated)
	}
}
