// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package api

import (
	"fmt"
	"io"
)

// readAllLimit reads up to max+1 bytes and refuses anything larger, so a runaway
// upload cannot exhaust daemon memory. The +1 is how we tell "exactly at the limit"
// (fine) from "over it" (rejected).
func readAllLimit(r io.Reader, max int64) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(r, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > max {
		return nil, fmt.Errorf("payload exceeds %d bytes", max)
	}
	return data, nil
}
