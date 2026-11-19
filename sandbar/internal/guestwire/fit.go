// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package guestwire

import "unicode/utf8"

// Fit cuts an exec response's Stdout and Stderr, keeping each one's start and cutting at rune
// boundaries, until the encoded header is sure to fit MaxHeaderLen, and marks the response
// Truncated when it cuts. Two outputs of core.MaxExecOutput can escape past the bound (a NUL
// becomes \u0000), and a header over it is refused by the host: the caller would get a failed
// exec instead of a truncated one. The other fields must be small, as an exec's are.
func (r *Response) Fit() {
	budget := MaxHeaderLen - headerSlack
	out, errs := escapedLen(r.Stdout), escapedLen(r.Stderr)
	if out+errs <= budget {
		return
	}
	// Each output gets half the room; one that needs less leaves the rest to the other.
	outBudget := budget / 2
	if errs < budget-outBudget {
		outBudget = budget - errs
	}
	r.Stdout = cutEscaped(r.Stdout, outBudget)
	r.Stderr = cutEscaped(r.Stderr, budget-escapedLen(r.Stdout))
	r.Truncated = true
}

// escapedLen is an upper bound on s's length once JSON-encoded, quotes excluded.
func escapedLen(s string) int {
	n := 0
	for len(s) > 0 {
		r, size := utf8.DecodeRuneInString(s)
		n += escapedRuneLen(r, size)
		s = s[size:]
	}
	return n
}

// cutEscaped returns the longest start of s whose escapedLen is at most budget.
func cutEscaped(s string, budget int) string {
	n, i := 0, 0
	for i < len(s) {
		r, size := utf8.DecodeRuneInString(s[i:])
		if n += escapedRuneLen(r, size); n > budget {
			break
		}
		i += size
	}
	return s[:i]
}

// escapedRuneLen bounds one rune's encoded length: encoding/json writes a control character, a
// quote, a backslash, <, >, &, U+2028, U+2029 and an invalid byte (as �) in at most six bytes,
// and every other rune as itself.
func escapedRuneLen(r rune, size int) int {
	switch {
	case r == utf8.RuneError && size == 1, r < 0x20, r == '"', r == '\\', r == '<', r == '>', r == '&',
		r == ' ', r == ' ':
		return 6
	}
	return size
}
