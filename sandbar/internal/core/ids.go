// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package core

import (
	"crypto/rand"
	"encoding/hex"
	"regexp"
)

// idPattern is what a caller-supplied sandbox id may look like. Ids become directory
// names, container names, and vsock paths on the host, so they are held to a strict
// filesystem-and-shell-safe shape - no traversal, no spaces, no surprises.
var idPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$`)

// ValidID reports whether id is safe to use as a resource name.
func ValidID(id string) bool { return idPattern.MatchString(id) }

// NewID mints a random id when the caller does not supply one.
func NewID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "sb-" + hex.EncodeToString(b[:])
}
